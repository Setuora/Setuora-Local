from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models import Batch
from app.services.settings import gst_rate_key
from app.services.voucher import calculate_voucher_summary


_IST = timezone(timedelta(hours=5, minutes=30))
_ONES = (
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _under_thousand(value: int) -> str:
    parts: list[str] = []
    if value >= 100:
        parts.extend((_ONES[value // 100], "Hundred"))
        value %= 100
    if value >= 20:
        parts.append(_TENS[value // 10])
        if value % 10:
            parts.append(_ONES[value % 10])
    elif value:
        parts.append(_ONES[value])
    return " ".join(parts)


def integer_in_indian_words(value: int) -> str:
    if value == 0:
        return _ONES[0]
    if value < 0:
        return f"Minus {integer_in_indian_words(abs(value))}"

    parts: list[str] = []
    crore, value = divmod(value, 10_000_000)
    lakh, value = divmod(value, 100_000)
    thousand, value = divmod(value, 1_000)
    if crore:
        parts.extend((integer_in_indian_words(crore), "Crore"))
    if lakh:
        parts.extend((_under_thousand(lakh), "Lakh"))
    if thousand:
        parts.extend((_under_thousand(thousand), "Thousand"))
    if value:
        parts.append(_under_thousand(value))
    return " ".join(parts)


def amount_in_words(value: Decimal) -> str:
    amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "Minus " if amount < 0 else ""
    amount = abs(amount)
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    words = f"{sign}INR {integer_in_indian_words(rupees)}"
    if paise:
        words += f" and {integer_in_indian_words(paise)} Paise"
    return f"{words} Only"


def _local_datetime(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_IST)


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _page_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#b8bcc4"))
    canvas.line(10 * mm, 10 * mm, A4[0] - 10 * mm, 10 * mm)
    canvas.setFillColor(colors.HexColor("#555b66"))
    canvas.setFont("Helvetica", 7)
    canvas.drawString(10 * mm, 6.5 * mm, "Computer Generated Pre-Invoice - Not a Tax Invoice")
    canvas.drawRightString(A4[0] - 10 * mm, 6.5 * mm, f"Page {doc.page}")
    canvas.restoreState()


def sale_preinvoice_pdf(batch: Batch, settings: dict[str, str]) -> bytes:
    summary = calculate_voucher_summary(batch)
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=14 * mm,
        title=f"Pre-Invoice {batch.batch_number}",
        author=settings.get("company_name") or "Setuora",
        pageCompression=0,
    )

    ink = colors.HexColor("#20242b")
    muted = colors.HexColor("#5e6570")
    line = colors.HexColor("#aeb4be")
    header_fill = colors.HexColor("#edf4fb")
    warning_fill = colors.HexColor("#fff5d9")

    title = ParagraphStyle(
        "PreInvoiceTitle",
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=18,
        alignment=TA_CENTER,
        textColor=ink,
    )
    subtitle = ParagraphStyle(
        "PreInvoiceSubtitle",
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#8a5b00"),
    )
    company = ParagraphStyle(
        "PreInvoiceCompany",
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=ink,
    )
    label = ParagraphStyle(
        "PreInvoiceLabel",
        fontName="Helvetica",
        fontSize=7,
        leading=9,
        textColor=muted,
    )
    value = ParagraphStyle(
        "PreInvoiceValue",
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=ink,
    )
    body = ParagraphStyle(
        "PreInvoiceBody",
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=ink,
    )
    body_bold = ParagraphStyle(
        "PreInvoiceBodyBold",
        parent=body,
        fontName="Helvetica-Bold",
    )
    body_right = ParagraphStyle(
        "PreInvoiceBodyRight",
        parent=body,
        alignment=TA_RIGHT,
    )
    table_header = ParagraphStyle(
        "PreInvoiceTableHeader",
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=8,
        alignment=TA_CENTER,
        textColor=ink,
    )
    small = ParagraphStyle(
        "PreInvoiceSmall",
        fontName="Helvetica",
        fontSize=7,
        leading=9,
        textColor=muted,
    )

    company_name = escape((settings.get("company_name") or "Company").strip())
    reference_name = escape((batch.party_name or "Customer").strip())
    customer_state = escape((getattr(batch, "party_state", None) or "-").strip())
    gst_registration_type = escape(
        (getattr(batch, "party_gst_registration_type", None) or "Unregistered/Consumer").strip()
    )
    party_gst_name = escape(
        (getattr(batch, "party_gst_name", None) or batch.party_name or "Customer").strip()
    )
    party_gstin = escape((getattr(batch, "party_gstin", None) or "-").strip())
    gst_type = "IGST" if getattr(batch, "gst_treatment", None) == "INTER_STATE" else "CGST + SGST"
    document_date = _local_datetime(batch.submitted_at or batch.created_at)

    story = [
        Paragraph("PRE-INVOICE", title),
        Paragraph("PROVISIONAL SALES BILL - NOT A TAX INVOICE", subtitle),
        Spacer(1, 3 * mm),
    ]

    document_details = Table(
        [
            [Paragraph("Pre-Invoice No.", label), Paragraph(escape(batch.batch_number), value)],
            [Paragraph("Date", label), Paragraph(document_date.strftime("%d-%b-%Y"), value)],
            [Paragraph("Sale Status", label), Paragraph(escape(batch.status), value)],
            [Paragraph("GST Type", label), Paragraph(gst_type, value)],
        ],
        colWidths=[27 * mm, 43 * mm],
    )
    document_details.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.45, line),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ]
        )
    )
    header = Table(
        [
            [
                [
                    Paragraph(company_name, company),
                    Paragraph("Sales pre-invoice generated by Setuora", small),
                ],
                document_details,
            ]
        ],
        colWidths=[110 * mm, 70 * mm],
    )
    header.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.7, line),
                ("INNERGRID", (0, 0), (-1, -1), 0.45, line),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 3 * mm),
                ("RIGHTPADDING", (0, 0), (0, 0), 3 * mm),
                ("TOPPADDING", (0, 0), (0, 0), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (0, 0), 3 * mm),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (1, 0), (1, 0), 0),
                ("BOTTOMPADDING", (1, 0), (1, 0), 0),
            ]
        )
    )
    story.extend((header, Spacer(1, 3 * mm)))

    party = Table(
        [
            [
                [
                    Paragraph("Consignee / Reference Name", label),
                    Paragraph(reference_name, value),
                    Paragraph("Customer State", label),
                    Paragraph(customer_state, body),
                ],
                [
                    Paragraph("Buyer / Bill To", label),
                    Paragraph(party_gst_name, value),
                    Paragraph("GST Registration", label),
                    Paragraph(gst_registration_type, body),
                    Paragraph("GST Number", label),
                    Paragraph(party_gstin, body),
                ],
            ]
        ],
        colWidths=[90 * mm, 90 * mm],
    )
    party.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.7, line),
                ("INNERGRID", (0, 0), (-1, -1), 0.45, line),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]
        )
    )
    story.extend((party, Spacer(1, 3 * mm)))

    item_rows = [
        [
            Paragraph("Sl.", table_header),
            Paragraph("Description of Goods", table_header),
            Paragraph("HSN/SAC", table_header),
            Paragraph("Quantity", table_header),
            Paragraph("Rate", table_header),
            Paragraph("Disc. %", table_header),
            Paragraph("Taxable", table_header),
            Paragraph("Amount", table_header),
        ]
    ]
    for index, item in enumerate(summary.lines, start=1):
        item_rows.append(
            [
                Paragraph(str(index), body_right),
                Paragraph(escape(item.product_name), body_bold),
                Paragraph(escape(item.hsn), body),
                Paragraph(f"{item.quantity} {escape(item.unit)}", body_right),
                Paragraph(_money(item.rate), body_right),
                Paragraph(_money(item.discount_rate), body_right),
                Paragraph(_money(item.taxable_value), body_right),
                Paragraph(_money(item.line_total), body_right),
            ]
        )
    if not summary.lines:
        item_rows.append(
            [
                "",
                Paragraph("No sale items", body),
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    items = LongTable(
        item_rows,
        repeatRows=1,
        colWidths=[8 * mm, 58 * mm, 20 * mm, 20 * mm, 20 * mm, 16 * mm, 23 * mm, 25 * mm],
        splitByRow=1,
    )
    items.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), header_fill),
                ("GRID", (0, 0), (-1, -1), 0.4, line),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1.2 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ]
        )
    )
    story.extend((items, Spacer(1, 3 * mm)))

    tax_by_rate: dict[str, dict[str, Decimal]] = {}
    for item in summary.lines:
        rate = gst_rate_key(item.gst_rate)
        amounts = tax_by_rate.setdefault(
            rate,
            {
                "taxable": Decimal("0"),
                "cgst_rate": item.cgst_rate,
                "sgst_rate": item.sgst_rate,
                "igst_rate": item.igst_rate,
                "cgst": Decimal("0"),
                "sgst": Decimal("0"),
                "igst": Decimal("0"),
            },
        )
        amounts["taxable"] += item.taxable_value
        amounts["cgst"] += item.cgst_amount
        amounts["sgst"] += item.sgst_amount
        amounts["igst"] += item.igst_amount

    if tax_by_rate:
        if summary.igst_amount > 0:
            tax_rows = [
                [
                    Paragraph("GST Rate", table_header),
                    Paragraph("Taxable Value", table_header),
                    Paragraph("IGST Rate", table_header),
                    Paragraph("IGST Amount", table_header),
                ]
            ]
            for rate, amounts in sorted(tax_by_rate.items(), key=lambda pair: Decimal(pair[0])):
                tax_rows.append(
                    [
                        Paragraph(f"{rate}%", body_right),
                        Paragraph(_money(amounts["taxable"]), body_right),
                        Paragraph(f"{gst_rate_key(amounts['igst_rate'])}%", body_right),
                        Paragraph(_money(amounts["igst"]), body_right),
                    ]
                )
            col_widths = [32 * mm, 48 * mm, 40 * mm, 60 * mm]
        else:
            tax_rows = [
                [
                    Paragraph("GST Rate", table_header),
                    Paragraph("Taxable Value", table_header),
                    Paragraph("CGST Rate", table_header),
                    Paragraph("CGST Amount", table_header),
                    Paragraph("SGST Rate", table_header),
                    Paragraph("SGST Amount", table_header),
                ]
            ]
            for rate, amounts in sorted(tax_by_rate.items(), key=lambda pair: Decimal(pair[0])):
                tax_rows.append(
                    [
                        Paragraph(f"{rate}%", body_right),
                        Paragraph(_money(amounts["taxable"]), body_right),
                        Paragraph(f"{gst_rate_key(amounts['cgst_rate'])}%", body_right),
                        Paragraph(_money(amounts["cgst"]), body_right),
                        Paragraph(f"{gst_rate_key(amounts['sgst_rate'])}%", body_right),
                        Paragraph(_money(amounts["sgst"]), body_right),
                    ]
                )
            col_widths = [22 * mm, 34 * mm, 25 * mm, 34 * mm, 25 * mm, 40 * mm]
        tax_table = Table(
            tax_rows,
            repeatRows=1,
            colWidths=col_widths,
        )
        tax_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), header_fill),
                    ("GRID", (0, 0), (-1, -1), 0.4, line),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 1.5 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ]
            )
        )
        story.extend((Paragraph("GST Breakup", body_bold), Spacer(1, 1.2 * mm), tax_table, Spacer(1, 3 * mm)))

    totals = Table(
        [
            [Paragraph("Taxable Value", body), Paragraph(_money(summary.taxable_value), body_right)],
            [Paragraph("CGST", body), Paragraph(_money(summary.cgst_amount), body_right)],
            [Paragraph("SGST", body), Paragraph(_money(summary.sgst_amount), body_right)],
            [Paragraph("IGST", body), Paragraph(_money(summary.igst_amount), body_right)],
            [Paragraph("Round Off", body), Paragraph(_money(summary.round_off), body_right)],
            [Paragraph("Pre-Invoice Total (INR)", body_bold), Paragraph(_money(summary.final_value), body_bold)],
        ],
        colWidths=[55 * mm, 35 * mm],
        hAlign="RIGHT",
    )
    totals.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, line),
                ("BACKGROUND", (0, -1), (-1, -1), header_fill),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ]
        )
    )
    story.extend(
        (
            totals,
            Spacer(1, 3 * mm),
            Paragraph("<b>Amount in words:</b> " + escape(amount_in_words(summary.final_value)), body),
            Spacer(1, 3 * mm),
        )
    )

    if batch.notes:
        story.extend(
            (
                Paragraph("<b>Sale notes:</b> " + escape(batch.notes), body),
                Spacer(1, 2 * mm),
            )
        )
    notice = Table(
        [
            [
                Paragraph(
                    "This document is a provisional pre-invoice generated from the Setuora sale reference. "
                    "It is not a final GST tax invoice. The final statutory invoice is issued through Tally.",
                    body,
                )
            ]
        ],
        colWidths=[180 * mm],
    )
    notice.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), warning_fill),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d4aa4a")),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]
        )
    )
    story.extend(
        (
            notice,
            Spacer(1, 8 * mm),
            Table(
                [
                    [
                        Paragraph("Customer acknowledgement", small),
                        Paragraph(f"For {company_name}", small),
                    ],
                    ["", ""],
                    [Paragraph("Signature", label), Paragraph("Authorised Signatory", label)],
                ],
                colWidths=[90 * mm, 90 * mm],
                rowHeights=[6 * mm, 12 * mm, 6 * mm],
                style=[
                    ("BOX", (0, 0), (-1, -1), 0.5, line),
                    ("INNERGRID", (0, 0), (-1, -1), 0.4, line),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ],
            ),
        )
    )

    document.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return stream.getvalue()
