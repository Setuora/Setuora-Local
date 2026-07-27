from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import Company, TallyLedgerCache, User
from app.security import create_session_token
from app.services.settings import add_company, get_all_settings
from app.services.tally_access import replace_user_access
from app.services.tally_cache import replace_cached_ledgers, replace_cached_sales_book
from app.services.tally_masters import GatewayCheckResult, TallyLedger, TallySalesVoucher


COMPANY_CONFIG = {
    "company_name": "Original Tally Company",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "sales_voucher_type": "Sales",
    "purchase_voucher_type": "Purchase",
    "sales_ledger_name": "Sales Ledger",
    "purchase_ledger_name": "Purchase Ledger",
    "cgst_ledger_name": "CGST Ledger",
    "sgst_ledger_name": "SGST Ledger",
    "sales_gst_ledger_mappings": "",
    "round_off_ledger_name": "Round Off",
}


def test_tally_check_lists_company_names_and_updates_from_modal_endpoint():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        db.add(
            User(
                id=1,
                username="company-admin",
                password_hash="x",
                role="admin",
                active=True,
            )
        )
        db.commit()
        company = add_company(db, "Original Label", COMPANY_CONFIG)
        company_id = company.id

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        cookies = {SESSION_COOKIE: create_session_token(1)}
        page = client.get("/tally-check", cookies=cookies)
        visible_config = {
            key: value
            for key, value in COMPANY_CONFIG.items()
            if key not in {
                "sales_voucher_type",
                "purchase_voucher_type",
                "sales_ledger_name",
                "purchase_ledger_name",
                "cgst_ledger_name",
                "sgst_ledger_name",
            }
        }
        update = client.post(
            f"/tally-check/companies/{company_id}",
            cookies=cookies,
            headers={"Accept": "application/json"},
            data={
                **visible_config,
                "name": "Edited Label",
                "company_name": "Edited Tally Company",
            },
        )
        with (
            patch(
                "app.routers.tally_check.fetch_tally_companies",
                return_value=["Live Company", "Other Company"],
            ),
            patch(
                "app.routers.tally_check.fetch_tally_ledgers",
                return_value=[TallyLedger("Customer A", "Sundry Debtors", "-500.00")],
            ),
            patch(
                "app.routers.tally_check.fetch_tally_sales_book",
                return_value=[
                    TallySalesVoucher(
                        "2026-07-15",
                        "42",
                        "Sales",
                        "Customer A",
                        "500.00",
                        "Test sale",
                    )
                ],
            ),
            patch(
                "app.routers.tally_check.test_tally_gateway",
                return_value=GatewayCheckResult(
                    True,
                    "Tally gateway responded",
                    "<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER></ENVELOPE>",
                ),
            ),
        ):
            live_companies = client.get(
                f"/tally-check/companies/{company_id}/live/companies",
                cookies=cookies,
            )
            live_ledgers = client.get(
                f"/tally-check/companies/{company_id}/live/ledgers",
                params={"tally_company": "Live Company"},
                cookies=cookies,
            )
            live_sales = client.get(
                f"/tally-check/companies/{company_id}/live/sales-book",
                params={
                    "tally_company": "Live Company",
                    "from_date": "2026-04-01",
                    "to_date": "2026-07-15",
                },
                cookies=cookies,
            )
            cached_data = client.get(
                f"/tally-check/companies/{company_id}/cached",
                params={
                    "tally_company": "Live Company",
                    "from_date": "2026-04-01",
                    "to_date": "2026-07-15",
                },
                cookies=cookies,
            )
            gateway_check = client.post("/tally-check/test-gateway", cookies=cookies)
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        saved_company = db.get(Company, company_id)
        settings = get_all_settings(db)
        saved_name = saved_company.name
        saved_tally_name = saved_company.tally_company_name
    engine.dispose()

    assert page.status_code == 200
    assert 'data-company-open="company-modal-' in page.text
    assert 'data-tally-search-form' in page.text
    assert 'placeholder="Search companies or ledgers"' in page.text
    assert 'data-tally-company-search=' in page.text
    assert 'data-tally-master-search-row' in page.text
    assert "Original Label" in page.text
    assert "Required Tally masters" not in page.text
    assert 'name="default_party_name"' not in page.text
    assert "Mark checked" not in page.text
    assert "Unmark" not in page.text
    assert "tally-check-toggle" in page.text
    assert "Live Tally data" in page.text
    assert 'data-tally-live-company' in page.text
    assert 'data-tally-live-ledgers' in page.text
    assert 'data-tally-live-sales' in page.text
    assert 'data-auto-refresh="true"' in page.text
    assert '"/cached?"' in page.text
    assert update.status_code == 200
    assert update.json()["ok"]
    assert saved_name == "Edited Label"
    assert saved_tally_name == "Edited Tally Company"
    assert settings["sales_ledger_name"] == COMPANY_CONFIG["sales_ledger_name"]
    assert live_companies.status_code == 200
    assert live_companies.json()["companies"] == ["Live Company", "Other Company"]
    assert live_ledgers.json()["ledgers"][0]["name"] == "Customer A"
    assert live_sales.json()["vouchers"][0]["voucher_number"] == "42"
    assert cached_data.status_code == 200
    assert cached_data.json()["ledger_count"] == 1
    assert cached_data.json()["sales_count"] == 1
    assert cached_data.json()["ledgers"][0]["name"] == "Customer A"
    assert gateway_check.status_code == 200
    assert 'class="alert success"' in gateway_check.text
    assert "The configured Tally HTTP server is reachable and responding correctly." in gateway_check.text
    assert "&lt;ENVELOPE&gt;" not in gateway_check.text


def test_tally_check_enforces_user_company_ledger_and_tally_user_assignments():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        user = User(
            id=1,
            username="limited-admin",
            password_hash="x",
            role="admin",
            active=True,
        )
        db.add(user)
        db.commit()
        assigned = add_company(db, "Assigned Company", COMPANY_CONFIG)
        hidden = add_company(
            db,
            "Hidden Company",
            {**COMPANY_CONFIG, "company_name": "Hidden Tally Company"},
        )
        ledgers = [
            TallyLedger("Customer A", "Sundry Debtors", "-500"),
            TallyLedger("Customer B", "Sundry Debtors", "-250"),
        ]
        replace_cached_ledgers(db, assigned.id, "Original Tally Company", ledgers)
        replace_cached_sales_book(
            db,
            assigned.id,
            "Original Tally Company",
            date(2026, 4, 1),
            date(2026, 7, 15),
            [
                TallySalesVoucher(
                    "2026-07-15",
                    "1",
                    "Sales",
                    "Customer A",
                    "500",
                    remote_id="voucher-1",
                    tally_user="operator-a",
                ),
                TallySalesVoucher(
                    "2026-07-15",
                    "2",
                    "Sales",
                    "Customer A",
                    "250",
                    remote_id="voucher-2",
                    tally_user="operator-b",
                ),
                TallySalesVoucher(
                    "2026-07-15",
                    "3",
                    "Sales",
                    "Customer B",
                    "125",
                    remote_id="voucher-3",
                    tally_user="operator-a",
                ),
            ],
        )
        customer_a = db.scalar(
            select(TallyLedgerCache).where(
                TallyLedgerCache.company_id == assigned.id,
                TallyLedgerCache.name == "Customer A",
            )
        )
        replace_user_access(
            db,
            user,
            company_ids=[assigned.id],
            ledger_ids=[customer_a.id],
            tally_user_values=[f"{assigned.id}:operator-a"],
        )
        assigned_id = assigned.id
        hidden_id = hidden.id

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        cookies = {SESSION_COOKIE: create_session_token(1)}
        page = client.get("/tally-check", cookies=cookies)
        visible = client.get(
            f"/tally-check/companies/{assigned_id}/cached",
            params={
                "tally_company": "Original Tally Company",
                "from_date": "2026-04-01",
                "to_date": "2026-07-15",
            },
            cookies=cookies,
        )
        blocked = client.get(
            f"/tally-check/companies/{hidden_id}/cached",
            params={
                "tally_company": "Hidden Tally Company",
                "from_date": "2026-04-01",
                "to_date": "2026-07-15",
            },
            cookies=cookies,
        )
        wrong_tally_company = client.get(
            f"/tally-check/companies/{assigned_id}/cached",
            params={
                "tally_company": "A Different Tally Company",
                "from_date": "2026-04-01",
                "to_date": "2026-07-15",
            },
            cookies=cookies,
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert page.status_code == 200
    assert "Assigned Company" in page.text
    assert "Hidden Company" not in page.text
    assert visible.status_code == 200
    assert [row["name"] for row in visible.json()["ledgers"]] == ["Customer A"]
    assert [row["voucher_number"] for row in visible.json()["vouchers"]] == ["1"]
    assert blocked.status_code == 404
    assert wrong_tally_company.status_code == 403
