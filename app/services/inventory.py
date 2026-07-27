from collections import defaultdict
from datetime import date, datetime, timezone
import re

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Batch,
    BatchItem,
    BatchStatus,
    BatchType,
    GstRegistrationType,
    GstTreatment,
    InventoryTransaction,
    Product,
    ScanLog,
    Serial,
    SerialStatus,
    TransactionType,
    User,
    WarehouseLevel,
)
from app.services.expiry import validate_fefo_scan


class InventoryError(ValueError):
    pass


DEFAULT_UNREGISTERED_SALE_STATE = "Karnataka"
REGISTERED_GST_REGISTRATION_TYPES = {
    GstRegistrationType.COMPOSITION.value,
    GstRegistrationType.REGULAR.value,
}


def normalize_serial(serial_number: str) -> str:
    return serial_number.strip().upper()


def gst_registration_requires_gstin(value: str | None) -> bool:
    return (value or "").strip() in REGISTERED_GST_REGISTRATION_TYPES


def normalize_gstin(value: str | None) -> str | None:
    raw = re.sub(r"\s+", "", value or "").upper()
    if not raw:
        return None
    if not re.fullmatch(r"[0-9A-Z]{15}", raw):
        raise InventoryError("GST number must be a 15-character GSTIN.")
    return raw


def normalize_gst_registration_type(value: str | None, batch_type: BatchType) -> str | None:
    raw = value.strip() if value else ""
    if not raw and batch_type == BatchType.SALE:
        raw = GstRegistrationType.UNREGISTERED_CONSUMER.value
    valid_types = {registration_type.value for registration_type in GstRegistrationType}
    if raw and raw not in valid_types:
        raise InventoryError("Choose a valid GST registration type.")
    return raw or None


def next_batch_number(db: Session, batch_type: BatchType) -> str:
    prefix = {
        BatchType.PURCHASE: "PUR",
        BatchType.RECEIVE: "RCV",
        BatchType.SALE: "SAL",
        BatchType.AUDIT: "AUD",
        BatchType.PURCHASE_RETURN: "PRT",
        BatchType.SALES_RETURN: "SRT",
        BatchType.ISSUE: "ISS",
        BatchType.QR_ASSIGNMENT: "ASN",
    }[batch_type]
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    count = db.scalar(select(func.count(Batch.id)).where(Batch.batch_number.like(f"{prefix}-{today}-%"))) or 0
    return f"{prefix}-{today}-{count + 1:04d}"


def create_batch(
    db: Session,
    user: User,
    batch_type: BatchType,
    party_name: str | None,
    notes: str | None,
    reason_code: str | None = None,
    *,
    party_state: str | None = None,
    party_gst_registration_type: str | None = None,
    party_gst_name: str | None = None,
    party_gstin: str | None = None,
    gst_treatment: str | None = None,
    gst_cgst_rate: float | None = None,
    gst_sgst_rate: float | None = None,
    gst_igst_rate: float | None = None,
    audit_assignment_id: int | None = None,
    commit: bool = True,
) -> Batch:
    treatment = gst_treatment.strip().upper() if gst_treatment else None
    if treatment and treatment not in {GstTreatment.INTRA_STATE.value, GstTreatment.INTER_STATE.value}:
        raise InventoryError("Choose either CGST + SGST or IGST for this sale")
    registration_type = normalize_gst_registration_type(party_gst_registration_type, batch_type)
    normalized_party_state = party_state.strip() if party_state else None
    if (
        batch_type == BatchType.SALE
        and registration_type == GstRegistrationType.UNREGISTERED_CONSUMER.value
        and not normalized_party_state
    ):
        normalized_party_state = DEFAULT_UNREGISTERED_SALE_STATE
    gst_name = party_gst_name.strip() if party_gst_name else None
    if gst_registration_requires_gstin(registration_type):
        gstin = normalize_gstin(party_gstin)
    else:
        gstin = None
    for attempt in range(5):
        batch = Batch(
            batch_number=next_batch_number(db, batch_type),
            batch_type=batch_type.value,
            party_name=party_name.strip() if party_name else None,
            party_state=normalized_party_state,
            party_gst_registration_type=registration_type,
            party_gst_name=gst_name,
            party_gstin=gstin,
            gst_treatment=treatment,
            gst_cgst_rate=gst_cgst_rate,
            gst_sgst_rate=gst_sgst_rate,
            gst_igst_rate=gst_igst_rate,
            reason_code=reason_code.strip().upper() if reason_code else None,
            user_id=user.id,
            audit_assignment_id=audit_assignment_id,
            notes=notes.strip() if notes else None,
        )
        db.add(batch)
        try:
            if commit:
                db.commit()
            else:
                db.flush()
        except IntegrityError as exc:
            db.rollback()
            if not commit or attempt == 4:
                raise InventoryError("Could not allocate a unique batch number; try again") from exc
            continue
        db.refresh(batch)
        return batch
    raise InventoryError("Could not allocate a unique batch number; try again")


def transaction_type_for_batch(batch_type: BatchType) -> TransactionType:
    if batch_type in {BatchType.PURCHASE, BatchType.RECEIVE}:
        return TransactionType.PURCHASE
    if batch_type == BatchType.SALE:
        return TransactionType.SALE
    if batch_type == BatchType.SALES_RETURN:
        return TransactionType.SALES_RETURN
    if batch_type == BatchType.PURCHASE_RETURN:
        return TransactionType.PURCHASE_RETURN
    if batch_type == BatchType.ISSUE:
        return TransactionType.ISSUE
    if batch_type == BatchType.AUDIT:
        return TransactionType.AUDIT
    if batch_type == BatchType.QR_ASSIGNMENT:
        return TransactionType.QR_ASSIGNMENT
    raise InventoryError(f"{batch_type.value} is not a supported transaction type")


def log_inventory_transaction(
    db: Session,
    user: User,
    transaction_type: TransactionType,
    serial: Serial | None = None,
    product: Product | None = None,
    batch: Batch | None = None,
    status_from: str | None = None,
    status_to: str | None = None,
    reason_code: str | None = None,
    tally_reference: str | None = None,
    reference_number: str | None = None,
    notes: str | None = None,
) -> InventoryTransaction:
    row = InventoryTransaction(
        transaction_type=transaction_type.value,
        serial_id=serial.id if serial else None,
        product_id=(product.id if product else serial.product_id if serial else None),
        batch_id=batch.id if batch else None,
        user_id=user.id,
        serial_number=serial.serial_number if serial else None,
        status_from=status_from,
        status_to=status_to,
        reason_code=reason_code.strip().upper() if reason_code else None,
        tally_reference=tally_reference,
        reference_number=reference_number or (batch.batch_number if batch else None),
        notes=notes.strip() if notes else None,
    )
    db.add(row)
    return row


def serial_allowed_for_batch(serial: Serial, batch_type: BatchType) -> None:
    status = SerialStatus(serial.status)
    if not serial.active or status in {SerialStatus.REPLACED, SerialStatus.INVALID}:
        raise InventoryError(f"{serial.serial_number} is inactive")
    if status == SerialStatus.GENERATED and batch_type not in {
        BatchType.PURCHASE,
        BatchType.RECEIVE,
        BatchType.QR_ASSIGNMENT,
    }:
        raise InventoryError(
            f"{serial.serial_number} is unassigned. "
            "Complete its purchase before using this QR in a stock transaction."
        )
    if batch_type in {BatchType.PURCHASE, BatchType.RECEIVE}:
        if status == SerialStatus.IN_STOCK:
            raise InventoryError(
                f"{serial.serial_number} is already in stock. "
                "For purchase, scan a QR label that starts as Generated."
            )
        if status not in {SerialStatus.GENERATED, SerialStatus.PURCHASE_RETURN}:
            raise InventoryError(f"{serial.serial_number} cannot be purchased from {serial.status}")
    elif batch_type == BatchType.SALE:
        if status not in {SerialStatus.IN_STOCK, SerialStatus.RETURNED}:
            raise InventoryError(f"{serial.serial_number} is not available for sale")
    elif batch_type == BatchType.AUDIT:
        return
    elif batch_type == BatchType.SALES_RETURN:
        if status != SerialStatus.SOLD:
            raise InventoryError(f"{serial.serial_number} is not sold")
    elif batch_type == BatchType.PURCHASE_RETURN:
        if status not in {SerialStatus.IN_STOCK, SerialStatus.RETURNED}:
            raise InventoryError(f"{serial.serial_number} is not available for purchase return")
    elif batch_type == BatchType.ISSUE:
        if status != SerialStatus.IN_STOCK:
            raise InventoryError(f"{serial.serial_number} is not available for issue")
    elif batch_type == BatchType.QR_ASSIGNMENT:
        if status != SerialStatus.GENERATED:
            raise InventoryError(f"{serial.serial_number} is already assigned")
    else:
        raise InventoryError(f"{batch_type.value} is not supported")


def add_serial_to_batch(db: Session, batch: Batch, user: User, serial_number: str) -> BatchItem:
    if batch.status != BatchStatus.DRAFT.value:
        raise InventoryError("This batch is already submitted")
    serial_number = normalize_serial(serial_number)
    serial = db.scalar(select(Serial).where(Serial.serial_number == serial_number))
    if not serial:
        _record_rejected_scan(db, batch, user, serial_number, "Serial number not found")
        raise InventoryError("Serial number not found")
    try:
        serial_allowed_for_batch(serial, BatchType(batch.batch_type))
        if batch.batch_type == BatchType.AUDIT.value and batch.audit_assignment_id:
            from app.services.audit import validate_assignment_scan

            validate_assignment_scan(batch, user, serial)
        fefo_error = validate_fefo_scan(db, batch, serial)
        if fefo_error:
            raise InventoryError(fefo_error)
        existing = db.scalar(
            select(BatchItem).where(BatchItem.batch_id == batch.id, BatchItem.serial_id == serial.id)
        )
        if existing:
            raise InventoryError("Already scanned in this batch")
        in_other_draft = db.scalar(
            select(BatchItem.id)
            .join(Batch, BatchItem.batch_id == Batch.id)
            .where(
                BatchItem.serial_id == serial.id,
                Batch.status == BatchStatus.DRAFT.value,
                Batch.id != batch.id,
            )
        )
        if in_other_draft:
            raise InventoryError(f"{serial.serial_number} is already in another open batch")
    except InventoryError as exc:
        _record_rejected_scan(db, batch, user, serial.serial_number, str(exc), serial=serial)
        raise
    item = BatchItem(batch_id=batch.id, serial_id=serial.id)
    db.add(item)
    db.add(
        ScanLog(
            serial_id=serial.id,
            serial_number_raw=serial.serial_number,
            user_id=user.id,
            action=batch.batch_type,
            batch_id=batch.id,
            status="SCANNED",
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _record_rejected_scan(db, batch, user, serial.serial_number, "Already scanned in this batch", serial=serial)
        raise InventoryError("Already scanned in this batch") from exc
    db.refresh(item)
    return item


def _record_rejected_scan(
    db: Session,
    batch: Batch,
    user: User,
    serial_number: str,
    message: str,
    *,
    serial: Serial | None = None,
) -> None:
    db.add(
        ScanLog(
            serial_id=serial.id if serial else None,
            serial_number_raw=serial_number,
            user_id=user.id,
            action=batch.batch_type,
            batch_id=batch.id,
            status="REJECTED",
            message=message,
        )
    )
    db.commit()


def remove_batch_item(db: Session, batch: Batch, item_id: int) -> None:
    if batch.status != BatchStatus.DRAFT.value:
        raise InventoryError("Submitted batches cannot be edited")
    item = db.get(BatchItem, item_id)
    if not item or item.batch_id != batch.id:
        raise InventoryError("Scan not found")
    db.delete(item)
    db.commit()


def apply_batch_statuses(db: Session, batch: Batch, user: User) -> None:
    from app.services.shelf_verification import validate_shelf_verification_complete

    batch_type = BatchType(batch.batch_type)
    if not batch.items:
        raise InventoryError("Scan at least one serial before submitting")
    validate_shelf_verification_complete(batch)
    for item in batch.items:
        serial_allowed_for_batch(item.serial, batch_type)
    for item in batch.items:
        previous_status = item.serial.status
        target_status = previous_status
        scan_status = "SUBMITTED"
        if batch_type != BatchType.AUDIT and item.rate is None:
            # Preserve the valuation used when the stock moved; product rates can change later.
            item.rate = float(item.serial.product.default_rate or 0)
        if batch_type in {BatchType.RECEIVE, BatchType.PURCHASE}:
            target_status = SerialStatus.IN_STOCK.value
            scan_status = SerialStatus.PURCHASED.value
        elif batch_type == BatchType.SALE:
            target_status = SerialStatus.SOLD.value
            scan_status = SerialStatus.SOLD.value
        elif batch_type == BatchType.SALES_RETURN:
            damaged = batch.reason_code in {"DAMAGED", "EXPIRED"}
            target_status = SerialStatus.DAMAGED.value if damaged else SerialStatus.IN_STOCK.value
            scan_status = SerialStatus.DAMAGED.value if damaged else SerialStatus.RETURNED.value
        elif batch_type == BatchType.PURCHASE_RETURN:
            target_status = SerialStatus.PURCHASE_RETURN.value
            scan_status = SerialStatus.PURCHASE_RETURN.value
        elif batch_type == BatchType.ISSUE:
            target_status = SerialStatus.ISSUED.value
            scan_status = SerialStatus.ISSUED.value
        elif batch_type == BatchType.AUDIT:
            scan_status = SerialStatus.AUDITED.value

        if batch_type != BatchType.AUDIT:
            claimed = db.execute(
                update(Serial)
                .where(Serial.id == item.serial.id, Serial.status == previous_status, Serial.active.is_(True))
                .values(status=target_status)
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                db.rollback()
                raise InventoryError(f"{item.serial.serial_number} is no longer available; re-scan the batch")
            item.serial.status = target_status

        db.add(
            ScanLog(
                serial_id=item.serial.id,
                serial_number_raw=item.serial.serial_number,
                user_id=user.id,
                action=batch.batch_type,
                batch_id=batch.id,
                status=scan_status,
                tally_reference=batch.tally_reference,
            )
        )
        log_inventory_transaction(
            db,
            user,
            transaction_type_for_batch(batch_type),
            serial=item.serial,
            batch=batch,
            status_from=previous_status,
            status_to=target_status,
            reason_code=batch.reason_code,
            tally_reference=batch.tally_reference,
            notes=batch.notes,
        )
    batch.status = BatchStatus.SUBMITTED.value
    batch.submitted_at = datetime.now(timezone.utc)


def update_batch_transaction_references(db: Session, batch: Batch) -> None:
    if not batch.tally_reference:
        return
    rows = db.scalars(select(InventoryTransaction).where(InventoryTransaction.batch_id == batch.id)).all()
    for row in rows:
        row.tally_reference = batch.tally_reference


def group_batch_items(batch: Batch) -> list[dict[str, object]]:
    grouped: dict[tuple[int, float], dict[str, object]] = {}
    for item in batch.items:
        product = item.serial.product
        rate = item.rate if item.rate is not None else product.default_rate
        row = grouped.setdefault(
            (product.id, float(rate or 0)),
            {
                "product": product,
                "quantity": 0,
                "serials": [],
                "rate": rate,
            },
        )
        row["quantity"] = int(row["quantity"]) + item.quantity
        row["serials"].append(item.serial.serial_number)
    return list(grouped.values())


def update_batch_item_rate(db: Session, batch: Batch, item_id: int, rate: float) -> None:
    if batch.status != BatchStatus.DRAFT.value:
        raise InventoryError("Submitted batches cannot be edited")
    if rate < 0:
        raise InventoryError("Rate cannot be negative")
    item = db.get(BatchItem, item_id)
    if not item or item.batch_id != batch.id:
        raise InventoryError("Scan not found")
    item.rate = rate
    db.commit()


def update_product_rate_in_batch(db: Session, batch: Batch, product_id: int, rate: float) -> None:
    if batch.status != BatchStatus.DRAFT.value:
        raise InventoryError("Submitted batches cannot be edited")
    if rate < 0:
        raise InventoryError("Rate cannot be negative")
    updated = False
    for item in batch.items:
        if item.serial.product_id == product_id:
            item.rate = rate
            updated = True
    if not updated:
        raise InventoryError("Product not found in batch")
    db.commit()


def generate_serials(
    db: Session,
    product: Product,
    quantity: int,
    prefix: str | None = None,
    initial_status: SerialStatus = SerialStatus.GENERATED,
    product_batch_number: str | None = None,
    mfg_date: date | None = None,
    expiry_date: date | None = None,
    warehouse: str | None = None,
    warehouse_level: str = WarehouseLevel.COMPANY_WAREHOUSE.value,
    *,
    commit: bool = True,
) -> list[Serial]:
    if quantity < 1:
        raise InventoryError("Quantity must be at least 1")
    if quantity > 5000:
        raise InventoryError("Generate 5000 labels or fewer at a time")
    serial_prefix = normalize_serial(prefix or product.product_code)
    pattern = re.compile(rf"^{re.escape(serial_prefix)}-(\d+)$")
    for attempt in range(5):
        max_number = 0
        rows = db.scalars(select(Serial.serial_number).where(Serial.serial_number.like(f"{serial_prefix}-%"))).all()
        for serial_number in rows:
            match = pattern.match(serial_number)
            if match:
                max_number = max(max_number, int(match.group(1)))
        created = []
        for offset in range(1, quantity + 1):
            serial = Serial(
                serial_number=f"{serial_prefix}-{max_number + offset:06d}",
                product_id=product.id,
                status=initial_status.value,
                product_batch_number=product_batch_number.strip().upper() if product_batch_number else None,
                mfg_date=mfg_date,
                expiry_date=expiry_date,
                warehouse=warehouse.strip().upper() if warehouse else None,
                warehouse_level=WarehouseLevel(warehouse_level).value,
            )
            db.add(serial)
            created.append(serial)
        try:
            if commit:
                db.commit()
            else:
                db.flush()
        except IntegrityError as exc:
            db.rollback()
            if not commit or attempt == 4:
                raise InventoryError("Could not allocate unique serial numbers; try again") from exc
            continue
        for serial in created:
            db.refresh(serial)
        return created
    raise InventoryError("Could not allocate unique serial numbers; try again")


def dashboard_counts(db: Session) -> dict[str, int]:
    from app.services.shelf_verification import pending_shelf_batches

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = {
        "products": db.scalar(select(func.count(Product.id))) or 0,
        "serials": db.scalar(select(func.count(Serial.id))) or 0,
        "in_stock": db.scalar(select(func.count(Serial.id)).where(Serial.status == SerialStatus.IN_STOCK.value)) or 0,
        "sold": db.scalar(select(func.count(Serial.id)).where(Serial.status == SerialStatus.SOLD.value)) or 0,
        "pending_sync": db.scalar(
            select(func.count(Batch.id)).where(
                Batch.status.in_({BatchStatus.PENDING_SYNC.value, BatchStatus.SYNCING.value})
            )
        ) or 0,
        "failed": db.scalar(select(func.count(Batch.id)).where(Batch.status == BatchStatus.FAILED.value)) or 0,
        "today_scans": db.scalar(select(func.count(ScanLog.id)).where(ScanLog.created_at >= today_start)) or 0,
        "shelf_verification_pending": len(pending_shelf_batches(db)),
    }
    return counts


def status_summary(db: Session) -> dict[str, int]:
    rows = db.execute(select(Serial.status, func.count(Serial.id)).group_by(Serial.status)).all()
    summary = defaultdict(int)
    for status, count in rows:
        label = "UNASSIGNED" if status == SerialStatus.GENERATED.value else status
        summary[label] += count
    return summary
