from sqlalchemy import func, select

from app.models import LabelPrintLog, Product, SerialStatus, User
from app.services.inventory import generate_serials
from app.services.label_printing import (
    DEFAULT_LABEL_LAYOUT,
    LabelPrintError,
    mark_serial_labels_printed_once,
    record_serial_label_prints,
    validate_label_layout,
)


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


def test_purchase_user_can_print_each_label_only_once(db_session):
    user = User(username="buyer", password_hash="x", role="purchase")
    product = _product()
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]

    logs = record_serial_label_prints(db_session, user, [serial.id])

    assert len(logs) == 1
    assert logs[0].copies == 1
    assert logs[0].is_reprint is False
    try:
        record_serial_label_prints(db_session, user, [serial.id])
    except LabelPrintError as exc:
        assert "already used" in str(exc)
    else:
        assert False


def test_purchase_user_cannot_request_multiple_copies(db_session):
    user = User(username="buyer", password_hash="x", role="purchase")
    product = _product()
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]

    try:
        record_serial_label_prints(db_session, user, [serial.id], copies=2)
    except LabelPrintError as exc:
        assert "only one copy" in str(exc)
    else:
        assert False


def test_admin_reprint_requires_reason_and_records_copies(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    product = _product()
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    record_serial_label_prints(db_session, user, [serial.id])

    try:
        record_serial_label_prints(db_session, user, [serial.id], copies=3)
    except LabelPrintError as exc:
        assert "reason is required" in str(exc)
    else:
        assert False

    logs = record_serial_label_prints(
        db_session,
        user,
        [serial.id],
        copies=3,
        reason="Printer jam damaged the first sheet",
        template_name="A4 – 44 Labels",
    )
    assert logs[0].is_reprint is True
    assert logs[0].copies == 3
    assert logs[0].reason == "Printer jam damaged the first sheet"
    assert logs[0].template_name == "A4 – 44 Labels"
    total = db_session.scalar(
        select(func.sum(LabelPrintLog.copies)).where(LabelPrintLog.serial_id == serial.id)
    )
    assert total == 4


def test_first_admin_multi_copy_print_is_a_reasoned_reprint(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    product = _product()
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]

    logs = record_serial_label_prints(
        db_session,
        user,
        [serial.id],
        copies=2,
        reason="Two warehouse bins require labels",
    )

    assert logs[0].is_reprint is True
    assert logs[0].copies == 2


def test_label_layout_validation_rejects_sheet_overflow_and_bad_start():
    assert validate_label_layout(DEFAULT_LABEL_LAYOUT)["columns"] == 4
    try:
        validate_label_layout({**DEFAULT_LABEL_LAYOUT, "columns": 5})
    except LabelPrintError as exc:
        assert "Layout needs" in str(exc)
    else:
        assert False


def test_invalid_copy_value_is_rejected_by_print_service(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    product = _product()
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]

    try:
        record_serial_label_prints(db_session, user, [serial.id], copies=0)
    except LabelPrintError as exc:
        assert "Copies must be" in str(exc)
    else:
        assert False

    try:
        validate_label_layout({**DEFAULT_LABEL_LAYOUT, "start_position": 45})
    except LabelPrintError as exc:
        assert "cannot exceed 44" in str(exc)
    else:
        assert False
