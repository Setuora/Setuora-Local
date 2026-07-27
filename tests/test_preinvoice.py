from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import BatchType, GstTreatment, Product, SerialStatus, User
from app.security import create_session_token
from app.services.inventory import add_serial_to_batch, create_batch, generate_serials
from app.services.preinvoice import amount_in_words, sale_preinvoice_pdf


def _product(db, code: str, gst_rate: float, rate: float) -> Product:
    product = Product(
        product_code=code,
        product_name=f"Product {code}",
        hsn=f"HSN-{code}",
        gst_rate=gst_rate,
        unit="Pcs",
        default_rate=rate,
        tally_stock_item_name=f"Tally Product {code}",
    )
    db.add(product)
    db.commit()
    return product


def test_amount_in_words_uses_indian_numbering():
    assert amount_in_words(Decimal("2766.00")) == (
        "INR Two Thousand Seven Hundred Sixty Six Only"
    )
    assert amount_in_words(Decimal("12345678.50")) == (
        "INR One Crore Twenty Three Lakh Forty Five Thousand "
        "Six Hundred Seventy Eight and Fifty Paise Only"
    )


def test_sale_preinvoice_pdf_contains_reference_and_multi_rate_totals(db_session):
    user = User(username="sales-preinvoice", password_hash="x", role="sales")
    db_session.add(user)
    db_session.commit()
    product_5 = _product(db_session, "GST5", 5, 100)
    product_18 = _product(db_session, "GST18", 18, 200)
    serial_5 = generate_serials(
        db_session,
        product_5,
        1,
        initial_status=SerialStatus.IN_STOCK,
    )[0]
    serial_18 = generate_serials(
        db_session,
        product_18,
        1,
        initial_status=SerialStatus.IN_STOCK,
    )[0]
    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "SFB524982 VENKATESH G",
        "Reference sale",
    )
    add_serial_to_batch(db_session, batch, user, serial_5.serial_number)
    add_serial_to_batch(db_session, batch, user, serial_18.serial_number)

    pdf = sale_preinvoice_pdf(
        batch,
        {"company_name": "SWARNAGOWRI 26-27"},
    )

    assert pdf.startswith(b"%PDF")
    assert b"PRE-INVOICE" in pdf
    assert b"SFB524982 VENKATESH G" in pdf
    assert b"SWARNAGOWRI 26-27" in pdf
    assert b"Product GST5" in pdf
    assert b"Product GST18" in pdf
    assert b"Not a Tax Invoice" in pdf


def test_sale_preinvoice_pdf_contains_interstate_igst_context(db_session):
    user = User(username="sales-preinvoice-igst", password_hash="x", role="sales")
    db_session.add(user)
    db_session.commit()
    product = _product(db_session, "IGST5", 5, 100)
    serial = generate_serials(
        db_session,
        product,
        1,
        initial_status=SerialStatus.IN_STOCK,
    )[0]
    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "Interstate Customer",
        "",
        party_state="Tamil Nadu",
        gst_treatment=GstTreatment.INTER_STATE.value,
        gst_igst_rate=12,
    )
    add_serial_to_batch(db_session, batch, user, serial.serial_number)

    pdf = sale_preinvoice_pdf(batch, {"company_name": "SWARNAGOWRI 26-27"})

    assert b"IGST" in pdf
    assert b"Tamil Nadu" in pdf
    assert b"10.71" in pdf


def test_preinvoice_route_is_available_only_for_nonempty_sales():
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
            username="preinvoice-admin",
            password_hash="x",
            role="admin",
            active=True,
        )
        db.add(user)
        db.commit()
        product = _product(db, "ROUTE", 5, 125)
        serial = generate_serials(
            db,
            product,
            1,
            initial_status=SerialStatus.IN_STOCK,
        )[0]
        sale = create_batch(db, user, BatchType.SALE, "Route Customer", "")
        add_serial_to_batch(db, sale, user, serial.serial_number)
        empty_sale = create_batch(db, user, BatchType.SALE, "Empty Customer", "")
        purchase = create_batch(db, user, BatchType.PURCHASE, "Supplier", "")
        sale_id = sale.id
        empty_sale_id = empty_sale.id
        purchase_id = purchase.id

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
        response = client.get(f"/batches/{sale_id}/preinvoice.pdf", cookies=cookies)
        empty_response = client.get(
            f"/batches/{empty_sale_id}/preinvoice.pdf",
            cookies=cookies,
        )
        purchase_response = client.get(
            f"/batches/{purchase_id}/preinvoice.pdf",
            cookies=cookies,
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].endswith("-preinvoice.pdf")
    assert response.content.startswith(b"%PDF")
    assert empty_response.status_code == 400
    assert purchase_response.status_code == 303
    assert purchase_response.headers["location"] == f"/batches/{purchase_id}"
