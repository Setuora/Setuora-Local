import asyncio
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import event

from app.models import BatchStatus, BatchType, Product, SerialStatus, Setting, User, utc_now
from app.services import sync_worker
from app.services import tally as tally_service
from app.services.inventory import add_serial_to_batch, apply_batch_statuses, create_batch, generate_serials
from app.services.tally import TallyResult, sync_batch


def test_retry_worker_start_replaces_finished_task(monkeypatch):
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace())

        async def already_done():
            return None

        finished = asyncio.create_task(already_done())
        await finished
        setattr(app.state, sync_worker.WORKER_STATE_KEY, finished)

        async def replacement_loop():
            await asyncio.Event().wait()

        monkeypatch.setattr(sync_worker, "retry_worker_loop", replacement_loop)
        sync_worker.start_retry_worker(app)
        replacement = getattr(app.state, sync_worker.WORKER_STATE_KEY)

        assert replacement is not finished
        assert replacement.done() is False

        replacement.cancel()
        with suppress(asyncio.CancelledError):
            await replacement

    asyncio.run(scenario())


def test_retry_sync_records_retry_metadata_when_still_queued(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = Product(
        product_code="SG030",
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
    apply_batch_statuses(db_session, batch, user)
    db_session.commit()
    sync_batch(db_session, batch)
    assert batch.status == BatchStatus.PENDING_SYNC.value
    assert batch.retry_count == 0
    sync_batch(db_session, batch)
    assert batch.status == BatchStatus.PENDING_SYNC.value
    assert batch.retry_count == 1
    assert batch.last_retry_at is not None


def test_already_synced_batch_is_not_posted_again(db_session, monkeypatch):
    user = User(username="sales2", password_hash="x", role="sales")
    product = Product(
        product_code="SG031",
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
    apply_batch_statuses(db_session, batch, user)
    batch.status = BatchStatus.SYNCED.value
    db_session.commit()

    calls = {"count": 0}

    def fail_if_posted(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("post_to_tally must not run for an already-synced batch")

    monkeypatch.setattr(tally_service, "post_to_tally", fail_if_posted)
    sync_batch(db_session, batch)
    assert calls["count"] == 0
    assert batch.status == BatchStatus.SYNCED.value


def test_crash_after_tally_success_reuses_frozen_payload_and_remote_id(db_session, monkeypatch):
    user = User(username="sales3", password_hash="x", role="sales")
    product = Product(
        product_code="SG032",
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
    apply_batch_statuses(db_session, batch, user)
    settings = {
        "company_name": "Setuora Test Company",
        "tally_enabled": "true",
        "tally_host": "127.0.0.1",
        "tally_port": "9000",
        "sales_voucher_type": "Sales",
        "purchase_voucher_type": "Purchase",
        "sales_ledger_name": "Sales",
        "purchase_ledger_name": "Purchase",
        "cgst_ledger_name": "CGST",
        "sgst_ledger_name": "SGST",
        "sales_gst_ledger_mappings": "5 | Sales | CGST | SGST | IGST",
        "round_off_ledger_name": "Round Off",
    }
    db_session.add_all(Setting(key=key, value=value) for key, value in settings.items())
    db_session.commit()

    posted_xml: list[str] = []

    def fake_post(xml, _settings):
        posted_xml.append(xml)
        reference = "CREATED=1; ALTERED=0" if len(posted_xml) == 1 else "CREATED=0; ALTERED=1"
        return TallyResult(xml, "<RESPONSE/>", reference)

    monkeypatch.setattr(tally_service, "post_to_tally", fake_post)

    def fail_success_commit(_session):
        if posted_xml:
            raise RuntimeError("simulated process loss after Tally accepted the voucher")

    event.listen(db_session, "before_commit", fail_success_commit)
    try:
        sync_batch(db_session, batch)
    except RuntimeError:
        db_session.rollback()
    else:
        assert False, "the simulated crash must interrupt the success commit"
    event.remove(db_session, "before_commit", fail_success_commit)

    db_session.refresh(batch)
    assert batch.status == BatchStatus.SYNCING.value
    frozen_xml = batch.sync_request_xml
    frozen_remote_id = batch.sync_remote_id

    batch.sync_started_at = utc_now() - timedelta(minutes=11)
    db_session.commit()
    sync_batch(db_session, batch)

    assert batch.status == BatchStatus.SYNCED.value
    assert posted_xml == [frozen_xml, frozen_xml]
    assert batch.sync_remote_id == frozen_remote_id
