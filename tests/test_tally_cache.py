from datetime import date

from sqlalchemy import func, select

from app.models import TallyLedgerCache, TallySalesVoucherCache
from app.services.settings import add_company
from app.services.tally_cache import (
    cached_ledgers,
    cached_sales_book,
    replace_cached_ledgers,
    replace_cached_sales_book,
)
from app.services.tally_masters import TallyLedger, TallySalesVoucher


COMPANY_CONFIG = {
    "company_name": "Cache Company",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "round_off_ledger_name": "Round Off",
}


def test_tally_cache_upserts_new_data_and_removes_stale_rows(db_session):
    company = add_company(db_session, "Cache Profile", COMPANY_CONFIG)
    period_start = date(2026, 4, 1)
    period_end = date(2026, 7, 16)

    replace_cached_ledgers(
        db_session,
        company.id,
        "Cache Company",
        [
            TallyLedger("Customer A", "Sundry Debtors", "-500"),
            TallyLedger("Old Ledger", "Sales Accounts", "100"),
        ],
    )
    replace_cached_sales_book(
        db_session,
        company.id,
        "Cache Company",
        period_start,
        period_end,
        [
            TallySalesVoucher("2026-07-15", "42", "Sales", "Customer A", "500", remote_id="guid-42", tally_user="tally-a"),
            TallySalesVoucher("2026-07-14", "41", "Sales", "Customer B", "250", remote_id="guid-41"),
        ],
    )

    replace_cached_ledgers(
        db_session,
        company.id,
        "Cache Company",
        [
            TallyLedger("Customer A", "Sundry Debtors", "-750"),
            TallyLedger("New Ledger", "Sales Accounts", "0"),
        ],
    )
    replace_cached_sales_book(
        db_session,
        company.id,
        "Cache Company",
        period_start,
        period_end,
        [
            TallySalesVoucher("2026-07-15", "42", "Sales", "Customer A", "600", remote_id="guid-42", tally_user="tally-b"),
        ],
    )

    ledgers = cached_ledgers(db_session, company.id, "CACHE COMPANY")
    vouchers = cached_sales_book(
        db_session,
        company.id,
        "Cache Company",
        period_start,
        period_end,
    )

    assert [(ledger.name, ledger.closing_balance) for ledger in ledgers] == [
        ("Customer A", "-750"),
        ("New Ledger", "0"),
    ]
    assert len(vouchers) == 1
    assert vouchers[0].remote_id == "guid-42"
    assert vouchers[0].amount == "600"
    assert vouchers[0].tally_user == "tally-b"
    assert db_session.scalar(select(func.count(TallyLedgerCache.id))) == 2
    assert db_session.scalar(select(func.count(TallySalesVoucherCache.id))) == 1
