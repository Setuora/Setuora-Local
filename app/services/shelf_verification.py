from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Batch,
    BatchItem,
    BatchStatus,
    BatchType,
    ScanLog,
    Serial,
    StorageLocation,
    User,
)


SHELF_CONTROLLED_BATCH_TYPES = {
    BatchType.PURCHASE.value,
    BatchType.RECEIVE.value,
    BatchType.AUDIT.value,
}


class ShelfVerificationError(ValueError):
    pass


def _interval(item: BatchItem) -> int:
    return max(1, int(item.serial.product.shelf_verification_interval or 1))


def pending_shelf_items(batch: Batch) -> list[BatchItem]:
    if batch.batch_type not in SHELF_CONTROLLED_BATCH_TYPES:
        return []
    return [
        item
        for item in batch.items
        if _interval(item) > 0 and item.shelf_verified_at is None
    ]


def shelf_verification_state(batch: Batch) -> dict[str, int | bool]:
    pending = pending_shelf_items(batch)
    counts = Counter(item.serial.product_id for item in pending)
    intervals = {
        item.serial.product_id: _interval(item)
        for item in pending
    }
    due = any(counts[product_id] >= interval for product_id, interval in intervals.items())
    next_required_in = min(
        (interval - counts[product_id] for product_id, interval in intervals.items()),
        default=0,
    )
    return {
        "controlled": batch.batch_type in SHELF_CONTROLLED_BATCH_TYPES,
        "pending_count": len(pending),
        "shelf_required": due,
        "next_required_in": max(0, next_required_in),
        "complete": len(pending) == 0,
    }


def ensure_product_scan_allowed(batch: Batch) -> None:
    state = shelf_verification_state(batch)
    if state["shelf_required"]:
        raise ShelfVerificationError(
            "Shelf verification is required now. Scan the shelf QR before scanning another product."
        )


def verify_pending_items_on_shelf(
    db: Session,
    *,
    batch: Batch,
    location: StorageLocation,
    user: User,
) -> int:
    if batch.status != BatchStatus.DRAFT.value:
        raise ShelfVerificationError("This batch is already submitted")
    if batch.batch_type not in SHELF_CONTROLLED_BATCH_TYPES:
        raise ShelfVerificationError("Shelf verification is not used for this batch type")

    pending = pending_shelf_items(batch)
    if not pending:
        raise ShelfVerificationError("Scan at least one shelf-controlled product before the shelf QR")

    verified_at = datetime.now(timezone.utc)
    mismatch_count = 0
    for item in pending:
        previous_location_id = item.serial.location_id
        if (
            batch.batch_type == BatchType.AUDIT.value
            and previous_location_id is not None
            and previous_location_id != location.id
        ):
            mismatch_count += 1
        item.shelf_location_id = location.id
        item.shelf_verified_by_id = user.id
        item.shelf_verified_at = verified_at
        item.serial.location_id = location.id
        item.serial.warehouse = location.warehouse
        item.serial.warehouse_level = location.warehouse_level

    message = f"{len(pending)} product(s) verified at {location.full_path}"
    if mismatch_count:
        message += f"; {mismatch_count} audit location mismatch(es) recorded"
    db.add(
        ScanLog(
            serial_number_raw=location.code,
            user_id=user.id,
            action="SHELF_VERIFY",
            batch_id=batch.id,
            status="MISMATCH" if mismatch_count else "VERIFIED",
            message=message,
        )
    )
    db.commit()
    return len(pending)


def validate_shelf_verification_complete(batch: Batch) -> None:
    pending_count = len(pending_shelf_items(batch))
    if pending_count:
        noun = "product" if pending_count == 1 else "products"
        workflow = "Purchase" if batch.batch_type in {BatchType.PURCHASE.value, BatchType.RECEIVE.value} else "Audit"
        raise ShelfVerificationError(
            f"{workflow} is incomplete: verify the shelf QR for {pending_count} scanned {noun} before submitting."
        )


def pending_shelf_batches(db: Session, *, limit: int | None = None) -> list[dict[str, object]]:
    query = (
        select(Batch)
        .where(
            Batch.status == BatchStatus.DRAFT.value,
            Batch.batch_type.in_(SHELF_CONTROLLED_BATCH_TYPES),
        )
        .options(
            selectinload(Batch.user),
            selectinload(Batch.items)
            .selectinload(BatchItem.serial)
            .selectinload(Serial.product),
        )
        .order_by(desc(Batch.created_at))
    )
    rows: list[dict[str, object]] = []
    for batch in db.scalars(query).all():
        state = shelf_verification_state(batch)
        if state["pending_count"]:
            rows.append({"batch": batch, **state})
            if limit is not None and len(rows) >= limit:
                break
    return rows
