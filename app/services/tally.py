from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Batch, BatchStatus, BatchType, GstRegistrationType, SyncAttempt, utc_now
from app.services.inventory import update_batch_transaction_references
from app.services.settings import (
    get_all_settings,
    gst_rate_key,
    is_tally_enabled,
    parse_sales_gst_ledger_mappings,
)
from app.services.voucher import calculate_voucher_summary


TALLY_XML_SUPPORTED_BATCH_TYPES = {
    BatchType.PURCHASE.value,
    BatchType.RECEIVE.value,
    BatchType.SALE.value,
    BatchType.SALES_RETURN.value,
}
SYNC_LEASE_MINUTES = 10
REQUIRED_TALLY_SETTING_KEYS = {
    "company_name": "company name",
    "round_off_ledger_name": "round off ledger",
}
DEFAULT_SALES_VOUCHER_TYPE = "Sales"
DEFAULT_PURCHASE_VOUCHER_TYPE = "Purchase"
DEFAULT_SALES_RETURN_VOUCHER_TYPE = "Credit Note"


class TallySyncError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True, request_xml: str | None = None, response_xml: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.request_xml = request_xml
        self.response_xml = response_xml


@dataclass
class TallyResult:
    request_xml: str
    response_xml: str
    reference: str


def missing_tally_settings(settings: dict[str, str]) -> list[str]:
    return [label for key, label in REQUIRED_TALLY_SETTING_KEYS.items() if not settings.get(key, "").strip()]


def require_tally_settings(settings: dict[str, str]) -> None:
    missing = missing_tally_settings(settings)
    if missing:
        raise TallySyncError(f"Complete Tally settings before generating XML: {', '.join(missing)}", retryable=False)
    try:
        parse_sales_gst_ledger_mappings(settings.get("sales_gst_ledger_mappings"))
    except ValueError as exc:
        raise TallySyncError(str(exc), retryable=False) from exc


def _voucher_type(settings: dict[str, str], batch_type: BatchType) -> str:
    if batch_type == BatchType.SALE:
        return settings.get("sales_voucher_type", "").strip() or DEFAULT_SALES_VOUCHER_TYPE
    if batch_type == BatchType.SALES_RETURN:
        return settings.get("sales_return_voucher_type", "").strip() or DEFAULT_SALES_RETURN_VOUCHER_TYPE
    return settings.get("purchase_voucher_type", "").strip() or DEFAULT_PURCHASE_VOUCHER_TYPE


def _required_value(settings: dict[str, str], key: str, label: str) -> str:
    value = settings.get(key, "").strip()
    if not value:
        raise TallySyncError(f"Complete Tally settings before generating XML: {label}", retryable=False)
    return value


def _require_sales_gst_mappings_for_batch(
    sales_gst_mappings: dict[str, dict[str, str]],
    lines,
) -> None:
    missing_rates = sorted(
        {gst_rate_key(line.gst_rate) for line in lines if gst_rate_key(line.gst_rate) not in sales_gst_mappings},
        key=lambda value: Decimal(value),
    )
    if missing_rates:
        rates = ", ".join(f"{rate}%" for rate in missing_rates)
        raise TallySyncError(
            f"Add product GST ledger mappings for these GST rates before generating XML: {rates}.",
            retryable=False,
        )


def _purchase_ledgers(settings: dict[str, str], summary) -> dict[str, str]:
    missing: list[str] = []
    ledgers = {
        "purchase": settings.get("purchase_ledger_name", "").strip(),
        "cgst": settings.get("cgst_ledger_name", "").strip(),
        "sgst": settings.get("sgst_ledger_name", "").strip(),
    }
    if not ledgers["purchase"]:
        missing.append("purchase ledger")
    if summary.cgst_amount > 0 and not ledgers["cgst"]:
        missing.append("CGST ledger")
    if summary.sgst_amount > 0 and not ledgers["sgst"]:
        missing.append("SGST ledger")
    if missing:
        raise TallySyncError(
            f"Complete purchase Tally settings before generating XML: {', '.join(missing)}.",
            retryable=False,
        )
    return ledgers


def _text(parent: ET.Element, tag: str, value: object | None) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = "" if value is None else str(value)
    return child


def _money(value: float | int | Decimal) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# Tally voucher dates follow the local business day.
_IST = timezone(timedelta(hours=5, minutes=30))


def _voucher_date(batch: Batch) -> str:
    moment = batch.submitted_at or batch.created_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_IST).strftime("%Y%m%d")


def tally_remote_id(batch: Batch, settings: dict[str, str]) -> str:
    if batch.sync_remote_id:
        return batch.sync_remote_id
    company = settings.get("company_name", "").strip().casefold()
    return str(uuid5(NAMESPACE_URL, f"setuora:tally:{company}:{batch.batch_number}"))


def build_voucher_xml(batch: Batch, settings: dict[str, str]) -> str:
    require_tally_settings(settings)
    batch_type = BatchType(batch.batch_type)
    if batch.batch_type not in TALLY_XML_SUPPORTED_BATCH_TYPES:
        raise TallySyncError(f"Tally XML is not configured for {batch.batch_type}", retryable=False)
    is_sale = batch_type == BatchType.SALE
    is_sales_side = batch_type in {BatchType.SALE, BatchType.SALES_RETURN}
    voucher_type = _voucher_type(settings, batch_type)
    party_name = (batch.party_name or "").strip()
    if not party_name:
        raise TallySyncError(
            "Add a customer or supplier to this batch before generating Tally XML.",
            retryable=False,
        )
    sales_gst_mappings = parse_sales_gst_ledger_mappings(settings.get("sales_gst_ledger_mappings"))

    def sales_ledgers(gst_rate: Decimal) -> dict[str, str]:
        key = gst_rate_key(gst_rate)
        if key not in sales_gst_mappings:
            raise TallySyncError(
                f"Add a product GST ledger mapping for {key}% before generating Tally XML.",
                retryable=False,
            )
        return sales_gst_mappings[key]

    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    _text(header, "TALLYREQUEST", "Import Data")
    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    request_desc = ET.SubElement(import_data, "REQUESTDESC")
    _text(request_desc, "REPORTNAME", "Vouchers")
    static_variables = ET.SubElement(request_desc, "STATICVARIABLES")
    _text(static_variables, "SVCURRENTCOMPANY", settings["company_name"])
    request_data = ET.SubElement(import_data, "REQUESTDATA")
    message = ET.SubElement(request_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
    voucher = ET.SubElement(
        message,
        "VOUCHER",
        {
            "REMOTEID": tally_remote_id(batch, settings),
            "VCHTYPE": voucher_type,
            "ACTION": "Create",
            "OBJVIEW": "Accounting Voucher View",
        },
    )
    _text(voucher, "DATE", _voucher_date(batch))
    _text(voucher, "VOUCHERTYPENAME", voucher_type)
    _text(voucher, "VOUCHERNUMBER", batch.batch_number)
    _text(voucher, "PARTYLEDGERNAME", party_name)
    if is_sales_side:
        gst_registration_type = (
            batch.party_gst_registration_type or GstRegistrationType.UNREGISTERED_CONSUMER.value
        ).strip()
        party_gst_name = (batch.party_gst_name or party_name).strip()
        _text(voucher, "BASICBASEPARTYNAME", party_name)
        _text(voucher, "BASICBUYERNAME", party_gst_name)
        _text(voucher, "GSTREGISTRATIONTYPE", gst_registration_type)
        if batch.party_gstin:
            _text(voucher, "PARTYGSTIN", batch.party_gstin)
    if is_sale and batch.party_state:
        _text(voucher, "STATENAME", batch.party_state)
        _text(voucher, "PLACEOFSUPPLY", batch.party_state)
        _text(voucher, "COUNTRYOFRESIDENCE", "India")
    _text(voucher, "PERSISTEDVIEW", "Accounting Voucher View")
    _text(voucher, "NARRATION", f"Setuora barcode batch {batch.batch_number}")

    summary = calculate_voucher_summary(batch)
    if is_sales_side:
        _require_sales_gst_mappings_for_batch(sales_gst_mappings, summary.lines)
    else:
        purchase_ledgers = _purchase_ledgers(settings, summary)

    # Tally uses negative amounts for debits and positive amounts for credits.
    income_is_credit = is_sale

    def add_ledger(parent: ET.Element, tag: str, name: str, amount: Decimal, credit: bool) -> None:
        entry = ET.SubElement(parent, tag)
        _text(entry, "LEDGERNAME", name)
        _text(entry, "ISDEEMEDPOSITIVE", "No" if credit else "Yes")
        _text(entry, "AMOUNT", _money(amount if credit else -amount))

    for line in summary.lines:
        inventory = ET.SubElement(voucher, "ALLINVENTORYENTRIES.LIST")
        _text(inventory, "STOCKITEMNAME", line.tally_stock_item_name)
        _text(inventory, "ISDEEMEDPOSITIVE", "No" if income_is_credit else "Yes")
        _text(inventory, "RATE", f"{_money(line.rate)}/{line.unit}")
        if is_sale and line.discount_rate > 0:
            _text(inventory, "DISCOUNT", _money(line.discount_rate))
        signed_line = line.taxable_value if income_is_credit else -line.taxable_value
        _text(inventory, "AMOUNT", _money(signed_line))
        _text(inventory, "ACTUALQTY", f"{line.quantity} {line.unit}")
        _text(inventory, "BILLEDQTY", f"{line.quantity} {line.unit}")
        allocations = ET.SubElement(inventory, "ACCOUNTINGALLOCATIONS.LIST")
        line_income_ledger = (
            sales_ledgers(line.gst_rate)["sales"] if is_sales_side else purchase_ledgers["purchase"]
        )
        _text(allocations, "LEDGERNAME", line_income_ledger)
        _text(allocations, "ISDEEMEDPOSITIVE", "No" if income_is_credit else "Yes")
        _text(allocations, "AMOUNT", _money(signed_line))

    if is_sales_side:
        tax_by_rate: dict[str, dict[str, Decimal]] = {}
        for line in summary.lines:
            key = gst_rate_key(line.gst_rate)
            totals = tax_by_rate.setdefault(
                key,
                {"cgst": Decimal("0"), "sgst": Decimal("0"), "igst": Decimal("0")},
            )
            totals["cgst"] += line.cgst_amount
            totals["sgst"] += line.sgst_amount
            totals["igst"] += line.igst_amount
        for key, totals in tax_by_rate.items():
            ledgers = sales_ledgers(Decimal(key))
            if totals["cgst"] > 0:
                add_ledger(voucher, "LEDGERENTRIES.LIST", ledgers["cgst"], totals["cgst"], credit=is_sale)
            if totals["sgst"] > 0:
                add_ledger(voucher, "LEDGERENTRIES.LIST", ledgers["sgst"], totals["sgst"], credit=is_sale)
            if totals["igst"] > 0:
                if not ledgers["igst"]:
                    raise TallySyncError(
                        f"Add an IGST ledger for the {key}% product GST mapping.",
                        retryable=False,
                    )
                add_ledger(voucher, "LEDGERENTRIES.LIST", ledgers["igst"], totals["igst"], credit=is_sale)
    else:
        if summary.cgst_amount > 0:
            add_ledger(
                voucher,
                "LEDGERENTRIES.LIST",
                purchase_ledgers["cgst"],
                summary.cgst_amount,
                credit=False,
            )
        if summary.sgst_amount > 0:
            add_ledger(
                voucher,
                "LEDGERENTRIES.LIST",
                purchase_ledgers["sgst"],
                summary.sgst_amount,
                credit=False,
            )
    if summary.round_off != 0:
        add_ledger(
            voucher,
            "LEDGERENTRIES.LIST",
            _required_value(settings, "round_off_ledger_name", "round off ledger"),
            summary.round_off,
            credit=income_is_credit,
        )

    add_ledger(voucher, "LEDGERENTRIES.LIST", party_name, summary.final_value, credit=not is_sale)

    return ET.tostring(envelope, encoding="unicode")


def post_to_tally(xml: str, settings: dict[str, str]) -> TallyResult:
    host = settings.get("tally_host")
    port = settings.get("tally_port")
    if not host or not port:
        raise TallySyncError("Tally host/port is not configured", retryable=True, request_xml=xml)
    url = f"http://{host}:{port}"
    request = Request(url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml"}, method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            response_xml = response.read().decode("utf-8", errors="replace")
    except (URLError, OSError) as exc:
        raise TallySyncError("Tally connection failed", retryable=True, request_xml=xml) from exc

    try:
        root = ET.fromstring(response_xml)
    except ET.ParseError as exc:
        raise TallySyncError("Tally returned unreadable XML", retryable=False, request_xml=xml, response_xml=response_xml) from exc

    errors = [node.text for node in root.iter() if node.tag.upper().endswith("LINEERROR") and node.text]
    if errors:
        raise TallySyncError("; ".join(errors), retryable=False, request_xml=xml, response_xml=response_xml)

    created = next((node.text for node in root.iter() if node.tag.upper().endswith("CREATED")), None)
    altered = next((node.text for node in root.iter() if node.tag.upper().endswith("ALTERED")), None)

    def _as_int(value: str | None) -> int:
        try:
            return int((value or "0").strip())
        except (TypeError, ValueError):
            return 0

    # A 200 response can still mean Tally imported nothing.
    exceptions = next((node.text for node in root.iter() if node.tag.upper().endswith("EXCEPTIONS")), None)
    if _as_int(created) + _as_int(altered) < 1:
        detail = f"Tally created/altered nothing (CREATED={created or 0}, ALTERED={altered or 0}"
        detail += f", EXCEPTIONS={exceptions})" if exceptions is not None else ")"
        raise TallySyncError(detail, retryable=False, request_xml=xml, response_xml=response_xml)

    reference = f"CREATED={created or 0}; ALTERED={altered or 0}"
    return TallyResult(request_xml=xml, response_xml=response_xml, reference=reference)


# Prevent request/retry races from posting the same voucher twice.
_SYNC_LOCK = threading.Lock()


def sync_batch(db: Session, batch: Batch) -> None:
    with _SYNC_LOCK:
        current_status = db.scalar(select(Batch.status).where(Batch.id == batch.id))
        if current_status in {BatchStatus.SYNCED.value, BatchStatus.CLOSED.value}:
            return
        if current_status != batch.status:
            db.refresh(batch)
        _sync_batch_locked(db, batch)


def _sync_batch_locked(db: Session, batch: Batch) -> None:
    now = utc_now()
    stale_before = now - timedelta(minutes=SYNC_LEASE_MINUTES)
    sync_started_at = batch.sync_started_at
    if sync_started_at is not None and sync_started_at.tzinfo is None:
        sync_started_at = sync_started_at.replace(tzinfo=timezone.utc)
    if (
        batch.status == BatchStatus.SYNCING.value
        and sync_started_at is not None
        and sync_started_at > stale_before
    ):
        return

    is_retry = batch.status in {
        BatchStatus.PENDING_SYNC.value,
        BatchStatus.FAILED.value,
        BatchStatus.SYNCING.value,
    }
    if is_retry:
        batch.retry_count = (batch.retry_count or 0) + 1
        batch.last_retry_at = now
    if BatchType(batch.batch_type) in {BatchType.AUDIT, BatchType.QR_ASSIGNMENT}:
        batch.status = BatchStatus.CLOSED.value
        batch.synced_at = now
        db.commit()
        return
    settings = get_all_settings(db)
    if batch.batch_type not in TALLY_XML_SUPPORTED_BATCH_TYPES:
        batch.status = BatchStatus.PENDING_SYNC.value
        batch.last_error = f"Tally XML is not configured for {batch.batch_type}"
        db.add(SyncAttempt(batch_id=batch.id, status=BatchStatus.PENDING_SYNC.value, error=batch.last_error))
        db.commit()
        return
    batch.sync_remote_id = tally_remote_id(batch, settings)
    try:
        xml = batch.sync_request_xml or build_voucher_xml(batch, settings)
    except TallySyncError as exc:
        batch.status = BatchStatus.PENDING_SYNC.value
        batch.last_error = str(exc)
        db.add(SyncAttempt(batch_id=batch.id, status=BatchStatus.PENDING_SYNC.value, error=batch.last_error))
        db.commit()
        return
    if not is_tally_enabled(db):
        batch.status = BatchStatus.PENDING_SYNC.value
        batch.sync_request_xml = xml
        batch.last_error = "Tally sync is disabled in settings"
        db.add(SyncAttempt(batch_id=batch.id, status=BatchStatus.PENDING_SYNC.value, request_xml=xml, error=batch.last_error))
        db.commit()
        return

    claim_status = batch.status
    claim_query = update(Batch).where(Batch.id == batch.id, Batch.status == claim_status)
    if claim_status == BatchStatus.SYNCING.value:
        if batch.sync_started_at is None:
            claim_query = claim_query.where(Batch.sync_started_at.is_(None))
        else:
            claim_query = claim_query.where(Batch.sync_started_at == batch.sync_started_at)
    claim = db.execute(
        claim_query
        .values(
            status=BatchStatus.SYNCING.value,
            sync_remote_id=batch.sync_remote_id,
            sync_request_xml=xml,
            sync_started_at=now,
            retry_count=batch.retry_count,
            last_retry_at=batch.last_retry_at,
            last_error=None,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if claim != 1:
        db.rollback()
        return
    attempt = SyncAttempt(
        batch_id=batch.id,
        status=BatchStatus.SYNCING.value,
        request_xml=xml,
    )
    db.add(attempt)
    db.commit()
    db.refresh(batch)

    try:
        result = post_to_tally(xml, settings)
    except TallySyncError as exc:
        batch.status = BatchStatus.PENDING_SYNC.value if exc.retryable else BatchStatus.FAILED.value
        batch.last_error = str(exc)
        batch.sync_started_at = None
        attempt.status = batch.status
        attempt.request_xml = exc.request_xml or xml
        attempt.response_xml = exc.response_xml
        attempt.error = str(exc)
        db.commit()
        return
    batch.status = BatchStatus.SYNCED.value
    batch.tally_reference = result.reference
    batch.last_error = None
    batch.synced_at = utc_now()
    batch.sync_started_at = None
    update_batch_transaction_references(db, batch)
    attempt.status = BatchStatus.SYNCED.value
    attempt.request_xml = result.request_xml
    attempt.response_xml = result.response_xml
    db.commit()
