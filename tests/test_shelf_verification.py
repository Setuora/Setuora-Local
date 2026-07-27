from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import Batch, BatchItem, BatchStatus, BatchType, Product, Role, ScanLog, Serial, SerialStatus, StorageLocation, User
from app.security import create_session_token
from app.services.inventory import create_batch, generate_serials
from app.services import schema as schema_service


def test_runtime_schema_upgrades_existing_products_to_required_interval(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE batches (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO products (id) VALUES (1)"))

    monkeypatch.setattr(schema_service, "engine", engine)
    schema_service.ensure_runtime_schema()

    with engine.connect() as connection:
        interval = connection.execute(
            text("SELECT shelf_verification_interval FROM products WHERE id = 1")
        ).scalar_one()
    assert interval == 1
    engine.dispose()


def test_admin_sets_shelf_verification_interval_on_products_page():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        db.add(User(id=1, username="admin", password_hash="x", role=Role.ADMIN.value, active=True))
        product = Product(
            product_code="POLICY",
            product_name="Policy Item",
            hsn="0910",
            gst_rate=5,
            unit="Pcs",
            default_rate=50,
            tally_stock_item_name="Policy Item",
        )
        db.add(product)
        db.commit()
        product_id = product.id

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    cookies = {SESSION_COOKIE: create_session_token(1)}
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        page = client.get("/products", cookies=cookies)
        update = client.post(
            f"/products/{product_id}/pricing",
            data={
                "default_rate": "50",
                "sales_discount_rate": "0",
                "shelf_verification_interval": "6",
            },
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        rejected_zero = client.post(
            f"/products/{product_id}/pricing",
            data={
                "default_rate": "50",
                "sales_discount_rate": "0",
                "shelf_verification_interval": "0",
            },
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Shelf QR every N scans" in page.text
    assert f'id="product-modal-{product_id}"' in page.text
    assert f'data-product-open="product-modal-{product_id}"' in page.text
    assert "Details &amp; pricing" not in page.text
    assert "<th>Category / brand</th>" in page.text
    assert 'name="shelf_verification_interval" type="number" min="1"' in page.text
    assert "0 = off" not in page.text
    assert update.status_code == 200
    assert update.json()["ok"] is True
    assert update.json()["product"]["shelf_verification_interval"] == 6
    assert rejected_zero.status_code == 400
    assert "between 1 and 1000" in rejected_zero.json()["error"]
    with Session() as db:
        assert db.get(Product, product_id).shelf_verification_interval == 6
    engine.dispose()


def test_purchase_requires_shelf_qr_at_interval_and_before_submit():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        user = User(id=1, username="admin", password_hash="x", role=Role.ADMIN.value, active=True)
        product = Product(
            product_code="SHELF",
            product_name="Shelf Controlled Item",
            hsn="0910",
            gst_rate=5,
            unit="Pcs",
            default_rate=100,
            shelf_verification_interval=2,
            tally_stock_item_name="Shelf Controlled Item",
        )
        location = StorageLocation(
            code="LOC-A-01",
            warehouse="MAIN",
            zone="A",
            section="1",
            rack="R1",
            shelf="S1",
            bin="B1",
            active=True,
        )
        db.add_all([user, product, location])
        db.commit()
        serials = generate_serials(db, product, 3, initial_status=SerialStatus.GENERATED)
        batch = create_batch(db, user, BatchType.PURCHASE, "Supplier", "")
        batch_id = batch.id
        batch_number = batch.batch_number
        location_id = location.id
        serial_numbers = [serial.serial_number for serial in serials]

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    cookies = {SESSION_COOKIE: create_session_token(1)}
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})

        first = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": serial_numbers[0], "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        second = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": serial_numbers[1], "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        blocked = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": serial_numbers[2], "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        incomplete = client.post(f"/batches/{batch_id}/submit", cookies=cookies)
        dashboard_pending = client.get("/", cookies=cookies)
        shelf = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": "LOC-A-01", "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        third = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": serial_numbers[2], "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        final_incomplete = client.post(f"/batches/{batch_id}/submit", cookies=cookies)
        final_shelf = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": "LOC-A-01", "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        submitted = client.post(f"/batches/{batch_id}/submit", cookies=cookies)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["pending_count"] == 1
    assert first.json()["shelf_required"] is False
    assert second.json()["shelf_required"] is True
    assert blocked.status_code == 400
    assert blocked.json()["shelf_required"] is True
    assert incomplete.status_code == 400
    assert "Purchase is incomplete" in incomplete.text
    assert "Shelf verification alerts" in dashboard_pending.text
    assert batch_number in dashboard_pending.text
    assert shelf.json()["scan_type"] == "shelf"
    assert shelf.json()["verified_count"] == 2
    assert third.json()["pending_count"] == 1
    assert final_incomplete.status_code == 400
    assert final_shelf.json()["verified_count"] == 1
    assert submitted.status_code == 303

    with Session() as db:
        items = db.scalars(select(BatchItem).where(BatchItem.batch_id == batch_id)).all()
        saved_serials = db.scalars(select(Serial).where(Serial.serial_number.in_(serial_numbers))).all()
        saved_batch = db.get(Batch, batch_id)
        assert all(item.shelf_location_id == location_id for item in items)
        assert all(item.shelf_verified_at is not None for item in items)
        assert all(serial.location_id == location_id for serial in saved_serials)
        assert saved_batch.status == BatchStatus.PENDING_SYNC.value
    engine.dispose()


def test_auditor_can_verify_shelf_and_location_mismatch_is_logged():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        auditor = User(id=1, username="auditor", password_hash="x", role=Role.AUDITOR.value, active=True)
        product = Product(
            product_code="AUD-SHELF",
            product_name="Audited Shelf Item",
            hsn="0910",
            gst_rate=5,
            unit="Pcs",
            shelf_verification_interval=1,
            tally_stock_item_name="Audited Shelf Item",
        )
        expected = StorageLocation(
            code="LOC-EXPECTED",
            warehouse="MAIN",
            zone="A",
            section="1",
            rack="R1",
            shelf="S1",
            bin="B1",
            active=True,
        )
        actual = StorageLocation(
            code="LOC-ACTUAL",
            warehouse="MAIN",
            zone="A",
            section="1",
            rack="R1",
            shelf="S2",
            bin="B1",
            active=True,
        )
        db.add_all([auditor, product, expected, actual])
        db.commit()
        serial = Serial(
            serial_number="AUD-SHELF-000001",
            product_id=product.id,
            status=SerialStatus.IN_STOCK.value,
            location_id=expected.id,
        )
        db.add(serial)
        db.commit()
        batch = create_batch(db, auditor, BatchType.AUDIT, "Cycle count", "")
        batch_id = batch.id
        actual_id = actual.id

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    cookies = {SESSION_COOKIE: create_session_token(1)}
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        product_scan = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": "AUD-SHELF-000001", "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        shelf_scan = client.post(
            f"/batches/{batch_id}/scan",
            data={"serial_number": "LOC-ACTUAL", "scan_source": "camera"},
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
    finally:
        app.dependency_overrides.clear()

    assert product_scan.status_code == 200
    assert product_scan.json()["shelf_required"] is True
    assert shelf_scan.status_code == 200
    assert shelf_scan.json()["pending_count"] == 0

    with Session() as db:
        serial = db.scalar(select(Serial).where(Serial.serial_number == "AUD-SHELF-000001"))
        shelf_log = db.scalar(
            select(ScanLog).where(
                ScanLog.batch_id == batch_id,
                ScanLog.action == "SHELF_VERIFY",
            )
        )
        assert serial.location_id == actual_id
        assert shelf_log.status == "MISMATCH"
        assert "1 audit location mismatch" in shelf_log.message
    engine.dispose()
