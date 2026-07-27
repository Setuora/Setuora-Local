from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TallyLedgerCache, TallySalesVoucherCache, utc_now
from app.services.tally_masters import TallyLedger, TallySalesVoucher


def _key(value: str) -> str:
    return value.strip().casefold()


def _voucher_identity(voucher: TallySalesVoucher) -> str:
    if voucher.remote_id.strip():
        return voucher.remote_id.strip()
    return "fallback:" + "\x1f".join(
        (
            voucher.date.strip(),
            voucher.voucher_type.strip(),
            voucher.voucher_number.strip(),
            voucher.party_ledger.strip(),
            voucher.amount.strip(),
        )
    )


def replace_cached_ledgers(
    db: Session,
    company_id: int,
    tally_company: str,
    ledgers: list[TallyLedger],
) -> None:
    tally_company_name = tally_company.strip()
    tally_company_key = _key(tally_company_name)
    refreshed_at = utc_now()
    existing_rows = list(
        db.scalars(
            select(TallyLedgerCache).where(
                TallyLedgerCache.company_id == company_id,
                TallyLedgerCache.tally_company_key == tally_company_key,
            )
        )
    )
    existing = {row.ledger_key: row for row in existing_rows}
    seen: set[str] = set()
    for ledger in ledgers:
        ledger_key = _key(ledger.name)
        if not ledger_key:
            continue
        seen.add(ledger_key)
        row = existing.get(ledger_key)
        if row is None:
            row = TallyLedgerCache(
                company_id=company_id,
                tally_company=tally_company_name,
                tally_company_key=tally_company_key,
                ledger_key=ledger_key,
                name=ledger.name.strip(),
            )
            db.add(row)
        row.tally_company = tally_company_name
        row.name = ledger.name.strip()
        row.parent = ledger.parent.strip()
        row.closing_balance = ledger.closing_balance.strip()
        row.refreshed_at = refreshed_at
    for ledger_key, row in existing.items():
        if ledger_key not in seen:
            db.delete(row)
    db.commit()


def replace_cached_sales_book(
    db: Session,
    company_id: int,
    tally_company: str,
    from_date: date,
    to_date: date,
    vouchers: list[TallySalesVoucher],
) -> None:
    tally_company_name = tally_company.strip()
    tally_company_key = _key(tally_company_name)
    refreshed_at = utc_now()
    from_value = from_date.isoformat()
    to_value = to_date.isoformat()
    existing_rows = list(
        db.scalars(
            select(TallySalesVoucherCache).where(
                TallySalesVoucherCache.company_id == company_id,
                TallySalesVoucherCache.tally_company_key == tally_company_key,
                TallySalesVoucherCache.voucher_date >= from_value,
                TallySalesVoucherCache.voucher_date <= to_value,
            )
        )
    )
    invalid_rows = list(
        db.scalars(
            select(TallySalesVoucherCache).where(
                TallySalesVoucherCache.company_id == company_id,
                TallySalesVoucherCache.tally_company_key == tally_company_key,
                TallySalesVoucherCache.voucher_date == "",
            )
        )
    )
    for row in invalid_rows:
        db.delete(row)
    existing = {row.remote_id: row for row in existing_rows}
    seen: set[str] = set()
    for voucher in vouchers:
        remote_id = _voucher_identity(voucher)
        seen.add(remote_id)
        row = existing.get(remote_id)
        if row is None:
            row = TallySalesVoucherCache(
                company_id=company_id,
                tally_company=tally_company_name,
                tally_company_key=tally_company_key,
                remote_id=remote_id,
                voucher_date=voucher.date.strip(),
            )
            db.add(row)
        row.tally_company = tally_company_name
        row.voucher_date = voucher.date.strip()
        row.voucher_number = voucher.voucher_number.strip()
        row.voucher_type = voucher.voucher_type.strip()
        row.party_ledger = voucher.party_ledger.strip()
        row.amount = voucher.amount.strip()
        row.narration = voucher.narration.strip()
        row.tally_user = voucher.tally_user.strip()
        row.refreshed_at = refreshed_at
    for remote_id, row in existing.items():
        if remote_id not in seen:
            db.delete(row)
    db.commit()


def cached_ledgers(db: Session, company_id: int, tally_company: str) -> list[TallyLedger]:
    rows = db.scalars(
        select(TallyLedgerCache)
        .where(
            TallyLedgerCache.company_id == company_id,
            TallyLedgerCache.tally_company_key == _key(tally_company),
        )
        .order_by(TallyLedgerCache.name)
    )
    return [
        TallyLedger(name=row.name, parent=row.parent, closing_balance=row.closing_balance)
        for row in rows
    ]


def cached_sales_book(
    db: Session,
    company_id: int,
    tally_company: str,
    from_date: date,
    to_date: date,
) -> list[TallySalesVoucher]:
    rows = db.scalars(
        select(TallySalesVoucherCache)
        .where(
            TallySalesVoucherCache.company_id == company_id,
            TallySalesVoucherCache.tally_company_key == _key(tally_company),
            TallySalesVoucherCache.voucher_date >= from_date.isoformat(),
            TallySalesVoucherCache.voucher_date <= to_date.isoformat(),
        )
        .order_by(
            TallySalesVoucherCache.voucher_date.desc(),
            TallySalesVoucherCache.voucher_number.desc(),
        )
    )
    return [
        TallySalesVoucher(
            date=row.voucher_date,
            voucher_number=row.voucher_number,
            voucher_type=row.voucher_type,
            party_ledger=row.party_ledger,
            amount=row.amount,
            narration=row.narration,
            remote_id=row.remote_id,
            tally_user=row.tally_user,
        )
        for row in rows
    ]


def latest_cache_refresh(
    db: Session,
    company_id: int,
    tally_company: str,
) -> datetime | None:
    tally_company_key = _key(tally_company)
    timestamps = [
        db.scalar(
            select(TallyLedgerCache.refreshed_at)
            .where(
                TallyLedgerCache.company_id == company_id,
                TallyLedgerCache.tally_company_key == tally_company_key,
            )
            .order_by(TallyLedgerCache.refreshed_at.desc())
            .limit(1)
        ),
        db.scalar(
            select(TallySalesVoucherCache.refreshed_at)
            .where(
                TallySalesVoucherCache.company_id == company_id,
                TallySalesVoucherCache.tally_company_key == tally_company_key,
            )
            .order_by(TallySalesVoucherCache.refreshed_at.desc())
            .limit(1)
        ),
    ]
    available = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(available) if available else None
