from sqlalchemy import func, select

from app.models import BatchType, InventoryTransaction, Product, Serial, SerialStatus, TransactionType, User, WarehouseLevel
from app.services import replacement as replacement_service
from app.services.inventory import InventoryError, add_serial_to_batch, create_batch, generate_serials
from app.services.replacement import replace_qr_serial


def test_replace_qr_serial_links_old_and_new(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    product = Product(
        product_code="SG070",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    old = generate_serials(
        db_session,
        product,
        1,
        initial_status=SerialStatus.IN_STOCK,
        product_batch_number="B-070",
        warehouse="CF-BLR",
        warehouse_level=WarehouseLevel.C_AND_F.value,
    )[0]
    replacement = replace_qr_serial(db_session, user, old.serial_number, None, "Damaged label")
    db_session.refresh(old)
    assert old.status == SerialStatus.INVALID.value
    assert not old.active
    assert old.replaced_by_id == replacement.id
    assert replacement.status == SerialStatus.IN_STOCK.value
    assert replacement.product_batch_number == "B-070"
    assert replacement.warehouse == "CF-BLR"
    assert replacement.warehouse_level == WarehouseLevel.C_AND_F.value
    transactions = db_session.scalars(select(InventoryTransaction).order_by(InventoryTransaction.id)).all()
    assert [txn.transaction_type for txn in transactions] == [TransactionType.QR_REPLACEMENT.value] * 2
    assert transactions[0].status_to == SerialStatus.INVALID.value


def test_replace_unassigned_qr_remains_unassigned_and_locks_old_qr(db_session):
    user = User(username="unassigned-admin", password_hash="x", role="admin")
    product = Product(
        product_code="SG073",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    old = generate_serials(db_session, product, 1, initial_status=SerialStatus.GENERATED)[0]

    replacement = replace_qr_serial(db_session, user, old.serial_number, "SG073-MANUAL-001", "Damaged label")
    db_session.refresh(old)

    assert replacement.status == SerialStatus.GENERATED.value
    assert replacement.display_status == "UNASSIGNED"
    assert old.status == SerialStatus.INVALID.value
    assert old.active is False

    batch = create_batch(db_session, user, BatchType.PURCHASE, "Supplier", "")
    try:
        add_serial_to_batch(db_session, batch, user, old.serial_number)
    except InventoryError as exc:
        assert "inactive" in str(exc)
    else:
        assert False, "old replacement QR must not be accepted in a transaction"


def test_replace_damaged_qr_creates_normal_in_stock_replacement(db_session):
    user = User(username="damaged-admin", password_hash="x", role="admin")
    product = Product(
        product_code="SG074",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    old = generate_serials(db_session, product, 1, initial_status=SerialStatus.DAMAGED)[0]

    replacement = replace_qr_serial(db_session, user, old.serial_number, None, "Damaged label")
    db_session.refresh(old)

    assert old.status == SerialStatus.INVALID.value
    assert old.active is False
    assert replacement.status == SerialStatus.IN_STOCK.value


def test_replace_qr_serial_rejects_non_stock_statuses(db_session):
    user = User(username="sold-admin", password_hash="x", role="admin")
    product = Product(
        product_code="SG075",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    old = generate_serials(db_session, product, 1, initial_status=SerialStatus.SOLD)[0]

    try:
        replace_qr_serial(db_session, user, old.serial_number)
    except InventoryError as exc:
        assert "unassigned or in-stock" in str(exc)
    else:
        assert False, "sold stock must not be returned to inventory by QR replacement"

    db_session.refresh(old)
    assert old.status == SerialStatus.SOLD.value
    assert old.active is True


def test_replace_qr_serial_rejects_replaced_serial(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    product = Product(
        product_code="SG071",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    old = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    replace_qr_serial(db_session, user, old.serial_number)
    try:
        replace_qr_serial(db_session, user, old.serial_number)
    except InventoryError as exc:
        assert "already" in str(exc)
    else:
        assert False


def test_auto_replacement_rolls_back_generated_serial_when_history_fails(db_session, monkeypatch):
    user = User(username="atomic-admin", password_hash="x", role="admin")
    product = Product(
        product_code="SG072",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    old = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]

    def fail_history(*_args, **_kwargs):
        raise RuntimeError("simulated history failure")

    monkeypatch.setattr(replacement_service, "log_inventory_transaction", fail_history)

    try:
        replace_qr_serial(db_session, user, old.serial_number)
    except RuntimeError:
        db_session.rollback()
    else:
        assert False, "the simulated failure must escape"

    db_session.refresh(old)
    assert db_session.scalar(select(func.count(Serial.id))) == 1
    assert old.status == SerialStatus.IN_STOCK.value
    assert old.active is True
    assert old.replaced_by_id is None
