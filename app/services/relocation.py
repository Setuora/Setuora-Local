from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from uuid import uuid4

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.models import (
    InventoryTransaction,
    Product,
    RelocationSerial,
    ScanLog,
    Serial,
    SerialStatus,
    StockRelocation,
    StorageLocation,
    TransactionType,
    User,
    WarehouseLevel,
)


MOVABLE_STATUSES = {SerialStatus.IN_STOCK.value, SerialStatus.RETURNED.value}
LOCATION_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._:/-]{1,139}$")


class RelocationError(ValueError):
    pass


@dataclass(frozen=True)
class MoveItem:
    product_id: int
    quantity: int
    product_batch_number: str | None = None
    source_location_id: int | None = None
    legacy_warehouse: str | None = None
    serial_id: int | None = None


def _clean(value: str | None) -> str:
    return (value or "").strip().upper()


def location_snapshot(location: StorageLocation) -> str:
    return location.full_path


def create_location(
    db: Session,
    *,
    code: str,
    warehouse: str,
    zone: str,
    section: str,
    rack: str,
    shelf: str,
    bin_name: str,
    warehouse_level: str = WarehouseLevel.COMPANY_WAREHOUSE.value,
) -> StorageLocation:
    values = {
        "code": _clean(code),
        "warehouse": _clean(warehouse),
        "zone": _clean(zone),
        "section": _clean(section),
        "rack": _clean(rack),
        "shelf": _clean(shelf),
        "bin": _clean(bin_name),
    }
    if any(not value for value in values.values()):
        raise RelocationError("Complete the warehouse, zone, section, rack, shelf, bin, and location code")
    if not LOCATION_CODE_PATTERN.fullmatch(values["code"]):
        raise RelocationError("Location code may use letters, numbers, dots, dashes, underscores, colons, and slashes")
    try:
        values["warehouse_level"] = WarehouseLevel(warehouse_level).value
    except ValueError as exc:
        raise RelocationError("Select a recognized warehouse or franchise level") from exc
    location = StorageLocation(**values)
    db.add(location)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise RelocationError("That location code or storage path already exists") from exc
    db.refresh(location)
    return location


def find_location_by_code(db: Session, code: str, *, active_only: bool = True) -> StorageLocation | None:
    query = select(StorageLocation).where(StorageLocation.code == _clean(code))
    if active_only:
        query = query.where(StorageLocation.active.is_(True))
    return db.scalar(query)


def _source_text(serial: Serial) -> str:
    if serial.location:
        return serial.location.full_path
    return serial.warehouse.strip().upper() if serial.warehouse else "UNASSIGNED"


def search_stock(db: Session, query_text: str, limit: int = 80) -> list[dict[str, object]]:
    query_text = query_text.strip()
    if not query_text:
        return []
    exact_serial = db.scalar(
        select(Serial)
        .join(Product)
        .where(
            Serial.active.is_(True),
            Serial.status.in_(MOVABLE_STATUSES),
            func.upper(Serial.serial_number) == query_text.upper(),
        )
        .options(selectinload(Serial.product), selectinload(Serial.location))
    )
    if exact_serial:
        return [
            {
                "product_id": exact_serial.product_id,
                "product_code": exact_serial.product.product_code,
                "product_name": exact_serial.product.product_name,
                "batch_number": exact_serial.product_batch_number,
                "expiry_date": exact_serial.expiry_date.isoformat() if exact_serial.expiry_date else None,
                "source_location_id": exact_serial.location_id,
                "legacy_warehouse": _clean(exact_serial.warehouse) if not exact_serial.location_id else None,
                "source_location": _source_text(exact_serial),
                "quantity": 1,
                "serial_id": exact_serial.id,
                "serial_number": exact_serial.serial_number,
            }
        ]

    like = f"%{query_text}%"
    filters = [
        Serial.serial_number.ilike(like),
        Product.product_code.ilike(like),
        Product.product_name.ilike(like),
        Serial.product_batch_number.ilike(like),
    ]
    if query_text.isdigit():
        filters.append(Product.id == int(query_text))
    legacy_warehouse = case(
        (
            Serial.location_id.is_(None),
            func.upper(func.trim(func.coalesce(Serial.warehouse, ""))),
        ),
        else_="",
    ).label("legacy_warehouse")
    rows = db.execute(
        select(
            Product.id,
            Product.product_code,
            Product.product_name,
            Serial.product_batch_number,
            Serial.location_id,
            legacy_warehouse,
            func.count(Serial.id),
            func.min(Serial.expiry_date),
        )
        .select_from(Serial)
        .join(Product)
        .where(Serial.active.is_(True), Serial.status.in_(MOVABLE_STATUSES), or_(*filters))
        .group_by(
            Product.id,
            Product.product_code,
            Product.product_name,
            Serial.product_batch_number,
            Serial.location_id,
            legacy_warehouse,
        )
        .order_by(Product.product_name, Serial.product_batch_number, Serial.location_id)
        .limit(limit)
    ).all()
    location_ids = {row[4] for row in rows if row[4] is not None}
    locations = {
        location.id: location
        for location in db.scalars(
            select(StorageLocation).where(StorageLocation.id.in_(location_ids))
        ).all()
    } if location_ids else {}
    results = []
    for (
        product_id,
        product_code,
        product_name,
        batch_number,
        location_id,
        old_warehouse,
        quantity,
        earliest_expiry,
    ) in rows:
        location = locations.get(location_id)
        results.append(
            {
                "product_id": product_id,
                "product_code": product_code,
                "product_name": product_name,
                "batch_number": batch_number,
                "expiry_date": earliest_expiry.isoformat() if earliest_expiry else None,
                "source_location_id": location_id,
                "legacy_warehouse": old_warehouse or None,
                "source_location": location.full_path if location else (old_warehouse or "UNASSIGNED"),
                "quantity": int(quantity),
                "serial_id": None,
                "serial_number": None,
            },
        )
    return results


def _candidate_query(item: MoveItem):
    query = select(Serial).where(
        Serial.active.is_(True),
        Serial.status.in_(MOVABLE_STATUSES),
        Serial.product_id == item.product_id,
    )
    if item.serial_id is not None:
        query = query.where(Serial.id == item.serial_id)
    if item.product_batch_number:
        query = query.where(Serial.product_batch_number == item.product_batch_number)
    else:
        query = query.where(Serial.product_batch_number.is_(None))
    if item.source_location_id is not None:
        query = query.where(Serial.location_id == item.source_location_id)
    else:
        query = query.where(Serial.location_id.is_(None))
        if item.legacy_warehouse:
            query = query.where(func.upper(func.trim(Serial.warehouse)) == _clean(item.legacy_warehouse))
        else:
            query = query.where(or_(Serial.warehouse.is_(None), func.trim(Serial.warehouse) == ""))
    return query.order_by(Serial.expiry_date.is_(None), Serial.expiry_date, Serial.created_at, Serial.id)


def _reference_number() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"MOV-{stamp}-{uuid4().hex[:8].upper()}"


def relocate_stock(
    db: Session,
    *,
    user: User,
    destination_id: int,
    items: list[MoveItem],
    reason: str | None,
    device_used: str,
) -> list[StockRelocation]:
    destination = db.get(StorageLocation, destination_id)
    if not destination or not destination.active:
        raise RelocationError("The destination location is invalid or inactive")
    if not items:
        raise RelocationError("Add at least one product to the move")

    relocations: list[StockRelocation] = []
    claimed_ids: set[int] = set()
    try:
        for item in items:
            if item.quantity < 1:
                raise RelocationError("Move quantity must be at least 1")
            if item.serial_id is not None and item.quantity != 1:
                raise RelocationError("A scanned serial can only move one unit")
            if item.source_location_id == destination.id:
                raise RelocationError("The destination must be different from the current location")

            serials = [
                serial
                for serial in db.scalars(
                    _candidate_query(item)
                    .limit(item.quantity + len(claimed_ids))
                    .options(selectinload(Serial.product), selectinload(Serial.location))
                ).all()
                if serial.id not in claimed_ids
            ][: item.quantity]
            if len(serials) != item.quantity:
                raise RelocationError("Some selected stock is no longer available. Search again and retry")

            previous_snapshot = _source_text(serials[0])
            if any(_source_text(serial) != previous_snapshot for serial in serials):
                raise RelocationError("Selected stock must come from one current location")
            relocation = StockRelocation(
                reference_number=_reference_number(),
                product_id=item.product_id,
                product_batch_number=serials[0].product_batch_number,
                quantity=item.quantity,
                previous_location_id=item.source_location_id,
                new_location_id=destination.id,
                previous_location_snapshot=previous_snapshot,
                new_location_snapshot=location_snapshot(destination),
                user_id=user.id,
                reason=reason.strip() if reason and reason.strip() else None,
                device_used=(device_used.strip() or "Unknown device")[:240],
            )
            db.add(relocation)
            db.flush()

            serial_ids = [serial.id for serial in serials]
            claimed = db.execute(
                update(Serial)
                .where(
                    Serial.id.in_(serial_ids),
                    Serial.active.is_(True),
                    Serial.status.in_(MOVABLE_STATUSES),
                    Serial.location_id == item.source_location_id
                    if item.source_location_id is not None
                    else Serial.location_id.is_(None),
                )
                .values(
                    location_id=destination.id,
                    warehouse=destination.warehouse,
                    warehouse_level=destination.warehouse_level,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != item.quantity:
                raise RelocationError("Some selected stock moved at the same time. Search again and retry")

            note = f"{previous_snapshot} -> {destination.full_path}"
            for serial in serials:
                claimed_ids.add(serial.id)
                db.add(RelocationSerial(relocation_id=relocation.id, serial_id=serial.id))
                db.add(
                    ScanLog(
                        serial_id=serial.id,
                        serial_number_raw=serial.serial_number,
                        user_id=user.id,
                        action=TransactionType.RELOCATION.value,
                        status="MOVED",
                        message=note,
                    )
                )
                db.add(
                    InventoryTransaction(
                        transaction_type=TransactionType.RELOCATION.value,
                        serial_id=serial.id,
                        product_id=serial.product_id,
                        user_id=user.id,
                        serial_number=serial.serial_number,
                        status_from=serial.status,
                        status_to=serial.status,
                        reference_number=relocation.reference_number,
                        notes=note,
                    )
                )
            relocations.append(relocation)
        db.commit()
    except RelocationError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise RelocationError("The relocation could not be completed. No stock was moved") from exc
    for relocation in relocations:
        db.refresh(relocation)
    return relocations


def warehouse_map_rows(db: Session) -> list[dict[str, object]]:
    locations = db.scalars(select(StorageLocation).order_by(
        StorageLocation.warehouse,
        StorageLocation.zone,
        StorageLocation.section,
        StorageLocation.rack,
        StorageLocation.shelf,
        StorageLocation.bin,
    )).all()
    quantities = dict(
        db.execute(
            select(Serial.location_id, func.count(Serial.id))
            .where(
                Serial.active.is_(True),
                Serial.status.in_(MOVABLE_STATUSES),
                Serial.location_id.is_not(None),
            )
            .group_by(Serial.location_id)
        ).all()
    )
    return [
        {
            "location": location,
            "quantity": int(quantities.get(location.id, 0)),
        }
        for location in locations
    ]
