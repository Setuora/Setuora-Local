from app.models import Product, SerialStatus, User
from app.services.inventory import generate_serials
from app.services.label_printing import LabelPrintError, mark_serial_labels_printed_once


def _product() -> Product:
    return Product(
        product_code="SG090",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )


def test_mark_serial_labels_printed_once_sets_print_metadata(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    product = _product()
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]

    mark_serial_labels_printed_once(db_session, user, [serial.id])
    db_session.refresh(serial)

    assert serial.label_printed_at is not None
    assert serial.label_printed_by_id == user.id


def test_mark_serial_labels_printed_once_rejects_second_use(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    product = _product()
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    mark_serial_labels_printed_once(db_session, user, [serial.id])

    try:
        mark_serial_labels_printed_once(db_session, user, [serial.id])
    except LabelPrintError as exc:
        assert "already used" in str(exc)
    else:
        assert False
