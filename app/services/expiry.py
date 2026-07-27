from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import floor

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Batch, BatchItem, BatchStatus, InventoryTransaction, Product, ScanLog, Serial, SerialStatus, TransactionType, User


STOCK_STATUSES = {SerialStatus.IN_STOCK.value, SerialStatus.RETURNED.value}
FEFO_BATCH_TYPES = {"SALE", "ISSUE", "PURCHASE_RETURN"}


def fefo_available_statuses(batch_type: str) -> set[str]:
    if batch_type == "ISSUE":
        return {SerialStatus.IN_STOCK.value}
    if batch_type in FEFO_BATCH_TYPES:
        return set(STOCK_STATUSES)
    return set()


@dataclass(frozen=True)
class ExpiryBand:
    label: str
    css_class: str


def today() -> date:
    return datetime.now(timezone.utc).date()


def parse_optional_date(value: str | date | datetime | None) -> date | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return date.fromisoformat(text)


def expiring_band(days_left: int) -> ExpiryBand:
    if days_left <= 30:
        return ExpiryBand("Expiring within 30 days", "failed")
    if days_left <= 60:
        return ExpiryBand("Expiring within 60 days", "pending_sync")
    return ExpiryBand("Expiring within 90 days", "generated")


def stock_serials(db: Session, product_id: int | None = None) -> list[Serial]:
    query = select(Serial).where(Serial.active.is_(True), Serial.status.in_(STOCK_STATUSES))
    if product_id is not None:
        query = query.where(Serial.product_id == product_id)
    return db.scalars(
        query.options(selectinload(Serial.product)).order_by(
            Serial.expiry_date.is_(None),
            Serial.expiry_date,
            Serial.created_at,
        )
    ).all()


def expiry_batch_rows(
    db: Session,
    horizon_days: int = 90,
    as_of: date | None = None,
    product_id: int | None = None,
) -> list[dict[str, object]]:
    as_of = as_of or today()
    deadline = as_of + timedelta(days=horizon_days)
    grouped: dict[tuple[int, str, date, str], dict[str, object]] = {}
    for serial in stock_serials(db, product_id=product_id):
        if not serial.expiry_date or serial.expiry_date > deadline:
            continue
        key = (
            serial.product_id,
            serial.product_batch_number or "-",
            serial.expiry_date,
            serial.warehouse or "Main",
        )
        row = grouped.setdefault(
            key,
            {
                "product": serial.product,
                "product_name": serial.product.product_name,
                "product_code": serial.product.product_code,
                "batch": serial.product_batch_number or "-",
                "warehouse": serial.warehouse or "Main",
                "expiry_date": serial.expiry_date,
                "qty": 0,
                "value": 0.0,
            },
        )
        row["qty"] = int(row["qty"]) + 1
        row["value"] = float(row["value"]) + float(serial.product.default_rate or 0)

    rows: list[dict[str, object]] = []
    for row in grouped.values():
        days_left = int((row["expiry_date"] - as_of).days)  # type: ignore[operator]
        band = expiring_band(days_left)
        row["days_left"] = days_left
        row["band"] = band.label
        row["status_class"] = band.css_class
        rows.append(row)
    return sorted(rows, key=lambda item: (item["days_left"], item["product_name"], item["batch"]))


def sales_velocity_by_product(
    db: Session,
    months: int = 3,
    as_of: date | None = None,
    product_id: int | None = None,
) -> dict[int, float]:
    as_of = as_of or today()
    start_at = datetime.combine(as_of - timedelta(days=months * 30), datetime.min.time(), tzinfo=timezone.utc)
    conditions = [
        InventoryTransaction.transaction_type == TransactionType.SALE.value,
        InventoryTransaction.status_to == SerialStatus.SOLD.value,
        InventoryTransaction.product_id.is_not(None),
        InventoryTransaction.created_at >= start_at,
    ]
    if product_id is not None:
        conditions.append(InventoryTransaction.product_id == product_id)
    rows = db.execute(
        select(InventoryTransaction.product_id, func.count(InventoryTransaction.id))
        .where(*conditions)
        .group_by(InventoryTransaction.product_id)
    ).all()
    return {int(product_id): count / max(months, 1) for product_id, count in rows if product_id is not None}


def expiry_risk_rows(
    db: Session,
    as_of: date | None = None,
    product_id: int | None = None,
) -> list[dict[str, object]]:
    as_of = as_of or today()
    velocity = sales_velocity_by_product(db, as_of=as_of, product_id=product_id)
    rows: list[dict[str, object]] = []
    for row in expiry_batch_rows(db, horizon_days=365, as_of=as_of, product_id=product_id):
        product: Product = row["product"]  # type: ignore[assignment]
        qty = int(row["qty"])
        days_left = int(row["days_left"])
        months_left = max(days_left / 30, 0)
        sales_per_month = velocity.get(product.id, 0.0)
        expected_sales = floor(sales_per_month * months_left)
        unsold_stock = max(qty - expected_sales, 0)
        risk_ratio = unsold_stock / qty if qty else 0
        if days_left <= 30 or risk_ratio >= 0.65:
            risk = "High Expiry Risk"
            status_class = "failed"
        elif days_left <= 90 or risk_ratio >= 0.35:
            risk = "Watch"
            status_class = "pending_sync"
        else:
            risk = "Monitor"
            status_class = "synced"
        basis = f"{days_left} days left; {unsold_stock} of {qty} projected unsold"
        row.update(
            {
                "stock": qty,
                "sales_per_month": round(sales_per_month, 1),
                "months_left": round(months_left, 1),
                "expected_sales": expected_sales,
                "unsold_stock": unsold_stock,
                "risk": risk,
                "risk_class": status_class,
                "risk_basis": basis,
            }
        )
        if unsold_stock > 0 or days_left <= 90:
            rows.append(row)
    return sorted(rows, key=lambda item: (item["risk_class"] != "failed", item["days_left"], -int(item["unsold_stock"])))


def sleeping_stock_rows(
    db: Session,
    as_of: date | None = None,
    product_id: int | None = None,
) -> list[dict[str, object]]:
    as_of = as_of or today()
    stock_counts = Counter(serial.product_id for serial in stock_serials(db, product_id=product_id))
    if not stock_counts:
        return []
    last_sale_rows = db.execute(
        select(InventoryTransaction.product_id, func.max(InventoryTransaction.created_at))
        .where(
            InventoryTransaction.transaction_type == TransactionType.SALE.value,
            InventoryTransaction.status_to == SerialStatus.SOLD.value,
            InventoryTransaction.product_id.in_(stock_counts.keys()),
        )
        .group_by(InventoryTransaction.product_id)
    ).all()
    last_sales = {product_id: last_sale for product_id, last_sale in last_sale_rows}
    products = {
        product.id: product
        for product in db.scalars(select(Product).where(Product.id.in_(stock_counts.keys()))).all()
    }
    rows: list[dict[str, object]] = []
    for product_id, qty in stock_counts.items():
        product = products.get(product_id)
        if not product:
            continue
        last_sale = last_sales.get(product_id)
        if last_sale:
            days_since = (as_of - last_sale.date()).days
            last_sale_label = f"{days_since} days ago"
        else:
            days_since = (as_of - product.created_at.date()).days
            last_sale_label = "No sale yet"
        if days_since < 30:
            continue
        if days_since >= 90:
            status = "Dead Stock"
            status_class = "failed"
        elif days_since >= 60:
            status = "Slow Moving"
            status_class = "pending_sync"
        else:
            status = "Watch"
            status_class = "generated"
        basis = f"{qty} in stock; {days_since} days since last sale"
        rows.append(
            {
                "product": product,
                "product_name": product.product_name,
                "stock": qty,
                "last_sale": last_sale_label,
                "days_since_last_sale": days_since,
                "status": status,
                "status_class": status_class,
                "value": qty * float(product.default_rate or 0),
                "stock_basis": basis,
            }
        )
    return sorted(rows, key=lambda item: (-int(item["days_since_last_sale"]), item["product_name"]))


def warehouse_heatmap_rows(
    db: Session,
    as_of: date | None = None,
    product_id: int | None = None,
) -> list[dict[str, object]]:
    totals: dict[str, float] = defaultdict(float)
    qty_by_warehouse: dict[str, int] = defaultdict(int)
    for row in expiry_batch_rows(db, horizon_days=90, as_of=as_of, product_id=product_id):
        warehouse = str(row["warehouse"])
        totals[warehouse] += float(row["value"])
        qty_by_warehouse[warehouse] += int(row["qty"])
    max_value = max(totals.values(), default=0)
    rows = []
    for warehouse, value in totals.items():
        rows.append(
            {
                "warehouse": warehouse,
                "value": value,
                "qty": qty_by_warehouse[warehouse],
                "percent": round((value / max_value) * 100, 1) if max_value else 0,
                "status_class": "failed" if max_value and value >= max_value else "pending_sync",
            }
        )
    return sorted(rows, key=lambda item: -float(item["value"]))


def fefo_candidate_serials(
    db: Session,
    product_id: int,
    quantity: int,
    statuses: set[str] | None = None,
) -> list[Serial]:
    if quantity < 1:
        return []
    available_statuses = statuses or STOCK_STATUSES
    query = (
        select(Serial)
        .where(
            Serial.active.is_(True),
            Serial.product_id == product_id,
            Serial.status.in_(available_statuses),
        )
        .options(selectinload(Serial.product))
        .order_by(Serial.expiry_date.is_(None), Serial.expiry_date, Serial.created_at, Serial.id)
        .limit(quantity)
    )
    draft_subquery = (
        select(BatchItem.serial_id)
        .join(Batch, BatchItem.batch_id == Batch.id)
        .where(Batch.status == BatchStatus.DRAFT.value)
    )
    query = query.where(~Serial.id.in_(draft_subquery))
    return db.scalars(query).all()


def validate_fefo_scan(db: Session, batch: Batch, serial: Serial) -> str | None:
    statuses = fefo_available_statuses(batch.batch_type)
    if batch.batch_type not in FEFO_BATCH_TYPES or serial.status not in statuses:
        return None
    candidates = fefo_candidate_serials(db, serial.product_id, 1, statuses=statuses)
    if not candidates:
        return None
    earliest = candidates[0]
    if earliest.id == serial.id:
        return None
    selected_key = (serial.expiry_date is None, serial.expiry_date or date.max, serial.created_at, serial.id)
    earliest_key = (earliest.expiry_date is None, earliest.expiry_date or date.max, earliest.created_at, earliest.id)
    if earliest_key < selected_key:
        expiry_text = earliest.expiry_date.strftime("%d %b %Y") if earliest.expiry_date else "no expiry date"
        return f"FEFO requires {earliest.serial_number} first; it expires on {expiry_text}"
    return None


def add_fefo_serials_to_batch(db: Session, batch: Batch, user: User, product_id: int, quantity: int) -> list[BatchItem]:
    from app.services.inventory import InventoryError

    if batch.status != "DRAFT":
        raise InventoryError("This batch is already submitted")
    if batch.batch_type not in FEFO_BATCH_TYPES:
        raise InventoryError("FEFO picking is available for sale, issue, and purchase return batches")
    statuses = fefo_available_statuses(batch.batch_type)
    serials = fefo_candidate_serials(db, product_id, quantity, statuses=statuses)
    if len(serials) < quantity:
        raise InventoryError(f"Only {len(serials)} FEFO-ready serials are available")
    items: list[BatchItem] = []
    for serial in serials:
        item = BatchItem(batch_id=batch.id, serial_id=serial.id, fefo_picked=True)
        db.add(item)
        db.add(
            ScanLog(
                serial_id=serial.id,
                serial_number_raw=serial.serial_number,
                user_id=user.id,
                action=batch.batch_type,
                batch_id=batch.id,
                status="FEFO_PICKED",
                message="Auto-picked by First Expiry First Out",
            )
        )
        items.append(item)
    db.commit()
    for item in items:
        db.refresh(item)
    return items


def fefo_compliance_percent(db: Session, product_id: int | None = None) -> int:
    query = (
        select(func.count(BatchItem.id), func.sum(case((BatchItem.fefo_picked.is_(True), 1), else_=0)))
        .join(Batch, BatchItem.batch_id == Batch.id)
        .where(Batch.batch_type.in_(FEFO_BATCH_TYPES), Batch.status != "DRAFT")
    )
    if product_id is not None:
        query = query.join(Serial, BatchItem.serial_id == Serial.id).where(Serial.product_id == product_id)
    rows = db.execute(query).one()
    total = rows[0] or 0
    picked = rows[1] or 0
    if not total:
        return 100
    return round((picked / total) * 100)


def expiry_loss_avoided_this_month(
    db: Session,
    as_of: date | None = None,
    product_id: int | None = None,
) -> float:
    as_of = as_of or today()
    start_at = datetime.combine(as_of.replace(day=1), datetime.min.time(), tzinfo=timezone.utc)
    deadline = as_of + timedelta(days=90)
    conditions = [
        InventoryTransaction.transaction_type == TransactionType.SALE.value,
        InventoryTransaction.status_to == SerialStatus.SOLD.value,
        InventoryTransaction.created_at >= start_at,
    ]
    if product_id is not None:
        conditions.append(InventoryTransaction.product_id == product_id)
    transactions = db.scalars(
        select(InventoryTransaction)
        .where(*conditions)
        .options(selectinload(InventoryTransaction.serial).selectinload(Serial.product))
    ).all()
    value = 0.0
    for txn in transactions:
        serial = txn.serial
        if serial and serial.expiry_date and serial.expiry_date <= deadline:
            value += float(serial.product.default_rate or 0)
    return value


def expiry_summary(
    db: Session,
    as_of: date | None = None,
    product_id: int | None = None,
) -> dict[str, object]:
    as_of = as_of or today()
    critical = expiry_batch_rows(db, as_of=as_of, product_id=product_id)
    risk = expiry_risk_rows(db, as_of=as_of, product_id=product_id)
    sleeping = sleeping_stock_rows(db, as_of=as_of, product_id=product_id)
    warehouse = warehouse_heatmap_rows(db, as_of=as_of, product_id=product_id)
    expiring_30 = sum(1 for row in critical if int(row["days_left"]) <= 30)
    expiring_60 = sum(1 for row in critical if int(row["days_left"]) <= 60)
    high_risk = [row for row in risk if row["risk"] == "High Expiry Risk"]
    dead_stock_value = sum(float(row["value"]) for row in sleeping if row["status"] == "Dead Stock")
    slow_stock_value = sum(float(row["value"]) for row in sleeping if row["status"] in {"Slow Moving", "Dead Stock"})
    batch_count = len(
        {
            (row["product_code"], row["batch"], row["expiry_date"], row["warehouse"])
            for row in expiry_batch_rows(db, horizon_days=365, as_of=as_of, product_id=product_id)
        }
    )
    return {
        "critical_alerts": critical,
        "risk_rows": risk,
        "sleeping_stock": sleeping,
        "warehouse_heatmap": warehouse,
        "widgets": {
            "expiring_30": expiring_30,
            "expiring_60": expiring_60,
            "high_risk": len(high_risk),
            "dead_stock_value": dead_stock_value,
            "slow_stock_value": slow_stock_value,
            "batch_wise_stock": batch_count,
            "fefo_compliance": fefo_compliance_percent(db, product_id=product_id),
            "expiry_loss_avoided": expiry_loss_avoided_this_month(db, as_of=as_of, product_id=product_id),
        },
    }
