from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Product, TallyMasterConfirmation, User, utc_now
from app.services.settings import get_all_settings, parse_sales_gst_ledger_mappings


@dataclass(frozen=True)
class MasterRequirement:
    master_type: str
    master_name: str
    source: str
    detail: str
    local_status: str

    @property
    def key(self) -> str:
        return f"{self.master_type}|{self.master_name}"


@dataclass(frozen=True)
class GatewayCheckResult:
    ok: bool
    message: str
    response_excerpt: str = ""


class TallyDataError(RuntimeError):
    """Raised when read-only data discovery from Tally cannot be completed."""


@dataclass(frozen=True)
class TallyLedger:
    name: str
    parent: str = ""
    closing_balance: str = ""


@dataclass(frozen=True)
class TallySalesVoucher:
    date: str
    voucher_number: str
    voucher_type: str
    party_ledger: str
    amount: str
    narration: str = ""
    remote_id: str = ""
    tally_user: str = ""


def _status(name: str | None) -> str:
    return "READY" if name and name.strip() else "MISSING"


def _add(requirements: dict[tuple[str, str], MasterRequirement], master_type: str, name: str, source: str, detail: str) -> None:
    clean = (name or "").strip()
    key = (master_type, clean)
    if key in requirements:
        existing = requirements[key]
        requirements[key] = MasterRequirement(
            master_type=existing.master_type,
            master_name=existing.master_name,
            source=f"{existing.source}; {source}",
            detail=existing.detail,
            local_status=existing.local_status,
        )
        return
    requirements[key] = MasterRequirement(master_type, clean, source, detail, _status(clean))


def collect_master_requirements(db: Session) -> list[MasterRequirement]:
    settings = get_all_settings(db)
    requirements: dict[tuple[str, str], MasterRequirement] = {}

    _add(requirements, "Company", settings["company_name"], "Settings", "Must be the open Tally company")
    _add(requirements, "Ledger", settings["round_off_ledger_name"], "Settings", "Round off posting ledger")
    mappings = parse_sales_gst_ledger_mappings(settings.get("sales_gst_ledger_mappings"))
    for gst_rate, ledgers in mappings.items():
        source = f"Sales GST {gst_rate}% mapping"
        _add(requirements, "Ledger", ledgers["sales"], source, "Sales posting ledger")
        _add(requirements, "Ledger", ledgers["cgst"], source, "CGST posting ledger")
        _add(requirements, "Ledger", ledgers["sgst"], source, "SGST posting ledger")
        if ledgers["igst"]:
            _add(requirements, "Ledger", ledgers["igst"], source, "IGST posting ledger")

    products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.product_code)).all()
    for product in products:
        source = f"Product {product.product_code}"
        _add(requirements, "Stock Item", product.tally_stock_item_name, source, product.product_name)
        _add(requirements, "Unit", product.unit, source, "Product unit of measure")
        _add(requirements, "HSN", product.hsn, source, f"GST rate {product.gst_rate}%")

    return sorted(requirements.values(), key=lambda item: (item.master_type, item.master_name))


def confirmation_lookup(db: Session) -> dict[str, TallyMasterConfirmation]:
    rows = db.scalars(select(TallyMasterConfirmation).options(selectinload(TallyMasterConfirmation.confirmed_by))).all()
    return {f"{row.master_type}|{row.master_name}": row for row in rows}


def confirm_master(db: Session, user: User, master_type: str, master_name: str, source: str, notes: str = "") -> None:
    clean_type = master_type.strip()
    clean_name = master_name.strip()
    row = db.scalar(
        select(TallyMasterConfirmation).where(
            TallyMasterConfirmation.master_type == clean_type,
            TallyMasterConfirmation.master_name == clean_name,
        )
    )
    if row:
        row.source = source.strip()
        row.notes = notes.strip() or None
        row.confirmed_by_id = user.id
        row.confirmed_at = utc_now()
    else:
        db.add(
            TallyMasterConfirmation(
                master_type=clean_type,
                master_name=clean_name,
                source=source.strip(),
                notes=notes.strip() or None,
                confirmed_by_id=user.id,
            )
        )
    db.commit()


def remove_confirmation(db: Session, master_type: str, master_name: str) -> None:
    row = db.scalar(
        select(TallyMasterConfirmation).where(
            TallyMasterConfirmation.master_type == master_type.strip(),
            TallyMasterConfirmation.master_name == master_name.strip(),
        )
    )
    if row:
        db.delete(row)
        db.commit()


def readiness_counts(requirements: list[MasterRequirement], confirmations: dict[str, TallyMasterConfirmation]) -> dict[str, int]:
    missing = sum(1 for item in requirements if item.local_status == "MISSING")
    confirmed = sum(1 for item in requirements if item.local_status == "READY" and item.key in confirmations)
    ready = sum(1 for item in requirements if item.local_status == "READY")
    return {
        "total": len(requirements),
        "ready": ready,
        "missing": missing,
        "confirmed": confirmed,
        "unchecked": max(ready - confirmed, 0),
    }


def live_sync_readiness(db: Session) -> tuple[bool, dict[str, int]]:
    requirements = collect_master_requirements(db)
    confirmations = confirmation_lookup(db)
    counts = readiness_counts(requirements, confirmations)
    return counts["missing"] == 0 and counts["unchecked"] == 0 and counts["total"] > 0, counts


def build_company_list_xml() -> str:
    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "VERSION").text = "1"
    ET.SubElement(header, "TALLYREQUEST").text = "Export"
    ET.SubElement(header, "TYPE").text = "Collection"
    ET.SubElement(header, "ID").text = "List of Companies"
    body = ET.SubElement(envelope, "BODY")
    desc = ET.SubElement(body, "DESC")
    static_variables = ET.SubElement(desc, "STATICVARIABLES")
    ET.SubElement(static_variables, "SVEXPORTFORMAT").text = "$$SysName:XML"
    tdl = ET.SubElement(desc, "TDL")
    tdl_message = ET.SubElement(tdl, "TDLMESSAGE")
    collection = ET.SubElement(tdl_message, "COLLECTION", {"NAME": "List of Companies"})
    ET.SubElement(collection, "TYPE").text = "Company"
    ET.SubElement(collection, "NATIVEMETHOD").text = "Name"
    return ET.tostring(envelope, encoding="unicode")


def _build_collection_export(
    collection_name: str,
    object_type: str,
    methods: tuple[str, ...],
    *,
    company_name: str = "",
) -> tuple[ET.Element, ET.Element]:
    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "VERSION").text = "1"
    ET.SubElement(header, "TALLYREQUEST").text = "Export"
    ET.SubElement(header, "TYPE").text = "Collection"
    ET.SubElement(header, "ID").text = collection_name
    body = ET.SubElement(envelope, "BODY")
    desc = ET.SubElement(body, "DESC")
    static_variables = ET.SubElement(desc, "STATICVARIABLES")
    ET.SubElement(static_variables, "SVEXPORTFORMAT").text = "$$SysName:XML"
    if company_name:
        ET.SubElement(static_variables, "SVCURRENTCOMPANY").text = company_name
    tdl = ET.SubElement(desc, "TDL")
    tdl_message = ET.SubElement(tdl, "TDLMESSAGE")
    collection = ET.SubElement(tdl_message, "COLLECTION", {"NAME": collection_name})
    ET.SubElement(collection, "TYPE").text = object_type
    for method in methods:
        ET.SubElement(collection, "NATIVEMETHOD").text = method
    return envelope, tdl_message


def build_ledger_list_xml(company_name: str) -> str:
    envelope, _ = _build_collection_export(
        "Setuora Ledger List",
        "Ledger",
        ("Name", "Parent", "ClosingBalance"),
        company_name=company_name.strip(),
    )
    return ET.tostring(envelope, encoding="unicode")


def build_sales_book_xml(company_name: str, from_date: date, to_date: date) -> str:
    envelope, tdl_message = _build_collection_export(
        "Setuora Sales Book",
        "Voucher",
        (
            "Date",
            "VoucherNumber",
            "VoucherTypeName",
            "PartyLedgerName",
            "Amount",
            "Narration",
            "GUID",
            "MasterID",
            "EnteredBy",
            "CreatedBy",
            "AlteredBy",
            "AllLedgerEntries",
        ),
        company_name=company_name.strip(),
    )
    static_variables = envelope.find("./BODY/DESC/STATICVARIABLES")
    if static_variables is None:  # pragma: no cover - constructed immediately above
        raise RuntimeError("Tally request is missing static variables")
    ET.SubElement(static_variables, "SVFROMDATE", {"TYPE": "Date"}).text = from_date.strftime("%Y%m%d")
    ET.SubElement(static_variables, "SVTODATE", {"TYPE": "Date"}).text = to_date.strftime("%Y%m%d")
    collection = next(node for node in tdl_message if _local_tag(node) == "COLLECTION")
    ET.SubElement(collection, "FILTER").text = "SetuoraSalesVoucherFilter"
    formula = ET.SubElement(
        tdl_message,
        "SYSTEM",
        {"TYPE": "Formulae", "NAME": "SetuoraSalesVoucherFilter"},
    )
    formula.text = (
        "$$IsSales:$VoucherTypeName "
        "AND $Date >= ##SVFromDate AND $Date <= ##SVToDate"
    )
    return ET.tostring(envelope, encoding="unicode")


def _local_tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].upper()


def _direct_text(node: ET.Element, *names: str) -> str:
    expected = {name.upper() for name in names}
    for child in node:
        if _local_tag(child) in expected and child.text:
            return child.text.strip()
    return ""


def _first_text(node: ET.Element, *names: str) -> str:
    expected = {name.upper() for name in names}
    for child in node.iter():
        if _local_tag(child) in expected and child.text:
            return child.text.strip()
    return ""


def _response_errors(root: ET.Element) -> list[str]:
    return [
        node.text.strip()
        for node in root.iter()
        if _local_tag(node) in {"LINEERROR", "ERROR"} and node.text and node.text.strip()
    ]


_NUMERIC_CHARACTER_REFERENCE = re.compile(r"&#(?:x([0-9a-fA-F]+)|([0-9]+));")


def _valid_xml_character(codepoint: int) -> bool:
    return (
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _sanitize_tally_xml(body: str) -> str:
    """Remove XML 1.0 characters that Tally emits as invalid empty-value markers."""

    def replace_reference(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16) if match.group(1) else int(match.group(2))
        return match.group(0) if _valid_xml_character(codepoint) else ""

    without_invalid_references = _NUMERIC_CHARACTER_REFERENCE.sub(replace_reference, body)
    return "".join(
        character
        for character in without_invalid_references
        if _valid_xml_character(ord(character))
    )


def _post_read_request(settings: dict[str, str], xml: str) -> tuple[str, ET.Element]:
    host = settings.get("tally_host", "").strip()
    port = settings.get("tally_port", "").strip()
    if not host or not port:
        raise TallyDataError("Tally host and port are not configured.")
    url = f"http://{host}:{port}"
    request = Request(url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml"}, method="POST")
    try:
        with urlopen(request, timeout=8) as response:
            body = response.read().decode("utf-8", errors="replace")
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise TallyDataError(f"Tally gateway did not respond: {reason}") from exc
    except TimeoutError as exc:
        raise TallyDataError("Tally gateway timed out.") from exc
    except OSError as exc:
        raise TallyDataError(f"Tally gateway did not respond: {exc}") from exc
    if not body.strip():
        raise TallyDataError("Tally gateway returned an empty response.")
    sanitized_body = _sanitize_tally_xml(body)
    try:
        root = ET.fromstring(sanitized_body)
    except ET.ParseError as exc:
        raise TallyDataError("Tally gateway returned unreadable XML.") from exc
    errors = _response_errors(root)
    if errors:
        raise TallyDataError(f"Tally rejected the request: {'; '.join(errors)}")
    status = _first_text(root, "STATUS")
    if status == "0":
        raise TallyDataError("Tally rejected the request.")
    return sanitized_body, root


def fetch_tally_companies(settings: dict[str, str]) -> list[str]:
    _, root = _post_read_request(settings, build_company_list_xml())
    names: list[str] = []
    seen: set[str] = set()
    for node in root.iter():
        if _local_tag(node) != "COMPANY":
            continue
        name = _direct_text(node, "NAME") or (node.attrib.get("NAME") or "").strip()
        if name and name.casefold() not in seen:
            names.append(name)
            seen.add(name.casefold())
    return sorted(names, key=str.casefold)


def fetch_tally_ledgers(settings: dict[str, str], company_name: str) -> list[TallyLedger]:
    clean_company = company_name.strip()
    if not clean_company:
        raise TallyDataError("Choose a Tally company before loading ledgers.")
    _, root = _post_read_request(settings, build_ledger_list_xml(clean_company))
    ledgers: list[TallyLedger] = []
    seen: set[str] = set()
    for node in root.iter():
        if _local_tag(node) != "LEDGER":
            continue
        name = _direct_text(node, "NAME") or (node.attrib.get("NAME") or "").strip()
        if not name or name.casefold() in seen:
            continue
        ledgers.append(
            TallyLedger(
                name=name,
                parent=_direct_text(node, "PARENT"),
                closing_balance=_direct_text(node, "CLOSINGBALANCE"),
            )
        )
        seen.add(name.casefold())
    return sorted(ledgers, key=lambda ledger: ledger.name.casefold())


def _party_ledger(voucher: ET.Element) -> str:
    direct = _direct_text(voucher, "PARTYLEDGERNAME", "LEDGERNAME")
    if direct:
        return direct
    for entry in voucher:
        if _local_tag(entry) not in {"ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"}:
            continue
        if _direct_text(entry, "ISPARTYLEDGER").casefold() == "yes":
            return _direct_text(entry, "LEDGERNAME")
    return ""


def _voucher_amount(voucher: ET.Element) -> str:
    direct = _direct_text(voucher, "AMOUNT")
    if direct:
        return direct
    for entry in voucher:
        if _local_tag(entry) not in {"ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"}:
            continue
        if _direct_text(entry, "ISPARTYLEDGER").casefold() == "yes":
            return _direct_text(entry, "AMOUNT")
    return ""


def _display_tally_date(raw: str) -> str:
    clean = raw.strip()
    if len(clean) == 8 and clean.isdigit():
        try:
            return datetime.strptime(clean, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return clean


def fetch_tally_sales_book(
    settings: dict[str, str],
    company_name: str,
    from_date: date,
    to_date: date,
) -> list[TallySalesVoucher]:
    clean_company = company_name.strip()
    if not clean_company:
        raise TallyDataError("Choose a Tally company before loading the sales book.")
    if from_date > to_date:
        raise TallyDataError("Sales book start date must be on or before the end date.")
    _, root = _post_read_request(
        settings,
        build_sales_book_xml(clean_company, from_date, to_date),
    )
    vouchers: list[TallySalesVoucher] = []
    for node in root.iter():
        if _local_tag(node) != "VOUCHER":
            continue
        voucher = TallySalesVoucher(
            date=_display_tally_date(_direct_text(node, "DATE")),
            voucher_number=_direct_text(node, "VOUCHERNUMBER"),
            voucher_type=_direct_text(node, "VOUCHERTYPENAME"),
            party_ledger=_party_ledger(node),
            amount=_voucher_amount(node),
            narration=_direct_text(node, "NARRATION"),
            remote_id=_direct_text(node, "GUID", "MASTERID"),
            tally_user=_direct_text(node, "ENTEREDBY", "CREATEDBY", "ALTEREDBY"),
        )
        if voucher.date:
            vouchers.append(voucher)
    return sorted(
        vouchers,
        key=lambda voucher: (voucher.date, voucher.voucher_number),
        reverse=True,
    )


def test_tally_gateway(settings: dict[str, str]) -> GatewayCheckResult:
    xml = build_company_list_xml()
    url = f"http://{settings['tally_host']}:{settings['tally_port']}"
    request = Request(url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml"}, method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
    except URLError as exc:
        return GatewayCheckResult(False, f"Tally gateway did not respond: {exc.reason}")
    except TimeoutError:
        return GatewayCheckResult(False, "Tally gateway timed out")
    excerpt = " ".join(body.split())[:500]
    if not body.strip():
        return GatewayCheckResult(False, "Tally gateway returned an empty response")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return GatewayCheckResult(False, "Tally gateway returned unreadable XML", excerpt)

    errors = [
        node.text.strip()
        for node in root.iter()
        if node.tag.upper().endswith("LINEERROR") and node.text and node.text.strip()
    ]
    if errors:
        return GatewayCheckResult(False, f"Tally rejected gateway check: {'; '.join(errors)}", excerpt)

    status = next(
        (node.text.strip() for node in root.iter() if node.tag.upper().endswith("STATUS") and node.text),
        None,
    )
    if status == "0":
        return GatewayCheckResult(False, "Tally rejected gateway check", excerpt)
    return GatewayCheckResult(True, "Tally gateway responded", excerpt)
