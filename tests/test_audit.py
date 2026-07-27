from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import BatchStatus, BatchType, InventoryTransaction, Product, SerialStatus, StorageLocation, TransactionType, User
from app.services.audit import (
    create_audit_assignment,
    reconcile_audit_batch,
    refresh_expired_audit_assignments,
    summarize_audit_findings,
)
from app.services.inventory import add_serial_to_batch, apply_batch_statuses, create_batch, generate_serials
from app.services.shelf_verification import verify_pending_items_on_shelf


def test_audit_reconciliation_finds_verified_missing_and_extra(db_session):
    auditor = User(username="auditor", password_hash="x", role="auditor")
    product = Product(
        product_code="SG040",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([auditor, product])
    db_session.commit()
    in_stock = generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)
    sold = generate_serials(db_session, product, 1, initial_status=SerialStatus.SOLD)[0]
    batch = create_batch(db_session, auditor, BatchType.AUDIT, "Rack A", "")
    add_serial_to_batch(db_session, batch, auditor, in_stock[0].serial_number)
    add_serial_to_batch(db_session, batch, auditor, sold.serial_number)
    summary = reconcile_audit_batch(db_session, batch)
    assert summary.verified == 1
    assert summary.missing == 1
    assert summary.extra == 1
    findings = {finding.finding_type for finding in batch.audit_findings}
    assert findings == {"VERIFIED", "MISSING", "EXTRA"}


def test_audit_submit_logs_audit_transaction(db_session):
    auditor = User(username="auditor2", password_hash="x", role="auditor")
    product = Product(
        product_code="SG041",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    location = StorageLocation(
        code="AUDIT-SHELF",
        warehouse="MAIN",
        zone="A",
        section="1",
        rack="R1",
        shelf="S1",
        bin="B1",
    )
    db_session.add_all([auditor, product, location])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, auditor, BatchType.AUDIT, "Rack A", "")
    add_serial_to_batch(db_session, batch, auditor, serial.serial_number)
    verify_pending_items_on_shelf(db_session, batch=batch, location=location, user=auditor)
    apply_batch_statuses(db_session, batch, auditor)
    txn = db_session.scalar(select(InventoryTransaction).where(InventoryTransaction.serial_id == serial.id))
    assert txn.transaction_type == TransactionType.AUDIT.value
    assert txn.status_from == SerialStatus.IN_STOCK.value
    assert txn.status_to == SerialStatus.IN_STOCK.value


def test_timed_assignment_reconciles_scans_across_multiple_batches(db_session):
    now = datetime.now(timezone.utc)
    admin = User(username="audit-admin", password_hash="x", role="admin")
    auditor = User(username="window-auditor", password_hash="x", role="auditor")
    product = Product(
        product_code="WINDOW01",
        product_name="Windowed product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Windowed product",
    )
    db_session.add_all([admin, auditor, product])
    db_session.commit()
    serials = generate_serials(
        db_session,
        product,
        10,
        initial_status=SerialStatus.IN_STOCK,
    )
    assignment = create_audit_assignment(
        db_session,
        product=product,
        auditor=auditor,
        assigned_by=admin,
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
    )
    first_batch = assignment.batches[0]

    for serial in serials[:8]:
        add_serial_to_batch(
            db_session,
            first_batch,
            auditor,
            serial.serial_number,
        )
    first_summary = reconcile_audit_batch(db_session, first_batch, now=now)
    assert first_summary.verified == 8
    assert first_summary.pending == 2
    assert first_summary.missing == 0

    first_batch.status = BatchStatus.SUBMITTED.value
    second_batch = create_batch(
        db_session,
        auditor,
        BatchType.AUDIT,
        product.product_name,
        "",
        audit_assignment_id=assignment.id,
    )
    for serial in serials[8:]:
        add_serial_to_batch(
            db_session,
            second_batch,
            auditor,
            serial.serial_number,
        )
    final_summary = reconcile_audit_batch(db_session, second_batch, now=now)

    assert final_summary.verified == 10
    assert final_summary.pending == 0
    assert final_summary.missing == 0
    assert {finding.finding_type for finding in second_batch.audit_findings} == {
        "VERIFIED"
    }


def test_unscanned_assignment_stock_becomes_missing_only_after_deadline(db_session):
    now = datetime.now(timezone.utc)
    admin = User(username="deadline-admin", password_hash="x", role="admin")
    auditor = User(username="deadline-auditor", password_hash="x", role="auditor")
    product = Product(
        product_code="DEADLINE01",
        product_name="Deadline product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Deadline product",
    )
    db_session.add_all([admin, auditor, product])
    db_session.commit()
    serials = generate_serials(
        db_session,
        product,
        2,
        initial_status=SerialStatus.IN_STOCK,
    )
    assignment = create_audit_assignment(
        db_session,
        product=product,
        auditor=auditor,
        assigned_by=admin,
        starts_at=now - timedelta(minutes=30),
        ends_at=now + timedelta(minutes=30),
    )
    batch = assignment.batches[0]
    add_serial_to_batch(db_session, batch, auditor, serials[0].serial_number)

    active = reconcile_audit_batch(db_session, batch, now=now)
    refresh_expired_audit_assignments(
        db_session,
        now=assignment.ends_at + timedelta(seconds=1),
    )
    db_session.expire_all()
    expired = summarize_audit_findings(db_session.get(type(batch), batch.id))

    assert active.pending == 1
    assert active.missing == 0
    assert expired.pending == 0
    assert expired.missing == 1
