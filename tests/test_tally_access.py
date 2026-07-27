from sqlalchemy import select

from app.models import TallyLedgerCache, User
from app.services.settings import add_company
from app.services.tally_access import (
    can_access_company,
    filter_ledgers,
    filter_sales_vouchers,
    replace_user_access,
    scoped_companies,
)
from app.services.tally_cache import replace_cached_ledgers
from app.services.tally_masters import TallyLedger, TallySalesVoucher


COMPANY_CONFIG = {
    "company_name": "Tally Company",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "round_off_ledger_name": "Round Off",
}


def test_user_tally_access_is_unrestricted_until_assignments_are_saved(db_session):
    user = User(username="sales-user", password_hash="x", role="sales", active=True)
    db_session.add(user)
    db_session.commit()
    first = add_company(db_session, "First", COMPANY_CONFIG)
    second = add_company(
        db_session,
        "Second",
        {**COMPANY_CONFIG, "company_name": "Second Tally Company"},
    )

    assert [company.id for company in scoped_companies(db_session, user)] == [first.id, second.id]
    assert can_access_company(db_session, user, first.id)
    assert can_access_company(db_session, user, second.id)

    replace_user_access(
        db_session,
        user,
        company_ids=[second.id],
        ledger_ids=[],
        tally_user_values=[],
    )

    assert [company.id for company in scoped_companies(db_session, user)] == [second.id]
    assert not can_access_company(db_session, user, first.id)
    assert can_access_company(db_session, user, second.id)


def test_ledger_and_tally_user_assignments_filter_tally_results(db_session):
    user = User(username="limited", password_hash="x", role="sales", active=True)
    db_session.add(user)
    db_session.commit()
    company = add_company(db_session, "Limited", COMPANY_CONFIG)
    ledgers = [
        TallyLedger("Customer A", "Sundry Debtors", "-500"),
        TallyLedger("Customer B", "Sundry Debtors", "-250"),
    ]
    replace_cached_ledgers(db_session, company.id, "Tally Company", ledgers)
    ledger_rows = db_session.scalars(select(TallyLedgerCache)).all()
    customer_a = next(row for row in ledger_rows if row.name == "Customer A")

    replace_user_access(
        db_session,
        user,
        company_ids=[],
        ledger_ids=[customer_a.id],
        tally_user_values=[f"{company.id}:operator-a"],
    )
    vouchers = [
        TallySalesVoucher(
            "2026-07-15",
            "1",
            "Sales",
            "Customer A",
            "500",
            tally_user="operator-a",
        ),
        TallySalesVoucher(
            "2026-07-15",
            "2",
            "Sales",
            "Customer A",
            "250",
            tally_user="operator-b",
        ),
        TallySalesVoucher(
            "2026-07-15",
            "3",
            "Sales",
            "Customer B",
            "125",
            tally_user="operator-a",
        ),
    ]

    assert [ledger.name for ledger in filter_ledgers(db_session, user, company.id, ledgers)] == [
        "Customer A"
    ]
    assert [
        voucher.voucher_number
        for voucher in filter_sales_vouchers(db_session, user, company.id, vouchers)
    ] == ["1"]


def test_super_admin_tally_access_remains_unrestricted(db_session):
    user = User(username="root", password_hash="x", role="super_admin", active=True)
    db_session.add(user)
    db_session.commit()
    company = add_company(db_session, "Admin Company", COMPANY_CONFIG)

    assert can_access_company(db_session, user, company.id)
    try:
        replace_user_access(
            db_session,
            user,
            company_ids=[company.id],
            ledger_ids=[],
            tally_user_values=[],
        )
    except ValueError as exc:
        assert "unrestricted" in str(exc)
    else:  # pragma: no cover - explicit guard
        raise AssertionError("Expected super-admin assignment to be rejected")
