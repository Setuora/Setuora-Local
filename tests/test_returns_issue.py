from sqlalchemy import select

from app.models import BatchItem, BatchStatus, BatchType, InventoryTransaction, Product, ScanLog, SerialStatus, TransactionType, User
from app.services.inventory import InventoryError, add_serial_to_batch, apply_batch_statuses, create_batch, generate_serials
from app.services.sale_returns import scan_sale_return_product
from app.services.tally import sync_batch


def product():
    return Product(
        product_code="SG060",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )


def test_sales_return_good_condition_moves_sold_item_to_stock(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    item = product()
    db_session.add_all([user, item])
    db_session.commit()
    serial = generate_serials(db_session, item, 1, initial_status=SerialStatus.SOLD)[0]
    batch = create_batch(db_session, user, BatchType.SALES_RETURN, "Customer", "", "GOOD")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)
    assert serial.status == SerialStatus.IN_STOCK.value
    txn = db_session.scalar(select(InventoryTransaction).where(InventoryTransaction.serial_id == serial.id))
    assert txn.transaction_type == TransactionType.SALES_RETURN.value
    assert txn.status_from == SerialStatus.SOLD.value
    assert txn.status_to == SerialStatus.IN_STOCK.value


def test_sales_return_damaged_marks_item_damaged(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    item = product()
    db_session.add_all([user, item])
    db_session.commit()
    serial = generate_serials(db_session, item, 1, initial_status=SerialStatus.SOLD)[0]
    batch = create_batch(db_session, user, BatchType.SALES_RETURN, "Customer", "", "DAMAGED")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)
    assert serial.status == SerialStatus.DAMAGED.value


def test_draft_sale_return_rejects_inactive_replaced_qr(db_session):
    user = User(username="sales-return-lock", password_hash="x", role="sales")
    item = product()
    db_session.add_all([user, item])
    db_session.commit()
    serial = generate_serials(db_session, item, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    serial.status = SerialStatus.INVALID.value
    serial.active = False
    db_session.commit()

    try:
        scan_sale_return_product(db_session, batch, user, serial.serial_number)
    except InventoryError as exc:
        assert "inactive" in str(exc)
    else:
        assert False, "inactive QR codes must not be accepted in sale-return mode"

    assert db_session.scalar(select(BatchItem).where(BatchItem.batch_id == batch.id)) is not None
    rejected = db_session.scalar(select(ScanLog).where(ScanLog.batch_id == batch.id, ScanLog.status == "REJECTED"))
    assert rejected is not None
    assert "inactive" in rejected.message


def test_purchase_return_and_issue_statuses(db_session):
    user = User(username="admin", password_hash="x", role="admin")
    item = product()
    db_session.add_all([user, item])
    db_session.commit()
    serials = generate_serials(db_session, item, 2, initial_status=SerialStatus.IN_STOCK)
    purchase_return = create_batch(db_session, user, BatchType.PURCHASE_RETURN, "Supplier", "", "SUPPLIER_RETURN")
    add_serial_to_batch(db_session, purchase_return, user, serials[0].serial_number)
    apply_batch_statuses(db_session, purchase_return, user)
    issue = create_batch(db_session, user, BatchType.ISSUE, "Marketing", "", "SAMPLE")
    issue_item = add_serial_to_batch(db_session, issue, user, serials[1].serial_number)
    apply_batch_statuses(db_session, issue, user)
    assert serials[0].status == SerialStatus.PURCHASE_RETURN.value
    assert serials[1].status == SerialStatus.ISSUED.value
    assert issue_item.rate == 100
    txn_types = db_session.scalars(select(InventoryTransaction.transaction_type).order_by(InventoryTransaction.id)).all()
    assert txn_types == [TransactionType.PURCHASE_RETURN.value, TransactionType.ISSUE.value]


def test_unsupported_purchase_return_tally_sync_is_queued_not_posted(db_session):
    user = User(username="purchase", password_hash="x", role="purchase")
    item = product()
    db_session.add_all([user, item])
    db_session.commit()
    serial = generate_serials(db_session, item, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.PURCHASE_RETURN, "Supplier", "", "SUPPLIER_RETURN")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)
    db_session.commit()
    sync_batch(db_session, batch)
    assert batch.status == BatchStatus.PENDING_SYNC.value
    assert "not configured" in batch.last_error
