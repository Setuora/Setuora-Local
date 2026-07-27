from sqlalchemy import func, select, text
from sqlalchemy import create_engine
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import (
    InventoryTransaction,
    Product,
    RelocationSerial,
    ScanLog,
    Serial,
    SerialStatus,
    StockRelocation,
    StorageLocation,
    TransactionType,
    User,
)
from app.services.inventory import generate_serials
from app.services.relocation import MoveItem, RelocationError, relocate_stock, search_stock
from app.security import create_session_token
import app.services.schema as schema_service


def _product(code: str = "LOC001") -> Product:
    return Product(
        product_code=code,
        product_name=f"Location product {code}",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name=f"Location product {code}",
    )


def _location(code: str, bin_name: str, *, active: bool = True) -> StorageLocation:
    return StorageLocation(
        code=code,
        warehouse="WH1",
        zone="A",
        section="C",
        rack="04",
        shelf="02",
        bin=bin_name,
        active=active,
    )


def test_partial_relocation_updates_stock_and_permanent_audit_rows(db_session):
    user = User(username="auditor", password_hash="x", role="auditor")
    product = _product()
    source = _location("WH1-A-C-04-02-05", "05")
    destination = _location("WH1-A-C-04-02-06", "06")
    db_session.add_all([user, product, source, destination])
    db_session.commit()
    serials = generate_serials(
        db_session,
        product,
        3,
        initial_status=SerialStatus.IN_STOCK,
        product_batch_number="B2401",
        warehouse="WH1",
    )
    for serial in serials:
        serial.location_id = source.id
    db_session.commit()

    rows = relocate_stock(
        db_session,
        user=user,
        destination_id=destination.id,
        items=[
            MoveItem(
                product_id=product.id,
                product_batch_number="B2401",
                source_location_id=source.id,
                quantity=2,
            )
        ],
        reason="Picking optimization",
        device_used="Android device",
    )

    assert len(rows) == 1
    assert rows[0].quantity == 2
    assert rows[0].previous_location_snapshot == source.full_path
    assert rows[0].new_location_snapshot == destination.full_path
    assert db_session.scalar(select(func.count(Serial.id)).where(Serial.location_id == source.id)) == 1
    assert db_session.scalar(select(func.count(Serial.id)).where(Serial.location_id == destination.id)) == 2
    assert db_session.scalar(
        select(func.count(RelocationSerial.id)).where(RelocationSerial.relocation_id == rows[0].id)
    ) == 2
    assert db_session.scalar(
        select(func.count(InventoryTransaction.id)).where(
            InventoryTransaction.transaction_type == TransactionType.RELOCATION.value
        )
    ) == 2
    assert db_session.scalar(select(func.count(ScanLog.id)).where(ScanLog.action == "RELOCATION")) == 2


def test_bulk_relocation_is_atomic_when_one_line_is_unavailable(db_session):
    user = User(username="manager", password_hash="x", role="warehouse_manager")
    first = _product("LOC010")
    second = _product("LOC020")
    source = _location("WH1-A-C-04-02-10", "10")
    destination = _location("WH1-A-C-04-02-11", "11")
    db_session.add_all([user, first, second, source, destination])
    db_session.commit()
    first_serial = generate_serials(db_session, first, 1, initial_status=SerialStatus.IN_STOCK)[0]
    second_serial = generate_serials(db_session, second, 1, initial_status=SerialStatus.IN_STOCK)[0]
    first_serial.location_id = source.id
    second_serial.location_id = source.id
    db_session.commit()

    try:
        relocate_stock(
            db_session,
            user=user,
            destination_id=destination.id,
            items=[
                MoveItem(product_id=first.id, source_location_id=source.id, quantity=1),
                MoveItem(product_id=second.id, source_location_id=source.id, quantity=2),
            ],
            reason=None,
            device_used="Scanner",
        )
        assert False, "the full bulk move should fail"
    except RelocationError:
        pass

    db_session.expire_all()
    assert db_session.get(Serial, first_serial.id).location_id == source.id
    assert db_session.get(Serial, second_serial.id).location_id == source.id
    assert db_session.scalar(select(func.count(StockRelocation.id))) == 0


def test_inactive_or_same_destination_is_rejected(db_session):
    user = User(username="auditor2", password_hash="x", role="auditor")
    product = _product("LOC030")
    source = _location("WH1-A-C-04-02-20", "20")
    inactive = _location("WH1-A-C-04-02-21", "21", active=False)
    db_session.add_all([user, product, source, inactive])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    serial.location_id = source.id
    db_session.commit()
    item = MoveItem(product_id=product.id, source_location_id=source.id, quantity=1)

    for destination_id in (inactive.id, source.id):
        try:
            relocate_stock(
                db_session,
                user=user,
                destination_id=destination_id,
                items=[item],
                reason=None,
                device_used="Scanner",
            )
            assert False, "invalid destinations must fail"
        except RelocationError:
            pass

    db_session.expire_all()
    assert db_session.get(Serial, serial.id).location_id == source.id


def test_stock_search_supports_sku_batch_name_and_exact_serial(db_session):
    product = _product("SKU-SEARCH")
    source = _location("WH1-A-C-04-02-30", "30")
    db_session.add_all([product, source])
    db_session.commit()
    serials = generate_serials(
        db_session,
        product,
        2,
        initial_status=SerialStatus.IN_STOCK,
        product_batch_number="LOT-77",
    )
    for serial in serials:
        serial.location_id = source.id
    db_session.commit()

    assert search_stock(db_session, "SKU-SEARCH")[0]["quantity"] == 2
    assert search_stock(db_session, "LOT-77")[0]["quantity"] == 2
    assert search_stock(db_session, "Location product")[0]["quantity"] == 2
    exact = search_stock(db_session, serials[0].serial_number)
    assert exact[0]["quantity"] == 1
    assert exact[0]["serial_id"] == serials[0].id


def test_runtime_schema_makes_relocation_log_immutable(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'immutable.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(schema_service, "engine", engine)
    schema_service.ensure_runtime_schema()

    Session = sessionmaker(bind=engine)
    with Session() as db:
        user = User(username="root", password_hash="x", role="super_admin", active=True)
        product = _product("IMM")
        location = _location("IMM-1", "B")
        db.add_all([user, product, location])
        db.flush()
        db.add(
            StockRelocation(
                reference_number="MOV-IMM",
                product_id=product.id,
                quantity=1,
                new_location_id=location.id,
                previous_location_snapshot="OLD",
                new_location_snapshot="NEW",
                user_id=user.id,
                device_used="TEST",
            )
        )
        db.commit()

    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE stock_relocations SET reason = 'changed' WHERE id = 1"))
        assert False, "database trigger must reject edits"
    except DatabaseError as exc:
        assert "permanent" in str(exc).lower()
    finally:
        engine.dispose()


def test_relocation_routes_enforce_roles_and_record_android_device():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        auditor = User(id=1, username="audit-route", password_hash="x", role="auditor", active=True)
        purchase = User(id=2, username="purchase-route", password_hash="x", role="purchase", active=True)
        manager = User(id=3, username="manager-route", password_hash="x", role="warehouse_manager", active=True)
        product = _product("ROUTE")
        source = _location("ROUTE-SOURCE", "40")
        destination = _location("ROUTE-DEST", "41")
        db.add_all([auditor, purchase, manager, product, source, destination])
        db.commit()
        serial = generate_serials(db, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
        serial.location_id = source.id
        db.commit()
        product_id = product.id
        serial_id = serial.id
        source_id = source.id
        destination_id = destination.id

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        purchase_page = client.get(
            "/warehouse/move",
            cookies={SESSION_COOKIE: create_session_token(2)},
        )
        auditor_page = client.get(
            "/warehouse/move",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
        auditor_locations = client.get(
            "/warehouse/locations",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
        manager_locations = client.get(
            "/warehouse/locations",
            cookies={SESSION_COOKIE: create_session_token(3)},
        )
        moved = client.post(
            "/warehouse/relocate",
            cookies={SESSION_COOKIE: create_session_token(1)},
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120"},
            json={
                "destination_id": destination_id,
                "reason": "Route test",
                "items": [
                    {
                        "product_id": product_id,
                        "source_location_id": source_id,
                        "serial_id": serial_id,
                        "quantity": 1,
                    }
                ],
            },
        )
        history_page = client.get(
            "/warehouse/history",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
        map_page = client.get(
            "/warehouse/map",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
        location_qr = client.get(
            f"/warehouse/locations/{destination_id}/qr.png",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
        location_labels = client.get(
            f"/warehouse/locations/labels.pdf?ids={source_id},{destination_id}",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        relocation = db.scalar(select(StockRelocation))
        moved_serial = db.get(Serial, serial_id)
        assert relocation is not None
        assert relocation.device_used.startswith("Android device")
        assert moved_serial.location_id == destination_id
    engine.dispose()

    assert purchase_page.status_code == 403
    assert auditor_page.status_code == 200
    assert "Confirm relocation" in auditor_page.text
    assert auditor_locations.status_code == 403
    assert manager_locations.status_code == 200
    assert moved.status_code == 200
    assert moved.json()["ok"] is True
    assert history_page.status_code == 200
    assert "ROUTE" in history_page.text
    assert map_page.status_code == 200
    assert "ROUTE-DEST" in map_page.text
    assert location_qr.status_code == 200
    assert location_qr.headers["content-type"] == "image/png"
    assert location_labels.status_code == 200
    assert location_labels.headers["content-type"] == "application/pdf"
    assert location_labels.content.startswith(b"%PDF")
