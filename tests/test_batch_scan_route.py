from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import BatchItem, BatchType, Product, ScanLog, Serial, SerialStatus, StorageLocation, User
from app.security import create_session_token
from app.services.inventory import add_serial_to_batch, create_batch, generate_serials


def test_camera_scan_route_adds_multiple_serials_without_restarting():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        user = User(id=1, username="admin", password_hash="x", role="admin", active=True)
        product = Product(
            product_code="DIJ",
            product_name="Test Item",
            hsn="0910",
            gst_rate=5,
            unit="Pcs",
            default_rate=100,
            tally_stock_item_name="Test Item",
        )
        location = StorageLocation(
            code="SCAN-SHELF",
            warehouse="MAIN",
            zone="A",
            section="1",
            rack="R1",
            shelf="S1",
            bin="B1",
        )
        db.add_all([user, product, location])
        db.commit()
        serials = generate_serials(db, product, 2, initial_status=SerialStatus.GENERATED)
        batch = create_batch(db, user, BatchType.PURCHASE, "dijo-test", "")
        serial_numbers = [serial.serial_number for serial in serials]
        batch_id = batch.id

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        responses = []
        shelf_responses = []
        for serial_number in serial_numbers:
            responses.append(client.post(
                f"/batches/{batch_id}/scan",
                data={"serial_number": serial_number, "scan_source": "camera"},
                headers={"Accept": "application/json"},
                cookies={SESSION_COOKIE: create_session_token(1)},
            ))
            shelf_responses.append(client.post(
                f"/batches/{batch_id}/scan",
                data={"serial_number": "SCAN-SHELF", "scan_source": "camera"},
                headers={"Accept": "application/json"},
                cookies={SESSION_COOKIE: create_session_token(1)},
            ))
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        items = db.scalars(select(BatchItem).where(BatchItem.batch_id == batch_id)).all()
    engine.dispose()

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["ok"] for response in responses] == [True, True]
    assert [response.json()["serial"] for response in responses] == serial_numbers
    assert [response.json()["scan_type"] for response in shelf_responses] == ["shelf", "shelf"]
    assert len(items) == 2


def test_sale_return_mode_removes_item_and_requires_shelf_before_submit():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        user = User(id=1, username="sales", password_hash="x", role="sales", active=True)
        product = Product(
            product_code="SALRET",
            product_name="Sale Return Item",
            hsn="0910",
            gst_rate=5,
            unit="Pcs",
            default_rate=100,
            tally_stock_item_name="Sale Return Item",
        )
        location = StorageLocation(
            code="RETURN-SHELF",
            warehouse="MAIN",
            zone="A",
            section="1",
            rack="R1",
            shelf="S1",
            bin="B1",
        )
        db.add_all([user, product, location])
        db.commit()
        serials = generate_serials(db, product, 2, initial_status=SerialStatus.IN_STOCK)
        batch = create_batch(db, user, BatchType.SALE, "Customer Ledger", "")
        add_serial_to_batch(db, batch, user, serials[0].serial_number)
        add_serial_to_batch(db, batch, user, serials[1].serial_number)
        batch_id = batch.id
        returned_serial_id = serials[0].id
        sold_serial_id = serials[1].id
        returned_serial_number = serials[0].serial_number
        second_serial_number = serials[1].serial_number
        location_id = location.id

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
        returned = client.post(
            f"/batches/{batch_id}/scan",
            data={
                "serial_number": returned_serial_number,
                "scan_source": "camera",
                "scan_mode": "return",
            },
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        blocked_scan = client.post(
            f"/batches/{batch_id}/scan",
            data={
                "serial_number": second_serial_number,
                "scan_source": "camera",
                "scan_mode": "sale",
            },
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        blocked_submit = client.post(f"/batches/{batch_id}/submit", cookies=cookies)
        shelf = client.post(
            f"/batches/{batch_id}/scan",
            data={
                "serial_number": "RETURN-SHELF",
                "scan_source": "camera",
                "scan_mode": "return",
            },
            headers={"Accept": "application/json"},
            cookies=cookies,
        )
        submitted = client.post(f"/batches/{batch_id}/submit", cookies=cookies)
    finally:
        app.dependency_overrides.clear()

    assert returned.status_code == 200
    assert returned.json()["scan_type"] == "sale_return_product"
    assert returned.json()["item_count"] == 1
    assert returned.json()["sale_return"]["return_shelf_required"] is True
    assert blocked_scan.status_code == 400
    assert "shelf QR" in blocked_scan.json()["error"]
    assert blocked_submit.status_code == 400
    assert "Sale return is incomplete" in blocked_submit.text
    assert shelf.status_code == 200
    assert shelf.json()["scan_type"] == "sale_return_shelf"
    assert shelf.json()["verified_count"] == 1
    assert shelf.json()["sale_return"]["pending_count"] == 0
    assert submitted.status_code == 303

    with Session() as db:
        items = db.scalars(select(BatchItem).where(BatchItem.batch_id == batch_id)).all()
        returned_serial = db.get(Serial, returned_serial_id)
        sold_serial = db.get(Serial, sold_serial_id)
        logs = db.scalars(select(ScanLog).where(ScanLog.batch_id == batch_id)).all()

    engine.dispose()

    assert len(items) == 1
    assert items[0].serial_id == sold_serial_id
    assert returned_serial.location_id == location_id
    assert returned_serial.status == SerialStatus.IN_STOCK.value
    assert sold_serial.status == SerialStatus.SOLD.value
    assert any(log.action == "SALE_RETURN" and log.status == "SHELF_VERIFIED" for log in logs)
    assert any(log.action == "SALE_RETURN_SHELF" and log.status == "VERIFIED" for log in logs)
