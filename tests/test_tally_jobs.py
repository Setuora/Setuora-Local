from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import TallyDataJob, User
from app.services import sync_worker, tally_jobs
from app.services.settings import add_company
from app.services.tally_cache import cached_ledgers, cached_sales_book
from app.services.tally_jobs import TallyDataQueueFull
from app.services.tally_masters import TallyDataError, TallyLedger, TallySalesVoucher


COMPANY_CONFIG = {
    "company_name": "Queued Company",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "tally_stock_location": "Main Location",
    "sales_voucher_type": "Sales",
    "purchase_voucher_type": "Purchase",
    "sales_ledger_name": "Sales",
    "purchase_ledger_name": "Purchase",
    "cgst_ledger_name": "CGST",
    "sgst_ledger_name": "SGST",
    "sales_gst_ledger_mappings": "",
    "round_off_ledger_name": "Round Off",
}


def _company_and_user(db_session):
    user = User(username="tally-queue-user", password_hash="x", role="admin")
    db_session.add(user)
    db_session.commit()
    company = add_company(db_session, "Queued Company Profile", COMPANY_CONFIG)
    return company, user


def test_spammed_ledger_loads_share_one_durable_job(db_session, monkeypatch):
    company, user = _company_and_user(db_session)
    worker_session = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(tally_jobs, "SessionLocal", worker_session)
    calls: list[tuple[str, str]] = []

    def fake_fetch(settings, tally_company):
        calls.append((settings["tally_host"], tally_company))
        return [TallyLedger("Customer A", "Sundry Debtors", "-500")]

    monkeypatch.setattr(tally_jobs, "fetch_tally_ledgers", fake_fetch)
    jobs = [
        tally_jobs.queue_tally_data_job(
            db_session,
            job_type=tally_jobs.LEDGERS_JOB,
            company_id=company.id,
            requested_by_id=user.id,
            settings=COMPANY_CONFIG,
            tally_company="Queued Company",
        )
        for _ in range(50)
    ]

    assert len({job.id for job in jobs}) == 1
    assert tally_jobs.process_pending_tally_data_job() == 1
    assert tally_jobs.process_pending_tally_data_job() == 0
    assert calls == [("127.0.0.1", "Queued Company")]

    db_session.expire_all()
    job = db_session.get(TallyDataJob, jobs[0].id)
    assert job.status == "succeeded"
    assert [ledger.name for ledger in cached_ledgers(db_session, company.id, "Queued Company")] == [
        "Customer A"
    ]


def test_sales_book_job_caches_results_and_failure_is_terminal(db_session, monkeypatch):
    company, user = _company_and_user(db_session)
    worker_session = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(tally_jobs, "SessionLocal", worker_session)
    monkeypatch.setattr(
        tally_jobs,
        "fetch_tally_sales_book",
        lambda *_args: [
            TallySalesVoucher(
                "2026-08-02",
                "42",
                "Sales",
                "Customer A",
                "500",
                remote_id="sales-42",
            )
        ],
    )
    job = tally_jobs.queue_tally_data_job(
        db_session,
        job_type=tally_jobs.SALES_BOOK_JOB,
        company_id=company.id,
        requested_by_id=user.id,
        settings=COMPANY_CONFIG,
        tally_company="Queued Company",
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 2),
    )
    assert tally_jobs.process_pending_tally_data_job() == 1
    db_session.expire_all()
    assert db_session.get(TallyDataJob, job.id).status == "succeeded"
    assert [voucher.voucher_number for voucher in cached_sales_book(
        db_session,
        company.id,
        "Queued Company",
        date(2026, 8, 1),
        date(2026, 8, 2),
    )] == ["42"]

    monkeypatch.setattr(
        tally_jobs,
        "fetch_tally_sales_book",
        lambda *_args: (_ for _ in ()).throw(TallyDataError("Tally gateway timed out.")),
    )
    failed = tally_jobs.queue_tally_data_job(
        db_session,
        job_type=tally_jobs.SALES_BOOK_JOB,
        company_id=company.id,
        requested_by_id=user.id,
        settings=COMPANY_CONFIG,
        tally_company="Queued Company",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 2),
    )
    assert tally_jobs.process_pending_tally_data_job() == 1
    db_session.expire_all()
    failed = db_session.get(TallyDataJob, failed.id)
    assert failed.status == "failed"
    assert failed.error == "Tally gateway timed out."


def test_live_data_queue_has_bounded_backpressure(db_session):
    company, user = _company_and_user(db_session)
    for index in range(tally_jobs.MAX_ACTIVE_TALLY_DATA_JOBS):
        tally_jobs.queue_tally_data_job(
            db_session,
            job_type=tally_jobs.LEDGERS_JOB,
            company_id=company.id,
            requested_by_id=user.id,
            settings=COMPANY_CONFIG,
            tally_company=f"Queued Company {index}",
        )

    with pytest.raises(TallyDataQueueFull, match="queue is full"):
        tally_jobs.queue_tally_data_job(
            db_session,
            job_type=tally_jobs.SALES_BOOK_JOB,
            company_id=company.id,
            requested_by_id=user.id,
            settings=COMPANY_CONFIG,
            tally_company="Another Company",
            from_date=date(2026, 8, 1),
            to_date=date(2026, 8, 2),
        )


def test_worker_alternates_live_data_and_voucher_backlogs(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sync_worker, "process_pending_gateway_check", lambda: 0)
    monkeypatch.setattr(
        sync_worker,
        "process_pending_tally_data_job",
        lambda: calls.append("data") or 1,
    )
    monkeypatch.setattr(
        sync_worker,
        "retry_pending_batches",
        lambda _limit: calls.append("voucher") or 1,
    )
    monkeypatch.setattr(sync_worker, "_prefer_data_job", True)

    assert sync_worker.process_next_tally_request() == 1
    assert sync_worker.process_next_tally_request() == 1
    assert sync_worker.process_next_tally_request() == 1
    assert calls == ["data", "voucher", "data"]
