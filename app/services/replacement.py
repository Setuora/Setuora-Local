from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ScanLog, Serial, SerialStatus, TransactionType, User
from app.services.inventory import InventoryError, generate_serials, log_inventory_transaction, normalize_serial


REPLACEABLE_STOCK_STATUSES = {
    SerialStatus.IN_STOCK,
    SerialStatus.RETURNED,
    SerialStatus.DAMAGED,
}


def replacement_status_for(original_status: SerialStatus) -> SerialStatus:
    """Replacement labels restart as either unassigned or normal stock."""
    if original_status == SerialStatus.GENERATED:
        return SerialStatus.GENERATED
    if original_status in REPLACEABLE_STOCK_STATUSES:
        return SerialStatus.IN_STOCK
    raise InventoryError("Only unassigned or in-stock QR codes can be replaced")


def replace_barcode_serial(db: Session, user: User, old_serial_number: str, new_serial_number: str | None = None, reason: str | None = None) -> Serial:
    old_serial = db.scalar(select(Serial).where(Serial.serial_number == normalize_serial(old_serial_number)))
    if not old_serial:
        raise InventoryError("Serial number not found")
    if old_serial.status in {SerialStatus.INVALID.value, SerialStatus.REPLACED.value} or not old_serial.active:
        raise InventoryError("Serial is already inactive or invalid")

    original_status = SerialStatus(old_serial.status)
    replacement_status = replacement_status_for(original_status)
    if new_serial_number and new_serial_number.strip():
        replacement = Serial(
            serial_number=normalize_serial(new_serial_number),
            product_id=old_serial.product_id,
            status=replacement_status.value,
            active=True,
        )
        db.add(replacement)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise InventoryError("Replacement serial already exists") from exc
    else:
        replacement = generate_serials(
            db,
            old_serial.product,
            1,
            old_serial.product.product_code,
            replacement_status,
            commit=False,
        )[0]

    # A label replacement is the same physical unit, so retain every stock-analysis dimension.
    replacement.product_batch_number = old_serial.product_batch_number
    replacement.mfg_date = old_serial.mfg_date
    replacement.expiry_date = old_serial.expiry_date
    replacement.warehouse = old_serial.warehouse
    replacement.warehouse_level = old_serial.warehouse_level
    replacement.location_id = old_serial.location_id
    old_serial.status = SerialStatus.INVALID.value
    old_serial.active = False
    old_serial.replaced_by_id = replacement.id
    db.add(
        ScanLog(
            serial_id=old_serial.id,
            serial_number_raw=old_serial.serial_number,
            user_id=user.id,
            action=TransactionType.QR_REPLACEMENT.value,
            status=SerialStatus.INVALID.value,
            message=f"New serial: {replacement.serial_number}. {reason or ''}".strip(),
        )
    )
    db.add(
        ScanLog(
            serial_id=replacement.id,
            serial_number_raw=replacement.serial_number,
            user_id=user.id,
            action=TransactionType.QR_REPLACEMENT.value,
            status=replacement.status,
            message=f"Replaces: {old_serial.serial_number}. {reason or ''}".strip(),
        )
    )
    log_inventory_transaction(
        db,
        user,
        TransactionType.QR_REPLACEMENT,
        serial=old_serial,
        status_from=original_status.value,
        status_to=SerialStatus.INVALID.value,
        notes=f"New serial: {replacement.serial_number}. {reason or ''}".strip(),
    )
    log_inventory_transaction(
        db,
        user,
        TransactionType.QR_REPLACEMENT,
        serial=replacement,
        status_from=None,
        status_to=replacement.status,
        notes=f"Replaces: {old_serial.serial_number}. {reason or ''}".strip(),
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(replacement)
    return replacement


def replace_qr_serial(db: Session, user: User, old_serial_number: str, new_serial_number: str | None = None, reason: str | None = None) -> Serial:
    return replace_barcode_serial(db, user, old_serial_number, new_serial_number, reason)
