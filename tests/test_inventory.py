from sqlalchemy import select, update

from app.models import (
    BatchType,
    GstRegistrationType,
    InventoryTransaction,
    Product,
    ScanLog,
    Serial,
    SerialStatus,
    StorageLocation,
    TransactionType,
    User,
)
from app.services.inventory import (
    InventoryError,
    add_serial_to_batch,
    apply_batch_statuses,
    create_batch,
    generate_serials,
    status_summary,
)
from app.services.shelf_verification import verify_pending_items_on_shelf


def test_receive_generated_serial(db_session):
    user = User(username="purchase", password_hash="x", role="purchase")
    product = Product(
        product_code="SG001",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1)[0]
    batch = create_batch(db_session, user, BatchType.RECEIVE, "Supplier", "")
    item = add_serial_to_batch(db_session, batch, user, serial.serial_number)
    assert item.serial.status == SerialStatus.GENERATED.value
    assert item.serial.display_status == "UNASSIGNED"
    assert status_summary(db_session)["UNASSIGNED"] == 1


def test_unregistered_sale_ignores_stale_gst_number(db_session):
    user = User(username="sales-unregistered", password_hash="x", role="sales")
    db_session.add(user)
    db_session.commit()

    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "Cash Customer",
        "",
        party_gst_registration_type=GstRegistrationType.UNREGISTERED_CONSUMER.value,
        party_gst_name="Cash Customer GST Name",
        party_gstin="not-a-gstin",
    )

    assert batch.party_gst_registration_type == GstRegistrationType.UNREGISTERED_CONSUMER.value
    assert batch.party_gst_name == "Cash Customer GST Name"
    assert batch.party_gstin is None
    assert batch.party_state == "Karnataka"


def test_submit_aborts_when_serial_grabbed_concurrently(db_session):
    user = User(username="sales-race", password_hash="x", role="sales")
    product = Product(
        product_code="SG099",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)

    db_session.execute(
        update(Serial).where(Serial.id == serial.id).values(status=SerialStatus.SOLD.value)
        .execution_options(synchronize_session=False)
    )

    try:
        apply_batch_statuses(db_session, batch, user)
        assert False, "stale claim should abort"
    except InventoryError:
        pass
    assert batch.status == "DRAFT"
    assert db_session.scalar(select(InventoryTransaction).where(InventoryTransaction.batch_id == batch.id)) is None


def test_serial_cannot_be_added_to_two_open_batches(db_session):
    user = User(username="sales-dup", password_hash="x", role="sales")
    product = Product(
        product_code="SG098",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch_a = create_batch(db_session, user, BatchType.SALE, "Customer A", "")
    batch_b = create_batch(db_session, user, BatchType.SALE, "Customer B", "")
    add_serial_to_batch(db_session, batch_a, user, serial.serial_number)

    try:
        add_serial_to_batch(db_session, batch_b, user, serial.serial_number)
        assert False, "serial already in an open batch must be rejected"
    except InventoryError as exc:
        assert "another open batch" in str(exc)


def test_sale_rejects_generated_serial(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = Product(
        product_code="SG002",
        product_name="Chilli",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Chilli",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    try:
        add_serial_to_batch(db_session, batch, user, serial.serial_number)
    except InventoryError:
        assert True
    else:
        assert False
    rejected = db_session.scalar(
        select(ScanLog).where(
            ScanLog.batch_id == batch.id,
            ScanLog.serial_id == serial.id,
            ScanLog.status == "REJECTED",
        )
    )
    assert rejected is not None
    assert "is unassigned" in rejected.message


def test_audit_rejects_generated_unassigned_serial(db_session):
    user = User(username="audit-unassigned", password_hash="x", role="auditor")
    product = Product(
        product_code="SG003",
        product_name="Pepper",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Pepper",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1)[0]
    batch = create_batch(db_session, user, BatchType.AUDIT, "Rack A", "")

    try:
        add_serial_to_batch(db_session, batch, user, serial.serial_number)
    except InventoryError as exc:
        assert "is unassigned" in str(exc)
    else:
        assert False, "an unassigned serial must not be accepted by audit"

    rejected = db_session.scalar(
        select(ScanLog).where(
            ScanLog.batch_id == batch.id,
            ScanLog.serial_id == serial.id,
            ScanLog.status == "REJECTED",
        )
    )
    assert rejected is not None
    assert "is unassigned" in rejected.message


def test_purchase_batch_logs_purchase_transaction(db_session):
    user = User(username="purchase2", password_hash="x", role="purchase")
    product = Product(
        product_code="SG004",
        product_name="Turmeric",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Turmeric",
    )
    location = StorageLocation(
        code="PURCHASE-SHELF",
        warehouse="MAIN",
        zone="A",
        section="1",
        rack="R1",
        shelf="S1",
        bin="B1",
    )
    db_session.add_all([user, product, location])
    db_session.commit()
    serial = generate_serials(db_session, product, 1)[0]
    batch = create_batch(db_session, user, BatchType.PURCHASE, "Supplier", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    verify_pending_items_on_shelf(db_session, batch=batch, location=location, user=user)
    apply_batch_statuses(db_session, batch, user)
    txn = db_session.scalar(select(InventoryTransaction).where(InventoryTransaction.serial_id == serial.id))
    assert serial.status == SerialStatus.IN_STOCK.value
    assert txn.transaction_type == TransactionType.PURCHASE.value
    assert txn.status_from == SerialStatus.GENERATED.value
    assert txn.status_to == SerialStatus.IN_STOCK.value
