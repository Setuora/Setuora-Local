from io import BytesIO

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.routers.maintenance as maintenance_router
from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import (
    Batch,
    BatchItem,
    BatchType,
    Company,
    LoginAudit,
    Product,
    RelocationSerial,
    Serial,
    Setting,
    StockRelocation,
    StorageLocation,
    User,
)
from app.security import create_session_token, hash_password, verify_password


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def seed_product(db):
    product = Product(
        product_code="SKU-1",
        product_name="Sample",
        hsn="1234",
        gst_rate=5,
        default_rate=100,
        sales_discount_rate=0,
        tally_stock_item_name="Sample",
    )
    db.add(product)
    return product


def override_db(Session):
    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    return override_get_db


def test_database_reset_is_visible_only_to_super_admin_and_route_is_protected():
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="admin", password_hash=hash_password("admin-pass"), role="admin", active=True))
        db.add(User(id=2, username="root", password_hash=hash_password("root-pass"), role="super_admin", active=True))
        db.commit()

    app.dependency_overrides[get_db] = override_db(Session)
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        admin_cookies = {SESSION_COOKIE: create_session_token(1)}
        root_cookies = {SESSION_COOKIE: create_session_token(2)}
        admin_page = client.get("/maintenance", cookies=admin_cookies)
        root_page = client.get("/maintenance", cookies=root_cookies)
        admin_reset = client.post(
            "/maintenance/reset",
            cookies=admin_cookies,
            data={"super_admin_password": "admin-pass", "confirm_reset": "RESET"},
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert admin_page.status_code == 200
    assert root_page.status_code == 200
    assert 'action="/maintenance/reset"' not in admin_page.text
    assert 'action="/maintenance/reset"' in root_page.text
    assert 'action="/maintenance/restore-upload"' not in admin_page.text
    assert 'action="/maintenance/restore-upload"' in root_page.text
    assert 'action="/maintenance/backup-settings"' in root_page.text
    assert admin_reset.status_code == 403


def test_database_reset_requires_password_and_confirmation_before_clearing_data():
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="root", password_hash=hash_password("root-pass"), role="super_admin", active=True))
        seed_product(db)
        db.add(Setting(key="company_name", value="Custom Company"))
        db.commit()

    app.dependency_overrides[get_db] = override_db(Session)
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        cookies = {SESSION_COOKIE: create_session_token(1)}
        bad_confirmation = client.post(
            "/maintenance/reset",
            cookies=cookies,
            data={"super_admin_password": "root-pass", "confirm_reset": "reset"},
        )
        bad_password = client.post(
            "/maintenance/reset",
            cookies=cookies,
            data={"super_admin_password": "wrong-pass", "confirm_reset": "RESET"},
        )
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        product_count = db.scalar(select(func.count(Product.id)))
        login_audit = db.scalar(select(LoginAudit).where(LoginAudit.username == "root"))
    engine.dispose()

    assert bad_confirmation.status_code == 303
    assert bad_confirmation.headers["location"] == "/maintenance?error=confirm_required"
    assert bad_password.status_code == 303
    assert bad_password.headers["location"] == "/maintenance?error=bad_password"
    assert product_count == 1
    assert login_audit is not None
    assert login_audit.message == "Database reset password verification failed"


def test_super_admin_database_reset_clears_database_and_preserves_current_super_admin():
    engine, Session = make_session()
    root_hash = hash_password("root-pass")
    with Session() as db:
        db.add(User(id=1, username="root", password_hash=root_hash, role="super_admin", active=True))
        db.add(User(id=2, username="staff", password_hash=hash_password("staff-pass"), role="sales", active=True))
        product = seed_product(db)
        db.flush()
        serial = Serial(serial_number="SER-1", product_id=product.id)
        batch = Batch(batch_number="B-1", batch_type=BatchType.SALE.value, user_id=2)
        db.add_all([serial, batch])
        db.flush()
        db.add(BatchItem(batch_id=batch.id, serial_id=serial.id))
        db.add(Company(name="Custom", config='{"company_name":"Custom"}', is_active=True))
        db.add(Setting(key="company_name", value="Custom Company"))
        db.add(LoginAudit(username="staff", success=True, message="OK"))
        db.commit()

    app.dependency_overrides[get_db] = override_db(Session)
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        response = client.post(
            "/maintenance/reset",
            cookies={SESSION_COOKIE: create_session_token(1)},
            data={"super_admin_password": "root-pass", "confirm_reset": "RESET"},
        )
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        root = db.get(User, 1)
        staff = db.get(User, 2)
        product_count = db.scalar(select(func.count(Product.id)))
        serial_count = db.scalar(select(func.count(Serial.id)))
        batch_count = db.scalar(select(func.count(Batch.id)))
        batch_item_count = db.scalar(select(func.count(BatchItem.id)))
        company_count = db.scalar(select(func.count(Company.id)))
        login_audit_count = db.scalar(select(func.count(LoginAudit.id)))
        company_name = db.get(Setting, "company_name")
        tally_enabled = db.get(Setting, "tally_enabled")
    engine.dispose()

    assert response.status_code == 303
    assert response.headers["location"] == "/maintenance?success=database_reset"
    assert root is not None
    assert root.active is True
    assert verify_password("root-pass", root.password_hash)
    assert staff is None
    assert product_count == 0
    assert serial_count == 0
    assert batch_count == 0
    assert batch_item_count == 0
    assert company_count == 0
    assert login_audit_count == 0
    assert company_name is not None
    assert company_name.value == ""
    assert tally_enabled is not None
    assert tally_enabled.value == "false"


def test_database_reset_clears_relocation_history_despite_delete_guards():
    engine, Session = make_session()
    root_hash = hash_password("root-pass")
    with Session() as db:
        root = User(id=1, username="root", password_hash=root_hash, role="super_admin", active=True)
        db.add(root)
        product = seed_product(db)
        location = StorageLocation(
            code="MAIN-A-1-1-1-1",
            warehouse="MAIN",
            zone="A",
            section="1",
            rack="1",
            shelf="1",
            bin="1",
        )
        db.add(location)
        db.flush()
        serial = Serial(serial_number="SER-1", product_id=product.id, location_id=location.id)
        relocation = StockRelocation(
            reference_number="MOVE-1",
            product_id=product.id,
            quantity=1,
            previous_location_id=None,
            new_location_id=location.id,
            previous_location_snapshot="",
            new_location_snapshot=location.full_path,
            user_id=root.id,
            device_used="test",
        )
        db.add_all([serial, relocation])
        db.flush()
        db.add(RelocationSerial(relocation_id=relocation.id, serial_id=serial.id))
        db.commit()

    with engine.begin() as connection:
        for trigger_name, table_name in {
            "prevent_relocation_serials_delete": "relocation_serials",
            "prevent_stock_relocations_delete": "stock_relocations",
        }.items():
            connection.execute(
                text(
                    f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE DELETE ON {table_name}
                    BEGIN
                        SELECT RAISE(ABORT, 'Relocation history is permanent');
                    END
                    """
                )
            )

    app.dependency_overrides[get_db] = override_db(Session)
    try:
        response = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"}).post(
            "/maintenance/reset",
            cookies={SESSION_COOKIE: create_session_token(1)},
            data={"super_admin_password": "root-pass", "confirm_reset": "RESET"},
        )
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        relocation_count = db.scalar(select(func.count(StockRelocation.id)))
        relocation_serial_count = db.scalar(select(func.count(RelocationSerial.id)))
        trigger_names = set(
            db.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE 'prevent_%_delete'"
                )
            ).scalars()
        )
    engine.dispose()

    assert response.status_code == 303
    assert response.headers["location"] == "/maintenance?success=database_reset"
    assert relocation_count == 0
    assert relocation_serial_count == 0
    assert "prevent_relocation_serials_delete" in trigger_names
    assert "prevent_stock_relocations_delete" in trigger_names


def test_restore_upload_rejects_invalid_backup_file():
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="root", password_hash=hash_password("root-pass"), role="super_admin", active=True))
        db.commit()

    app.dependency_overrides[get_db] = override_db(Session)
    try:
        response = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"}).post(
            "/maintenance/restore-upload",
            cookies={SESSION_COOKIE: create_session_token(1)},
            data={"super_admin_password": "root-pass", "confirm_restore": "IMPORT"},
            files={"upload": ("bad.db", BytesIO(b"not sqlite"), "application/octet-stream")},
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 303
    assert response.headers["location"] == "/maintenance?error=restore_failed"


def test_download_backup_failure_redirects_to_maintenance(monkeypatch):
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="root", password_hash=hash_password("root-pass"), role="super_admin", active=True))
        db.commit()

    def fail_backup():
        raise RuntimeError("backup failed")

    monkeypatch.setattr(maintenance_router, "create_sqlite_backup", fail_backup)
    app.dependency_overrides[get_db] = override_db(Session)
    try:
        response = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"}).get(
            "/maintenance/backup.db",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 303
    assert response.headers["location"] == "/maintenance?error=backup_failed"
