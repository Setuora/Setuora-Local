import inspect
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import ChangeAudit, Company, User
from app.routers.settings import autosave_settings, validate_settings
from app.security import create_session_token
from app.services.settings import (
    COMPANY_SETTING_KEYS,
    LEGACY_PLACEHOLDER_SETTINGS,
    activate_company,
    add_company,
    clear_legacy_placeholder_settings,
    company_config,
    ensure_company_records,
    ensure_default_settings,
    get_active_company,
    get_all_settings,
    parse_sales_gst_ledger_mappings,
    persist_settings_and_active_company,
    save_active_company_config,
    update_company,
    update_settings,
)


VALID_SETTINGS = {
    "company_name": "Setuora Test Company",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "sales_voucher_type": "Sales",
    "purchase_voucher_type": "Purchase",
    "sales_ledger_name": "Sales Ledger",
    "purchase_ledger_name": "Purchase Ledger",
    "cgst_ledger_name": "CGST Ledger",
    "sgst_ledger_name": "SGST Ledger",
    "round_off_ledger_name": "Round Off",
    "retry_interval_seconds": "180",
}


def _seed(db):
    ensure_default_settings(db)
    update_settings(db, VALID_SETTINGS)
    ensure_company_records(db)


def _valid_request(db):
    settings = get_all_settings(db)
    requested = {key: settings[key] for key in COMPANY_SETTING_KEYS}
    requested["retry_interval_seconds"] = settings["retry_interval_seconds"]
    return requested


def test_autosave_endpoint_cannot_change_tally_enabled():
    params = inspect.signature(autosave_settings).parameters
    assert "tally_enabled" not in params


def test_autosave_persists_fields_and_mirrors_active_company(db_session):
    _seed(db_session)
    assert get_all_settings(db_session)["tally_enabled"] == "false"

    requested = _valid_request(db_session)
    requested["sales_ledger_name"] = "Sales @ 12%"
    requested["retry_interval_seconds"] = "200"
    assert validate_settings(requested) is None

    update_settings(db_session, requested)
    save_active_company_config(db_session, requested)

    saved = get_all_settings(db_session)
    assert saved["sales_ledger_name"] == "Sales @ 12%"
    assert saved["retry_interval_seconds"] == "200"
    assert saved["tally_enabled"] == "false"

    active = get_active_company(db_session)
    assert json.loads(active.config)["sales_ledger_name"] == "Sales @ 12%"


def test_autosave_route_records_settings_audit():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(User(id=1, username="admin", password_hash="x", role="admin", active=True))
        _seed(db)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with Session() as db:
            requested = _valid_request(db)
        requested["sales_ledger_name"] = "Audited Sales Ledger"
        response = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"}).post(
            "/settings/autosave",
            data=requested,
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    with Session() as db:
        audit = db.scalar(select(ChangeAudit).where(ChangeAudit.entity_type == "settings"))
        assert audit is not None
        assert audit.action == "autosave"
        assert "Audited Sales Ledger" in (audit.after_json or "")
    engine.dispose()


def test_settings_routes_preserve_removed_fields_when_omitted():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(User(id=1, username="admin", password_hash="x", role="admin", active=True))
        _seed(db)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    visible_fields = {
        "company_name": "Updated Tally Company",
        "tally_host": "127.0.0.1",
        "tally_port": "9000",
        "sales_gst_ledger_mappings": "",
        "round_off_ledger_name": "Round Off",
        "retry_interval_seconds": "180",
    }
    cookies = {SESSION_COOKIE: create_session_token(1)}
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        autosave = client.post("/settings/autosave", data=visible_fields, cookies=cookies)
        save = client.post("/settings", data=visible_fields, cookies=cookies)
    finally:
        app.dependency_overrides.clear()

    assert autosave.status_code == 200
    assert autosave.json()["ok"]
    assert save.status_code == 303
    with Session() as db:
        saved = get_all_settings(db)
        for key in (
            "sales_voucher_type",
            "purchase_voucher_type",
            "sales_ledger_name",
            "purchase_ledger_name",
            "cgst_ledger_name",
            "sgst_ledger_name",
        ):
            assert saved[key] == VALID_SETTINGS[key]
    engine.dispose()


def test_create_company_without_legacy_tally_fields_inherits_current_values():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(User(id=1, username="admin", password_hash="x", role="admin", active=True))
        _seed(db)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"}).post(
            "/settings/companies",
            data={
                "name": "Second Profile",
                "company_name": "Second Tally Company",
                "tally_host": "192.0.2.10",
                "tally_port": "9000",
                "sales_gst_ledger_mappings": "",
                "round_off_ledger_name": "Round Off",
            },
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    with Session() as db:
        company = db.scalar(select(Company).where(Company.name == "Second Profile"))
        saved = company_config(company)
        for key in (
            "sales_voucher_type",
            "purchase_voucher_type",
            "sales_ledger_name",
            "purchase_ledger_name",
            "cgst_ledger_name",
            "sgst_ledger_name",
        ):
            assert saved[key] == VALID_SETTINGS[key]
    engine.dispose()


def test_settings_and_active_company_update_can_roll_back_together(db_session):
    _seed(db_session)
    requested = _valid_request(db_session)
    requested["sales_ledger_name"] = "Uncommitted Sales"
    requested["retry_interval_seconds"] = "240"

    persist_settings_and_active_company(db_session, requested, commit=False)
    db_session.rollback()

    saved = get_all_settings(db_session)
    assert saved["sales_ledger_name"] == VALID_SETTINGS["sales_ledger_name"]
    assert saved["retry_interval_seconds"] == VALID_SETTINGS["retry_interval_seconds"]
    active = get_active_company(db_session)
    assert json.loads(active.config)["sales_ledger_name"] == VALID_SETTINGS["sales_ledger_name"]


def test_autosave_validation_rejects_bad_port_and_interval(db_session):
    _seed(db_session)
    bad_port = _valid_request(db_session)
    bad_port["tally_port"] = "99999"
    assert validate_settings(bad_port) is not None

    bad_interval = _valid_request(db_session)
    bad_interval["retry_interval_seconds"] = "5"
    assert validate_settings(bad_interval) is not None


def test_sales_gst_ledger_mappings_are_normalized_and_validated(db_session):
    mappings = parse_sales_gst_ledger_mappings(
        "5.00 | Sales @ 5% | CGST @ 2.5% | SGST @ 2.5% | IGST @ 5%\n"
        "18 | Sales @ 18% | CGST @ 9% | SGST @ 9% | IGST @ 18%"
    )
    assert mappings["5"]["sales"] == "Sales @ 5%"
    assert mappings["18"]["cgst"] == "CGST @ 9%"
    assert mappings["18"]["igst"] == "IGST @ 18%"

    legacy = parse_sales_gst_ledger_mappings(
        "5 | Sales @ 5% | CGST @ 2.5% | SGST @ 2.5%"
    )
    assert legacy["5"]["igst"] == ""

    _seed(db_session)
    duplicate = _valid_request(db_session)
    duplicate["sales_gst_ledger_mappings"] = (
        "5 | Sales A | CGST A | SGST A | IGST A\n"
        "5.0 | Sales B | CGST B | SGST B | IGST B"
    )
    assert "more than once" in validate_settings(duplicate)

    incomplete = _valid_request(db_session)
    incomplete["sales_gst_ledger_mappings"] = "5 | Sales | CGST | SGST |"
    assert "empty ledger name" in validate_settings(incomplete)


def test_activating_older_company_profile_clears_gst_ledger_mappings(db_session):
    _seed(db_session)
    update_settings(
        db_session,
        {
            "sales_gst_ledger_mappings": (
                "5 | Sales @ 5% | Output CGST @ 2.5% | Output SGST @ 2.5%"
            )
        },
    )
    older_config = {
        key: value
        for key, value in VALID_SETTINGS.items()
        if key in COMPANY_SETTING_KEYS and key != "sales_gst_ledger_mappings"
    }
    older_config["company_name"] = "Older Company"
    older_company = Company(name="Older Company", config=json.dumps(older_config), is_active=False)
    db_session.add(older_company)
    db_session.commit()

    activate_company(db_session, older_company.id)

    assert get_all_settings(db_session)["sales_gst_ledger_mappings"] == ""


def test_update_company_updates_active_settings_but_isolates_inactive_profile(db_session):
    _seed(db_session)
    active = get_active_company(db_session)
    active_config = company_config(active)
    active_config["sales_ledger_name"] = "Active Sales @ 5%"

    update_company(db_session, active.id, "Active Label", active_config)

    assert get_active_company(db_session).name == "Active Label"
    assert get_all_settings(db_session)["sales_ledger_name"] == "Active Sales @ 5%"

    inactive_config = {key: active_config.get(key, "") for key in COMPANY_SETTING_KEYS}
    inactive_config["company_name"] = "Inactive Tally Company"
    inactive = add_company(db_session, "Inactive Label", inactive_config)
    inactive_config["sales_ledger_name"] = "Inactive Sales @ 18%"

    update_company(db_session, inactive.id, "Edited Inactive Label", inactive_config)

    assert company_config(inactive)["sales_ledger_name"] == "Inactive Sales @ 18%"
    assert get_all_settings(db_session)["sales_ledger_name"] == "Active Sales @ 5%"


def test_legacy_placeholder_settings_are_cleared_when_sync_is_disabled(db_session):
    ensure_default_settings(db_session)
    update_settings(db_session, {**LEGACY_PLACEHOLDER_SETTINGS, "tally_enabled": "false"})
    db_session.add(
        Company(
            name=LEGACY_PLACEHOLDER_SETTINGS["company_name"],
            config=json.dumps({key: LEGACY_PLACEHOLDER_SETTINGS[key] for key in COMPANY_SETTING_KEYS}),
            is_active=True,
        )
    )
    db_session.commit()

    clear_legacy_placeholder_settings(db_session)

    settings = get_all_settings(db_session)
    assert settings["company_name"] == ""
    assert settings["sales_ledger_name"] == ""
    assert get_active_company(db_session) is None
