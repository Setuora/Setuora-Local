from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AuditFinding,
    Batch,
    BatchType,
    InventoryTransaction,
    Product,
    Serial,
    StorageLocation,
    TransactionType,
    WarehouseLevel,
)
from app.services.expiry import STOCK_STATUSES, expiry_summary, today


def audit_time(batch: Batch) -> datetime | None:
    return batch.submitted_at or batch.created_at


def _audit_order():
    return desc(func.coalesce(Batch.submitted_at, Batch.created_at))


def director_audit_batch_rows(db: Session, limit: int = 30) -> list[dict[str, object]]:
    batches = db.scalars(
        select(Batch)
        .where(Batch.batch_type == BatchType.AUDIT.value)
        .options(selectinload(Batch.user), selectinload(Batch.audit_findings))
        .order_by(_audit_order(), desc(Batch.id))
        .limit(limit)
    ).all()

    rows: list[dict[str, object]] = []
    for batch in batches:
        counts = Counter(finding.finding_type for finding in batch.audit_findings)
        product_codes = {
            finding.product_code
            for finding in batch.audit_findings
            if finding.product_code
        }
        rows.append(
            {
                "id": batch.id,
                "batch_number": batch.batch_number,
                "audited_by": batch.user.username if batch.user else "-",
                "audit_at": audit_time(batch),
                "products": len(product_codes),
                "verified": counts["VERIFIED"],
                "pending": counts["PENDING"],
                "missing": counts["MISSING"],
                "extra": counts["EXTRA"],
                "total": sum(counts.values()),
            }
        )
    return rows


def director_audit_reconciliation_report(
    db: Session,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    audit_moment = func.coalesce(Batch.submitted_at, Batch.created_at)
    query = (
        select(Batch)
        .where(Batch.batch_type == BatchType.AUDIT.value)
        .options(selectinload(Batch.user), selectinload(Batch.audit_findings))
        .order_by(desc(audit_moment), desc(Batch.id))
    )
    if start_at:
        query = query.where(audit_moment >= start_at)
    if end_at:
        query = query.where(audit_moment < end_at)
    if limit:
        query = query.limit(limit)
    batches = db.scalars(query).all()

    totals = Counter()
    product_rows: dict[tuple[str, str], dict[str, object]] = {}
    finding_rows: list[dict[str, object]] = []
    batch_rows: list[dict[str, object]] = []

    for batch in batches:
        batch_counts = Counter(finding.finding_type for finding in batch.audit_findings)
        batch_products = {
            finding.product_code
            for finding in batch.audit_findings
            if finding.product_code
        }
        batch_rows.append(
            {
                "id": batch.id,
                "batch_number": batch.batch_number,
                "audited_by": batch.user.username if batch.user else "-",
                "audit_at": audit_time(batch),
                "products": len(batch_products),
                "verified": batch_counts["VERIFIED"],
                "pending": batch_counts["PENDING"],
                "missing": batch_counts["MISSING"],
                "extra": batch_counts["EXTRA"],
                "total": sum(batch_counts.values()),
            }
        )

        for finding in batch.audit_findings:
            totals[finding.finding_type] += 1
            key = (finding.product_code or "-", finding.product_name or "-")
            product_row = product_rows.setdefault(
                key,
                {
                    "product_code": key[0],
                    "product_name": key[1],
                    "audit_batches": set(),
                    "verified": 0,
                    "pending": 0,
                    "missing": 0,
                    "extra": 0,
                    "total": 0,
                },
            )
            product_row["audit_batches"].add(batch.batch_number)  # type: ignore[union-attr]
            product_row["total"] = int(product_row["total"]) + 1
            if finding.finding_type == "VERIFIED":
                product_row["verified"] = int(product_row["verified"]) + 1
            elif finding.finding_type == "PENDING":
                product_row["pending"] = int(product_row["pending"]) + 1
            elif finding.finding_type == "MISSING":
                product_row["missing"] = int(product_row["missing"]) + 1
            elif finding.finding_type == "EXTRA":
                product_row["extra"] = int(product_row["extra"]) + 1

            finding_rows.append(
                {
                    "audit_at": audit_time(batch),
                    "batch_number": batch.batch_number,
                    "audited_by": batch.user.username if batch.user else "-",
                    "serial_number": finding.serial_number,
                    "product_code": finding.product_code or "-",
                    "product_name": finding.product_name or "-",
                    "type": finding.finding_type,
                    "expected_status": finding.expected_status or "-",
                    "scanned_status": finding.scanned_status or "-",
                }
            )

    normalized_product_rows = []
    for row in product_rows.values():
        batches_for_product = sorted(row["audit_batches"])  # type: ignore[arg-type]
        normalized_product_rows.append(
            {
                **row,
                "audit_batches": ", ".join(batches_for_product),
                "audit_batch_count": len(batches_for_product),
            }
        )

    return {
        "start_at": start_at,
        "end_at": end_at,
        "batch_rows": batch_rows,
        "product_rows": sorted(
            normalized_product_rows,
            key=lambda row: (-int(row["missing"]), -int(row["extra"]), str(row["product_name"])),
        ),
        "finding_rows": sorted(
            finding_rows,
            key=lambda row: (
                row["audit_at"] or datetime.min,
                str(row["batch_number"]),
                str(row["product_name"]),
                str(row["serial_number"]),
            ),
        ),
        "audit_batch_count": len(batches),
        "verified": totals["VERIFIED"],
        "pending": totals["PENDING"],
        "missing": totals["MISSING"],
        "extra": totals["EXTRA"],
        "total": sum(totals.values()),
    }


def director_product_stock_rows(
    db: Session,
    q: str = "",
    limit: int | None = None,
) -> list[dict[str, object]]:
    query = (
        select(
            Product.id,
            Product.product_code,
            Product.product_name,
            func.count(Serial.id).label("stock"),
            func.min(Serial.expiry_date).label("nearest_expiry"),
        )
        .outerjoin(
            Serial,
            and_(
                Serial.product_id == Product.id,
                Serial.active.is_(True),
                Serial.status.in_(STOCK_STATUSES),
            ),
        )
        .where(Product.active.is_(True))
        .group_by(Product.id, Product.product_code, Product.product_name)
        .order_by(desc(func.count(Serial.id)), Product.product_name)
    )
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Product.product_code.ilike(like),
                Product.product_name.ilike(like),
                Product.nickname.ilike(like),
                Product.tally_stock_item_name.ilike(like),
                Product.alternate_tally_stock_item_name.ilike(like),
                Product.hsn.ilike(like),
                Product.brand.ilike(like),
            )
        )
    if limit is not None:
        query = query.limit(limit)
    rows = db.execute(query).all()

    product_codes = [product_code for _product_id, product_code, *_ in rows]
    code_by_id = {product_id: product_code for product_id, product_code, *_ in rows}

    transaction_counts: dict[str, dict[str, int]] = {
        product_code: {"purchased": 0, "sold": 0} for product_code in product_codes
    }
    if code_by_id:
        for product_id, transaction_type, quantity in db.execute(
            select(
                InventoryTransaction.product_id,
                InventoryTransaction.transaction_type,
                func.count(InventoryTransaction.id),
            )
            .where(
                InventoryTransaction.product_id.in_(tuple(code_by_id)),
                InventoryTransaction.transaction_type.in_(
                    {TransactionType.PURCHASE.value, TransactionType.SALE.value}
                ),
            )
            .group_by(InventoryTransaction.product_id, InventoryTransaction.transaction_type)
        ).all():
            key = "purchased" if transaction_type == TransactionType.PURCHASE.value else "sold"
            transaction_counts[code_by_id[product_id]][key] = int(quantity or 0)

    latest_audits: dict[str, dict[str, object]] = {}
    if product_codes:
        audit_at = func.coalesce(Batch.submitted_at, Batch.created_at)
        audit_rows = db.execute(
            select(
                AuditFinding.product_code,
                Batch.id,
                audit_at.label("audit_at"),
                func.sum(
                    case(
                        (
                            AuditFinding.finding_type.in_(("VERIFIED", "EXTRA")),
                            1,
                        ),
                        else_=0,
                    )
                ).label("audited_quantity"),
                func.sum(
                    case((AuditFinding.finding_type == "MISSING", 1), else_=0)
                ).label("missing_quantity"),
                func.sum(
                    case((AuditFinding.finding_type == "EXTRA", 1), else_=0)
                ).label("extra_quantity"),
            )
            .join(Batch, Batch.id == AuditFinding.batch_id)
            .where(
                Batch.batch_type == BatchType.AUDIT.value,
                AuditFinding.product_code.in_(product_codes),
            )
            .group_by(
                AuditFinding.product_code,
                Batch.id,
                Batch.submitted_at,
                Batch.created_at,
            )
            .order_by(AuditFinding.product_code, desc(audit_at), desc(Batch.id))
        ).all()
        for (
            product_code,
            _batch_id,
            last_audit_at,
            audited_quantity,
            missing_quantity,
            extra_quantity,
        ) in audit_rows:
            if product_code not in latest_audits:
                latest_audits[product_code] = {
                    "last_audit_at": last_audit_at,
                    "last_audited_quantity": int(audited_quantity or 0),
                    "last_audit_missing": int(missing_quantity or 0),
                    "last_audit_extra": int(extra_quantity or 0),
                }

    return [
        {
            "product_code": product_code,
            "product_name": product_name,
            "stock": int(stock or 0),
            "purchased": transaction_counts[product_code]["purchased"],
            "sold": transaction_counts[product_code]["sold"],
            "last_audit_at": latest_audits.get(product_code, {}).get("last_audit_at"),
            "last_audited_quantity": latest_audits.get(product_code, {}).get(
                "last_audited_quantity", 0
            ),
            "last_audit_missing": latest_audits.get(product_code, {}).get(
                "last_audit_missing", 0
            ),
            "last_audit_extra": latest_audits.get(product_code, {}).get(
                "last_audit_extra", 0
            ),
            "nearest_expiry": nearest_expiry,
        }
        for _product_id, product_code, product_name, stock, nearest_expiry in rows
    ]


def director_product_filter_options(db: Session) -> list[dict[str, str]]:
    """Return active products for the Director Report product picker."""
    rows = db.execute(
        select(Product.product_code, Product.product_name)
        .where(Product.active.is_(True))
        .order_by(Product.product_code, Product.product_name)
    ).all()
    return [
        {"product_code": product_code, "product_name": product_name}
        for product_code, product_name in rows
    ]


WAREHOUSE_LEVEL_VALUES = tuple(level.value for level in WarehouseLevel)


def _warehouse_name(column):
    """Return a populated warehouse name, excluding franchise-level labels."""
    value = func.nullif(func.trim(column), "")
    return case(
        (value.not_in(WAREHOUSE_LEVEL_VALUES), value),
        else_=None,
    )


def _current_warehouse_name():
    return func.coalesce(
        _warehouse_name(StorageLocation.warehouse),
        _warehouse_name(Serial.warehouse),
        "Unassigned",
    )


def _current_warehouse_level():
    return func.coalesce(
        StorageLocation.warehouse_level,
        Serial.warehouse_level,
        WarehouseLevel.COMPANY_WAREHOUSE.value,
    )


def director_warehouse_stock_rows(
    db: Session,
    q: str = "",
    limit: int = 20,
) -> list[dict[str, object]]:
    """Return current in-stock units grouped by their warehouse level."""
    warehouse_level = _current_warehouse_level()
    warehouse_name = _current_warehouse_name()
    expiry_deadline = today() + timedelta(days=30)
    query = (
        select(
            warehouse_level.label("warehouse_level"),
            func.count(Serial.id).label("stock"),
            func.count(func.distinct(warehouse_name)).label("warehouses"),
            func.count(func.distinct(Serial.product_id)).label("products"),
            func.count(func.distinct(Serial.location_id)).label("locations"),
            func.sum(case((Serial.location_id.is_(None), 1), else_=0)).label(
                "unlocated"
            ),
            func.sum(
                case(
                    (
                        and_(
                            Serial.expiry_date.is_not(None),
                            Serial.expiry_date <= expiry_deadline,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("expiring_soon"),
            func.min(Serial.expiry_date).label("nearest_expiry"),
        )
        .outerjoin(StorageLocation, Serial.location_id == StorageLocation.id)
        .where(Serial.active.is_(True), Serial.status.in_(STOCK_STATUSES))
        .group_by(warehouse_level)
        .order_by(desc(func.count(Serial.id)), warehouse_level)
    )
    if q.strip():
        query = query.where(warehouse_level.ilike(f"%{q.strip()}%"))
    rows = db.execute(query.limit(limit)).all()
    return [
        {
            "warehouse_level": warehouse_level_name,
            "stock": int(stock or 0),
            "warehouses": int(warehouses or 0),
            "products": int(products or 0),
            "locations": int(locations or 0),
            "unlocated": int(unlocated or 0),
            "expiring_soon": int(expiring_soon or 0),
            "nearest_expiry": nearest_expiry,
        }
        for (
            warehouse_level_name,
            stock,
            warehouses,
            products,
            locations,
            unlocated,
            expiring_soon,
            nearest_expiry,
        ) in rows
    ]


def director_warehouse_filter_options(db: Session) -> list[str]:
    """Return every warehouse level in business hierarchy order."""
    return [level.value for level in WarehouseLevel]


def director_product_totals(db: Session) -> dict[str, int]:
    total_products = db.scalar(select(func.count(Product.id)).where(Product.active.is_(True))) or 0
    total_stock = db.scalar(
        select(func.count(Serial.id)).where(Serial.active.is_(True), Serial.status.in_(STOCK_STATUSES))
    ) or 0
    products_with_stock = db.scalar(
        select(func.count(func.distinct(Serial.product_id))).where(
            Serial.active.is_(True),
            Serial.status.in_(STOCK_STATUSES),
        )
    ) or 0
    return {
        "total_products": int(total_products),
        "total_stock": int(total_stock),
        "products_with_stock": int(products_with_stock),
    }


def director_report(
    db: Session,
    audit_start_at: datetime | None = None,
    audit_end_at: datetime | None = None,
    product_q: str = "",
    warehouse_q: str = "",
) -> dict[str, object]:
    audit_batches = director_audit_batch_rows(db)
    latest_audit = audit_batches[0] if audit_batches else None
    audit_batch_count = db.scalar(
        select(func.count(Batch.id)).where(Batch.batch_type == BatchType.AUDIT.value)
    ) or 0
    expiry = expiry_summary(db)
    sleeping_stock = expiry["sleeping_stock"]
    dead_stock_rows = [
        row for row in sleeping_stock if isinstance(row, dict) and row.get("status") == "Dead Stock"
    ]
    products = director_product_totals(db)
    return {
        "audit_batches": audit_batches,
        "audit_batch_count": int(audit_batch_count),
        "latest_audit": latest_audit,
        "latest_missing": int(latest_audit["missing"]) if latest_audit else 0,
        "latest_extra": int(latest_audit["extra"]) if latest_audit else 0,
        "expiry": expiry,
        "dead_stock_rows": dead_stock_rows,
        "product_rows": director_product_stock_rows(db, product_q),
        "warehouse_rows": director_warehouse_stock_rows(db, warehouse_q),
        "products": products,
        "reconciliation": director_audit_reconciliation_report(db, audit_start_at, audit_end_at),
    }


def director_audit_batch_report(db: Session, batch_id: int) -> dict[str, object] | None:
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id, Batch.batch_type == BatchType.AUDIT.value)
        .options(
            selectinload(Batch.user),
            selectinload(Batch.audit_findings).selectinload(AuditFinding.serial),
        )
    )
    if not batch:
        return None

    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for finding in batch.audit_findings:
        key = (finding.product_code or "-", finding.product_name or "-")
        row = grouped.setdefault(
            key,
            {
                "product_code": key[0],
                "product_name": key[1],
                "verified": 0,
                "pending": 0,
                "missing": 0,
                "extra": 0,
                "total": 0,
            },
        )
        row["total"] = int(row["total"]) + 1
        if finding.finding_type == "VERIFIED":
            row["verified"] = int(row["verified"]) + 1
        elif finding.finding_type == "PENDING":
            row["pending"] = int(row["pending"]) + 1
        elif finding.finding_type == "MISSING":
            row["missing"] = int(row["missing"]) + 1
        elif finding.finding_type == "EXTRA":
            row["extra"] = int(row["extra"]) + 1

    product_rows = sorted(
        (
            row
            for row in grouped.values()
            if int(row["pending"]) or int(row["missing"]) or int(row["extra"])
        ),
        key=lambda row: (-int(row["missing"]), -int(row["extra"]), str(row["product_name"])),
    )
    counts = Counter(finding.finding_type for finding in batch.audit_findings)
    finding_rows = sorted(
        (
            {
                "serial_number": finding.serial_number,
                "product_code": finding.product_code or "-",
                "product_name": finding.product_name or "-",
                "type": finding.finding_type,
                "expected_status": finding.expected_status or "-",
                "scanned_status": finding.scanned_status or "-",
            }
            for finding in batch.audit_findings
            if finding.finding_type in {"PENDING", "MISSING", "EXTRA"}
        ),
        key=lambda row: (str(row["type"]), str(row["product_name"]), str(row["serial_number"])),
    )
    return {
        "batch": batch,
        "audit_at": audit_time(batch),
        "audited_by": batch.user.username if batch.user else "-",
        "product_rows": product_rows,
        "finding_rows": finding_rows,
        "verified": counts["VERIFIED"],
        "pending": counts["PENDING"],
        "missing": counts["MISSING"],
        "extra": counts["EXTRA"],
        "total": sum(counts.values()),
    }
