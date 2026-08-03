from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from hashlib import sha256
import json
import logging
import threading

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import TallyDataJob, utc_now
from app.services.tally_cache import replace_cached_ledgers, replace_cached_sales_book
from app.services.tally_masters import (
    TallyDataError,
    fetch_tally_companies,
    fetch_tally_ledgers,
    fetch_tally_sales_book,
    fetch_tally_stock_locations,
)


COMPANIES_JOB = "companies"
LEDGERS_JOB = "ledgers"
STOCK_LOCATIONS_JOB = "stock_locations"
SALES_BOOK_JOB = "sales_book"
TALLY_DATA_JOB_TYPES = {
    COMPANIES_JOB,
    LEDGERS_JOB,
    STOCK_LOCATIONS_JOB,
    SALES_BOOK_JOB,
}
ACTIVE_JOB_STATUSES = {"queued", "running"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed"}
MAX_ACTIVE_TALLY_DATA_JOBS = 20
TALLY_DATA_JOB_LEASE_SECONDS = 60
TALLY_DATA_JOB_RETENTION_HOURS = 24

logger = logging.getLogger("setuora")
_enqueue_lock = threading.Lock()


class TallyDataQueueFull(RuntimeError):
    """Raised when bounded Tally data-job backpressure rejects more work."""


def decode_job_payload(job: TallyDataJob) -> dict[str, object]:
    try:
        payload = json.loads(job.payload_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def decode_job_result(job: TallyDataJob) -> dict[str, object]:
    try:
        result = json.loads(job.result_json or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _request_key(job_type: str, company_id: int, payload: dict[str, object]) -> str:
    identity = json.dumps(
        {"job_type": job_type, "company_id": company_id, "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def queue_tally_data_job(
    db: Session,
    *,
    job_type: str,
    company_id: int,
    requested_by_id: int | None,
    settings: dict[str, str],
    tally_company: str = "",
    from_date: date | None = None,
    to_date: date | None = None,
) -> TallyDataJob:
    if job_type not in TALLY_DATA_JOB_TYPES:
        raise ValueError(f"Unsupported Tally data job: {job_type}")
    payload: dict[str, object] = {
        "settings": {
            "tally_host": settings.get("tally_host", ""),
            "tally_port": settings.get("tally_port", ""),
        },
        "tally_company": tally_company.strip(),
    }
    if from_date is not None:
        payload["from_date"] = from_date.isoformat()
    if to_date is not None:
        payload["to_date"] = to_date.isoformat()
    request_key = _request_key(job_type, company_id, payload)

    with _enqueue_lock:
        existing = db.scalar(
            select(TallyDataJob)
            .where(
                TallyDataJob.request_key == request_key,
                TallyDataJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .order_by(TallyDataJob.created_at)
            .limit(1)
        )
        if existing:
            return existing

        active_count = db.scalar(
            select(func.count(TallyDataJob.id)).where(
                TallyDataJob.status.in_(ACTIVE_JOB_STATUSES)
            )
        ) or 0
        if active_count >= MAX_ACTIVE_TALLY_DATA_JOBS:
            raise TallyDataQueueFull(
                "The Tally data queue is full. Wait for the current requests to finish."
            )

        db.execute(
            delete(TallyDataJob).where(
                TallyDataJob.status.in_(TERMINAL_JOB_STATUSES),
                TallyDataJob.completed_at
                < utc_now() - timedelta(hours=TALLY_DATA_JOB_RETENTION_HOURS),
            ).execution_options(synchronize_session=False)
        )
        job = TallyDataJob(
            request_key=request_key,
            job_type=job_type,
            company_id=company_id,
            requested_by_id=requested_by_id,
            status="queued",
            payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


def _claim_next_job(db: Session) -> TallyDataJob | None:
    now = utc_now()
    stale_before = now - timedelta(seconds=TALLY_DATA_JOB_LEASE_SECONDS)
    available = or_(
        TallyDataJob.status == "queued",
        and_(
            TallyDataJob.status == "running",
            TallyDataJob.started_at <= stale_before,
        ),
    )
    job = db.scalar(
        select(TallyDataJob)
        .where(available)
        .order_by(TallyDataJob.created_at)
        .limit(1)
    )
    if job is None:
        return None
    changed = db.execute(
        update(TallyDataJob)
        .where(TallyDataJob.id == job.id, available)
        .values(status="running", started_at=now, error=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if not changed:
        return None
    db.refresh(job)
    return job


def _execute_job(job: TallyDataJob) -> dict[str, object]:
    payload = decode_job_payload(job)
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    clean_settings = {str(key): str(value) for key, value in settings.items()}
    tally_company = str(payload.get("tally_company", "")).strip()

    if job.job_type == COMPANIES_JOB:
        names = fetch_tally_companies(clean_settings)
        return {"companies": names}
    if job.job_type == LEDGERS_JOB:
        ledgers = fetch_tally_ledgers(clean_settings, tally_company)
        with SessionLocal() as db:
            replace_cached_ledgers(db, job.company_id, tally_company, ledgers)
        return {"count": len(ledgers)}
    if job.job_type == STOCK_LOCATIONS_JOB:
        locations = fetch_tally_stock_locations(clean_settings, tally_company)
        return {"locations": [asdict(location) for location in locations]}
    if job.job_type == SALES_BOOK_JOB:
        from_date = date.fromisoformat(str(payload.get("from_date", "")))
        to_date = date.fromisoformat(str(payload.get("to_date", "")))
        vouchers = fetch_tally_sales_book(
            clean_settings,
            tally_company,
            from_date,
            to_date,
        )
        with SessionLocal() as db:
            replace_cached_sales_book(
                db,
                job.company_id,
                tally_company,
                from_date,
                to_date,
                vouchers,
            )
        return {"count": len(vouchers)}
    raise ValueError(f"Unsupported Tally data job: {job.job_type}")


def _finish_job(job_id: int, *, result: dict[str, object] | None = None, error: str | None = None) -> None:
    with SessionLocal() as db:
        job = db.get(TallyDataJob, job_id)
        if job is None or job.status != "running":
            return
        job.status = "failed" if error else "succeeded"
        job.result_json = (
            json.dumps(result or {}, separators=(",", ":"), sort_keys=True)
            if error is None
            else None
        )
        job.error = error
        job.completed_at = utc_now()
        db.commit()


def process_pending_tally_data_job() -> int:
    with SessionLocal() as db:
        job = _claim_next_job(db)
        if job is None:
            return 0
        job_id = job.id
        db.expunge(job)
    try:
        result = _execute_job(job)
    except TallyDataError as exc:
        _finish_job(job_id, error=str(exc))
    except Exception as exc:
        logger.exception("Queued Tally data request failed")
        _finish_job(job_id, error=f"Tally data request failed: {exc}")
    else:
        _finish_job(job_id, result=result)
    return 1
