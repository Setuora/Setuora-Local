from datetime import date

from app.models import BatchType, Product, SerialStatus, User
from app.routers.batches import fefo_product_options
from app.services.expiry import add_fefo_serials_to_batch, expiry_summary
from app.services.inventory import InventoryError, add_serial_to_batch, create_batch, generate_serials


def _product(code="EXP001", active=True) -> Product:
    return Product(
        product_code=code,
        product_name="Turmeric Powder",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Turmeric Powder",
        active=active,
    )


def test_generated_serials_keep_expiry_batch_metadata(db_session):
    product = _product()
    db_session.add(product)
    db_session.commit()

    serial = generate_serials(
        db_session,
        product,
        1,
        initial_status=SerialStatus.IN_STOCK,
        product_batch_number="TP24002",
        mfg_date=date(2026, 6, 15),
        expiry_date=date(2026, 7, 14),
        warehouse="WH2",
    )[0]

    summary = expiry_summary(db_session, as_of=date(2026, 6, 24))

    assert serial.product_batch_number == "TP24002"
    assert serial.expiry_date == date(2026, 7, 14)
    assert summary["widgets"]["expiring_30"] == 1
    assert summary["critical_alerts"][0]["batch"] == "TP24002"
    assert summary["critical_alerts"][0]["warehouse"] == "WH2"


def test_fefo_rejects_later_expiry_manual_sale(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = _product("EXP002")
    db_session.add_all([user, product])
    db_session.commit()
    early, late = generate_serials(
        db_session,
        product,
        2,
        initial_status=SerialStatus.IN_STOCK,
        product_batch_number="B1",
        expiry_date=date(2026, 7, 1),
    )
    late.product_batch_number = "B2"
    late.expiry_date = date(2026, 12, 1)
    db_session.commit()
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")

    try:
        add_serial_to_batch(db_session, batch, user, late.serial_number)
    except InventoryError as exc:
        assert early.serial_number in str(exc)
    else:
        assert False, "later expiry serial should be rejected while earlier stock exists"


def test_fefo_auto_pick_uses_nearest_expiry(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = _product("EXP003")
    db_session.add_all([user, product])
    db_session.commit()
    early, late = generate_serials(
        db_session,
        product,
        2,
        initial_status=SerialStatus.IN_STOCK,
        product_batch_number="EARLY",
        expiry_date=date(2026, 7, 1),
    )
    late.product_batch_number = "LATE"
    late.expiry_date = date(2026, 12, 1)
    db_session.commit()
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")

    items = add_fefo_serials_to_batch(db_session, batch, user, product.id, 1)

    assert len(items) == 1
    assert items[0].serial.serial_number == early.serial_number
    assert items[0].fefo_picked is True


def test_sale_fefo_dropdown_uses_only_active_sellable_stock(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    available = _product("AVAIL")
    returned = _product("RET")
    sold = _product("SOLD")
    inactive = _product("INACT", active=False)
    empty = _product("EMPTY")
    db_session.add_all([user, available, returned, sold, inactive, empty])
    db_session.commit()
    available_serials = generate_serials(
        db_session,
        available,
        2,
        initial_status=SerialStatus.IN_STOCK,
        product_batch_number="A1",
        expiry_date=date(2026, 7, 1),
    )
    generate_serials(db_session, returned, 1, initial_status=SerialStatus.RETURNED)
    generate_serials(db_session, sold, 1, initial_status=SerialStatus.SOLD)
    generate_serials(db_session, inactive, 1, initial_status=SerialStatus.IN_STOCK)
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db_session, batch, user, available_serials[0].serial_number)

    options = fefo_product_options(db_session, batch)

    assert {option["product_code"]: option["available_quantity"] for option in options} == {
        "AVAIL": 1,
        "RET": 1,
    }
