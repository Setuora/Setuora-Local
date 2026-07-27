from datetime import date, datetime, timezone

from fastapi.testclient import TestClient
from io import BytesIO
from openpyxl import load_workbook
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import AuditFinding, Batch, BatchStatus, BatchType, InventoryTransaction, Product, Serial, SerialStatus, TransactionType, User, WarehouseLevel
from app.security import create_session_token
from app.services.stock_movement import (
    MovementConfig,
    MovementFilters,
    movement_status,
    product_inventory_metrics,
    product_sales_report_pdf,
    product_sales_transactions,
    stock_movement_pdf,
    stock_movement_rows,
    stock_movement_xlsx,
)
from app.services.settings import get_setting
from app.services import schema


def _product(code: str, name: str) -> Product:
    return Product(
        product_code=code,
        product_name=name,
        category="Furniture",
        brand="Setuora",
        hsn="9401",
        gst_rate=18,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name=name,
    )


def test_stock_movement_projects_unsold_expiry_stock(db_session):
    user = User(username="admin", password_hash="x", role="admin", active=True)
    product = _product("EXP-1", "Expiring product")
    db_session.add_all([user, product])
    db_session.flush()
    expiry = date(2026, 11, 24)  # 150 days after the analysis date.
    for index in range(8):
        db_session.add(
            Serial(
                serial_number=f"EXP-STOCK-{index}",
                product_id=product.id,
                status=SerialStatus.IN_STOCK.value,
                warehouse="C&F Bengaluru",
                warehouse_level=WarehouseLevel.C_AND_F.value,
                product_batch_number="B-100",
                expiry_date=expiry,
            )
        )
    sold = Serial(
        serial_number="EXP-SOLD-1",
        product_id=product.id,
        status=SerialStatus.SOLD.value,
        warehouse="C&F Bengaluru",
        warehouse_level=WarehouseLevel.C_AND_F.value,
        product_batch_number="B-100",
        expiry_date=expiry,
    )
    db_session.add(sold)
    db_session.flush()
    db_session.add(
        InventoryTransaction(
            transaction_type=TransactionType.SALE.value,
            serial_id=sold.id,
            product_id=product.id,
            user_id=user.id,
            serial_number=sold.serial_number,
            status_from=SerialStatus.IN_STOCK.value,
            status_to=SerialStatus.SOLD.value,
            created_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    rows, summary = stock_movement_rows(
        db_session,
        config=MovementConfig(analysis_days=30),
        filters=MovementFilters(start=date(2026, 5, 29), end=date(2026, 6, 27)),
        as_of=date(2026, 6, 27),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["current_stock"] == 8
    assert row["units_sold"] == 1
    assert row["movement_ratio"] == 12.5
    assert row["average_monthly_sales"] == 1
    assert row["estimated_months"] == 8
    assert row["franchise_level"] == "C&F"
    assert row["expiry_risk"] == "High Expiry Risk"
    assert row["estimated_unsold"] == 3
    assert summary["expiry_risk"] == 1


def test_movement_threshold_boundaries_are_configurable():
    config = MovementConfig(
        analysis_days=90,
        dead_below_pct=10,
        slow_below_pct=40,
        medium_up_to_pct=80,
    )
    assert movement_status(100, 9, 9, config) == "Dead Stock"
    assert movement_status(100, 10, 10, config) == "Slow Moving"
    assert movement_status(100, 40, 40, config) == "Medium Moving"
    assert movement_status(100, 80, 80, config) == "Medium Moving"
    assert movement_status(100, 81, 81, config) == "Fast Moving"
    assert movement_status(0, 5, None, config) == "Fast Moving"


def test_product_inventory_metrics_reports_sales_available_missing_and_restock(db_session):
    as_of = date(2026, 6, 27)
    user = User(username="admin", password_hash="x", role="admin", active=True)
    product = _product("REPORT-1", "Reported product")
    db_session.add_all([user, product])
    db_session.flush()
    stock = [
        Serial(
            serial_number=f"REPORT-STOCK-{index}",
            product_id=product.id,
            status=SerialStatus.IN_STOCK.value,
        )
        for index in range(4)
    ]
    sold = [
        Serial(
            serial_number=f"REPORT-SOLD-{index}",
            product_id=product.id,
            status=SerialStatus.SOLD.value,
        )
        for index in range(2)
    ]
    audit_batch = Batch(
        batch_number="AUD-REPORT-1",
        batch_type=BatchType.AUDIT.value,
        user_id=user.id,
        status=BatchStatus.SUBMITTED.value,
    )
    db_session.add_all([*stock, *sold, audit_batch])
    db_session.flush()
    db_session.add(
        AuditFinding(
            batch_id=audit_batch.id,
            serial_id=stock[0].id,
            serial_number=stock[0].serial_number,
            product_code=product.product_code,
            product_name=product.product_name,
            finding_type="MISSING",
            expected_status=SerialStatus.IN_STOCK.value,
            created_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
        )
    )
    for serial in sold:
        db_session.add(
            InventoryTransaction(
                transaction_type=TransactionType.SALE.value,
                serial_id=serial.id,
                product_id=product.id,
                user_id=user.id,
                serial_number=serial.serial_number,
                status_from=SerialStatus.IN_STOCK.value,
                status_to=SerialStatus.SOLD.value,
                created_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            )
        )
    db_session.commit()

    metrics, analysis_days = product_inventory_metrics(
        db_session,
        [product],
        config=MovementConfig(analysis_days=30),
        as_of=as_of,
    )

    assert analysis_days == 30
    assert metrics[product.id]["units_sold"] == 2
    assert metrics[product.id]["system_stock"] == 4
    assert metrics[product.id]["missing_stock"] == 1
    assert metrics[product.id]["available_stock"] == 3
    assert metrics[product.id]["restock_label"] == "In 45 days"
    assert metrics[product.id]["restock_detail"] == "By 11-08-2026"
    sales = product_sales_transactions(db_session, {product.id}, analysis_days, as_of=as_of)
    assert len(sales) == 2
    assert product_sales_report_pdf(
        product,
        metrics[product.id],
        sales,
        analysis_days,
        as_of=as_of,
    ).startswith(b"%PDF")


def test_product_inventory_metrics_resolves_missing_after_later_verified_audit(db_session):
    user = User(username="auditor", password_hash="x", role="admin", active=True)
    product = _product("REPORT-RESOLVE", "Resolved missing product")
    db_session.add_all([user, product])
    db_session.flush()
    serial = Serial(
        serial_number="REPORT-RESOLVE-001",
        product_id=product.id,
        status=SerialStatus.IN_STOCK.value,
    )
    first_audit = Batch(
        batch_number="AUD-RESOLVE-1",
        batch_type=BatchType.AUDIT.value,
        user_id=user.id,
        status=BatchStatus.SUBMITTED.value,
    )
    second_audit = Batch(
        batch_number="AUD-RESOLVE-2",
        batch_type=BatchType.AUDIT.value,
        user_id=user.id,
        status=BatchStatus.SUBMITTED.value,
    )
    db_session.add_all([serial, first_audit, second_audit])
    db_session.flush()
    db_session.add_all(
        [
            AuditFinding(
                batch_id=first_audit.id,
                serial_id=serial.id,
                serial_number=serial.serial_number,
                product_code=product.product_code,
                product_name=product.product_name,
                finding_type="MISSING",
                expected_status=SerialStatus.IN_STOCK.value,
                created_at=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
            ),
            AuditFinding(
                batch_id=second_audit.id,
                serial_id=serial.id,
                serial_number=serial.serial_number,
                product_code=product.product_code,
                product_name=product.product_name,
                finding_type="VERIFIED",
                expected_status=SerialStatus.IN_STOCK.value,
                scanned_status=SerialStatus.IN_STOCK.value,
                created_at=datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()

    metrics, _ = product_inventory_metrics(
        db_session,
        [product],
        config=MovementConfig(analysis_days=30),
        as_of=date(2026, 6, 27),
    )

    assert metrics[product.id]["system_stock"] == 1
    assert metrics[product.id]["missing_stock"] == 0
    assert metrics[product.id]["available_stock"] == 1


def test_stock_movement_exports_are_valid(db_session):
    user = User(username="admin", password_hash="x", role="admin", active=True)
    product = _product("DUR-1", "Office chair")
    db_session.add_all([user, product])
    db_session.flush()
    db_session.add(
        Serial(
            serial_number="DUR-STOCK-1",
            product_id=product.id,
            status=SerialStatus.IN_STOCK.value,
            warehouse="Main Warehouse",
        )
    )
    db_session.commit()
    rows, summary = stock_movement_rows(
        db_session,
        config=MovementConfig(analysis_days=30),
        filters=MovementFilters(start=date(2026, 5, 29), end=date(2026, 6, 27)),
        as_of=date(2026, 6, 27),
    )

    xlsx = stock_movement_xlsx(rows, summary)
    pdf = stock_movement_pdf(rows, summary)

    assert xlsx.startswith(b"PK")
    assert pdf.startswith(b"%PDF")
    assert rows[0]["movement_status"] == "Dead Stock"
    assert rows[0]["inventory_signal"] == "Overstocked"
    assert rows[0]["expiry_risk"] == "Not applicable"


def test_stock_movement_xlsx_exports_only_selected_fields(db_session):
    user = User(username="admin", password_hash="x", role="admin", active=True)
    product = _product("DUR-2", "Selected columns chair")
    db_session.add_all([user, product])
    db_session.flush()
    db_session.add(
        Serial(
            serial_number="DUR-STOCK-2",
            product_id=product.id,
            status=SerialStatus.IN_STOCK.value,
            warehouse="Main Warehouse",
        )
    )
    db_session.commit()
    rows, summary = stock_movement_rows(
        db_session,
        config=MovementConfig(analysis_days=30),
        filters=MovementFilters(start=date(2026, 5, 29), end=date(2026, 6, 27)),
        as_of=date(2026, 6, 27),
    )

    sheet = load_workbook(
        BytesIO(stock_movement_xlsx(rows, summary, ["Product Code", "Current Stock", "Movement Status"]))
    ).active

    assert [cell.value for cell in sheet[3]] == ["Product Code", "Current Stock", "Movement Status"]
    assert [cell.value for cell in sheet[4]] == ["DUR-2", 1, "Dead Stock"]


def test_stock_movement_page_and_exports_follow_role_access():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        admin = User(id=1, username="admin", password_hash="x", role="admin", active=True)
        manager = User(id=2, username="manager", password_hash="x", role="warehouse_manager", active=True)
        sales = User(id=3, username="sales", password_hash="x", role="sales", active=True)
        product = _product("ROUTE-1", "Route product")
        db.add_all([admin, manager, sales, product])
        db.flush()
        db.add(
            Serial(
                serial_number="ROUTE-STOCK-1",
                product_id=product.id,
                status=SerialStatus.IN_STOCK.value,
                warehouse="Main Warehouse",
            )
        )
        db.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        admin_response = client.get("/stock-movement", cookies={SESSION_COOKIE: create_session_token(1)})
        manager_response = client.get("/stock-movement", cookies={SESSION_COOKIE: create_session_token(2)})
        sales_response = client.get("/stock-movement", cookies={SESSION_COOKIE: create_session_token(3)})
        removed_csv_response = client.get(
            "/stock-movement/export.csv",
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
        save_response = client.post(
            "/stock-movement/settings",
            data={
                "analysis_days": "180",
                "dead_below_pct": "5",
                "slow_below_pct": "30",
                "medium_up_to_pct": "70",
            },
            cookies={SESSION_COOKIE: create_session_token(1)},
        )
        manager_save_response = client.post(
            "/stock-movement/settings",
            data={
                "analysis_days": "30",
                "dead_below_pct": "10",
                "slow_below_pct": "40",
                "medium_up_to_pct": "80",
            },
            cookies={SESSION_COOKIE: create_session_token(2)},
        )
        with Session() as db:
            saved_analysis_days = get_setting(db, "movement_analysis_days")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert admin_response.status_code == 200
    assert "Stock Movement" in admin_response.text
    assert "Route product" in admin_response.text
    assert "Movement classification settings" in admin_response.text
    assert manager_response.status_code == 200
    assert "Movement classification settings" not in manager_response.text
    assert sales_response.status_code == 403
    assert "export.csv" not in admin_response.text
    assert "export.xlsx" in admin_response.text
    assert "data-xlsx-export" in admin_response.text
    assert "Customize stock movement export" in admin_response.text
    assert "export.pdf" in admin_response.text
    assert removed_csv_response.status_code == 404
    assert save_response.status_code == 303
    assert saved_analysis_days == "180"
    assert manager_save_response.status_code == 403


def test_runtime_schema_adds_stock_movement_dimensions(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE batches (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE serials (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE storage_locations (id INTEGER PRIMARY KEY)"))

    monkeypatch.setattr(schema, "engine", engine)
    schema.ensure_runtime_schema()
    db_inspector = inspect(engine)

    assert "brand" in {column["name"] for column in db_inspector.get_columns("products")}
    assert "warehouse_level" in {column["name"] for column in db_inspector.get_columns("serials")}
    assert "warehouse_level" in {
        column["name"] for column in db_inspector.get_columns("storage_locations")
    }
    engine.dispose()
