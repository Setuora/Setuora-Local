from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Batch, BatchItem, BatchStatus, BatchType, ScanLog, Serial, SerialStatus, StorageLocation, User
from app.services.inventory import InventoryError, normalize_serial


SALE_RETURN_ACTION = "SALE_RETURN"
SALE_RETURN_SHELF_ACTION = "SALE_RETURN_SHELF"
SALE_RETURN_PENDING_STATUS = "PENDING_SHELF"
SALE_RETURN_VERIFIED_STATUS = "SHELF_VERIFIED"


def _ensure_draft_sale(batch: Batch) -> None:
    if batch.batch_type != BatchType.SALE.value:
        raise InventoryError("Return mode is available only inside a sale.")
    if batch.status != BatchStatus.DRAFT.value:
        raise InventoryError("This sale is already submitted.")


def pending_sale_return_logs(db: Session, batch: Batch) -> list[ScanLog]:
    return db.scalars(
        select(ScanLog)
        .where(
            ScanLog.batch_id == batch.id,
            ScanLog.action == SALE_RETURN_ACTION,
            ScanLog.status == SALE_RETURN_PENDING_STATUS,
        )
        .options(selectinload(ScanLog.serial).selectinload(Serial.product))
        .order_by(ScanLog.created_at)
    ).all()


def sale_return_state(db: Session, batch: Batch) -> dict[str, object]:
    if batch.batch_type != BatchType.SALE.value:
        return {"controlled": False, "pending_count": 0, "return_shelf_required": False, "pending_serials": []}
    pending = pending_sale_return_logs(db, batch)
    return {
        "controlled": True,
        "pending_count": len(pending),
        "return_shelf_required": bool(pending),
        "pending_serials": [
            {
                "serial": log.serial.serial_number if log.serial else log.serial_number_raw,
                "product": log.serial.product.product_name if log.serial and log.serial.product else "",
            }
            for log in pending
        ],
    }


def ensure_sale_scan_allowed(db: Session, batch: Batch) -> None:
    if batch.batch_type == BatchType.SALE.value and pending_sale_return_logs(db, batch):
        raise InventoryError("Scan the shelf QR for the returned product before continuing the sale.")


def _record_rejected_sale_return(
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
            action=SALE_RETURN_ACTION,
            batch_id=batch.id,
            status="REJECTED",
            message=message,
        )
    )
    db.commit()


def scan_sale_return_product(db: Session, batch: Batch, user: User, serial_number: str) -> Serial:
    _ensure_draft_sale(batch)
    if pending_sale_return_logs(db, batch):
        raise InventoryError("Scan the shelf QR for the returned product before returning another item.")

    normalized = normalize_serial(serial_number)
    serial = db.scalar(
        select(Serial)
        .where(Serial.serial_number == normalized)
        .options(selectinload(Serial.product))
    )
    if not serial:
        _record_rejected_sale_return(db, batch, user, normalized, "Serial number not found")
        raise InventoryError("Serial number not found")
    if not serial.active or serial.status in {SerialStatus.INVALID.value, SerialStatus.REPLACED.value}:
        message = f"{serial.serial_number} is inactive"
        _record_rejected_sale_return(db, batch, user, normalized, message, serial=serial)
        raise InventoryError(message)

    item = db.scalar(
        select(BatchItem).where(
            BatchItem.batch_id == batch.id,
            BatchItem.serial_id == serial.id,
        )
    )
    if not item:
        message = "Only products already scanned in this sale can be returned before checkout."
        _record_rejected_sale_return(db, batch, user, normalized, message, serial=serial)
        raise InventoryError(message)

    db.delete(item)
    db.add(
        ScanLog(
            serial_id=serial.id,
            serial_number_raw=serial.serial_number,
            user_id=user.id,
            action=SALE_RETURN_ACTION,
            batch_id=batch.id,
            status=SALE_RETURN_PENDING_STATUS,
            message="Removed from sale; waiting for shelf QR.",
        )
    )
    db.commit()
    db.refresh(serial)
    return serial


def verify_sale_return_on_shelf(
    db: Session,
    *,
    batch: Batch,
    location: StorageLocation,
    user: User,
) -> int:
    _ensure_draft_sale(batch)
    pending = pending_sale_return_logs(db, batch)
    if not pending:
        raise InventoryError("Scan a returned sale product before scanning the shelf QR.")

    verified_at = datetime.now(timezone.utc)
    for log in pending:
        log.status = SALE_RETURN_VERIFIED_STATUS
        log.message = f"Returned product placed at {location.full_path}."
        serial = log.serial
        if serial:
            serial.location_id = location.id
            serial.warehouse = location.warehouse
            serial.warehouse_level = location.warehouse_level

    db.add(
        ScanLog(
            serial_number_raw=location.code,
            user_id=user.id,
            action=SALE_RETURN_SHELF_ACTION,
            batch_id=batch.id,
            status="VERIFIED",
            message=f"{len(pending)} returned product(s) placed at {location.full_path}.",
            created_at=verified_at,
        )
    )
    db.commit()
    return len(pending)


def validate_sale_returns_complete(db: Session, batch: Batch) -> None:
    if batch.batch_type != BatchType.SALE.value:
        return
    pending_count = len(pending_sale_return_logs(db, batch))
    if pending_count:
        noun = "product" if pending_count == 1 else "products"
        raise InventoryError(
            f"Sale return is incomplete: scan the shelf QR for {pending_count} returned {noun} before submitting."
        )
