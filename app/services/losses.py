from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import BatchItem, InventoryTransaction, Product, SerialStatus, TransactionType


LOSS_FACTORS = (
    ("TRANSPORTATION", "Transportation"),
    ("THEFT", "Theft"),
    ("OTHER", "Other Things"),
)

LOSS_REASON_ALIASES = {
    "DAMAGE": "OTHER",
    "DAMAGED": "OTHER",
    "EXPIRED": "OTHER",
}


@dataclass(frozen=True)
class LossFactorRow:
    code: str
    label: str
    quantity: int
    value: float


@dataclass(frozen=True)
class LossSummary:
    rows: list[LossFactorRow]
    total_quantity: int
    total_value: float


def loss_summary(
    db: Session,
    *,
    action: str = "",
    q: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    product_id: int | None = None,
) -> LossSummary:
    loss_reason_codes = {code for code, _ in LOSS_FACTORS} | set(LOSS_REASON_ALIASES)
    conditions = [
        or_(
            and_(
                InventoryTransaction.transaction_type == TransactionType.ISSUE.value,
                InventoryTransaction.reason_code.in_(loss_reason_codes),
            ),
            and_(
                InventoryTransaction.status_to == SerialStatus.DAMAGED.value,
                InventoryTransaction.reason_code.in_(LOSS_REASON_ALIASES),
            ),
        )
    ]
    if action:
        conditions.append(InventoryTransaction.transaction_type == action)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                InventoryTransaction.serial_number.ilike(like),
                InventoryTransaction.tally_reference.ilike(like),
                InventoryTransaction.reference_number.ilike(like),
                Product.product_code.ilike(like),
                Product.product_name.ilike(like),
            )
        )
    if product_id is not None:
        conditions.append(InventoryTransaction.product_id == product_id)
    if start:
        conditions.append(InventoryTransaction.created_at >= start)
    if end:
        conditions.append(InventoryTransaction.created_at < end)

    rows = db.execute(
        select(
            InventoryTransaction.reason_code,
            BatchItem.quantity,
            BatchItem.rate,
            Product.default_rate,
        )
        .outerjoin(Product, InventoryTransaction.product_id == Product.id)
        .outerjoin(
            BatchItem,
            and_(
                InventoryTransaction.batch_id == BatchItem.batch_id,
                InventoryTransaction.serial_id == BatchItem.serial_id,
            ),
        )
        .where(and_(*conditions))
    ).all()

    totals = {code: {"quantity": 0, "value": 0.0} for code, _ in LOSS_FACTORS}
    for reason_code, quantity, recorded_rate, default_rate in rows:
        code = LOSS_REASON_ALIASES.get(reason_code or "", reason_code or "")
        if code not in totals:
            continue
        item_quantity = int(quantity or 1)
        rate = recorded_rate if recorded_rate is not None else default_rate
        totals[code]["quantity"] += item_quantity
        totals[code]["value"] += item_quantity * float(rate or 0)

    factor_rows = [
        LossFactorRow(
            code=code,
            label=label,
            quantity=int(totals[code]["quantity"]),
            value=round(float(totals[code]["value"]), 2),
        )
        for code, label in LOSS_FACTORS
    ]
    return LossSummary(
        rows=factor_rows,
        total_quantity=sum(row.quantity for row in factor_rows),
        total_value=round(sum(row.value for row in factor_rows), 2),
    )
