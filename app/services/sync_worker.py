from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.database import SessionLocal
from app.models import Batch, BatchItem, BatchStatus, Serial, Setting, utc_now
from app.services.settings import enabled_tally_sync_batch_types, get_all_settings
from app.services.tally import SYNC_LEASE_MINUTES, TALLY_XML_SUPPORTED_BATCH_TYPES, sync_batch
from app.services.tally_masters import GatewayCheckResult, test_tally_gateway


WORKER_STATE_KEY = "setuora_retry_worker_task"
TALLY_REQUEST_SPACING_SECONDS = 1.0
DEFAULT_RETRY_INTERVAL_SECONDS = 180
MIN_RETRY_INTERVAL_SECONDS = 30
GATEWAY_CHECK_STATE_KEY = "_tally_gateway_check_state"
GATEWAY_CHECK_LEASE_SECONDS = 30
logger = logging.getLogger("setuora")
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_wake_event: asyncio.Event | None = None


@dataclass(frozen=True)
class GatewayCheckState:
    request_id: str
    status: str
    message: str
    response_excerpt: str = ""

    @property
    def pending(self) -> bool:
        return self.status in {"queued", "running"}

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @property
    def failed(self) -> bool:
        return self.status == "failed"


def _gateway_state_payload(raw: str | None) -> dict[str, object] | None:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("request_id"):
        return None
    return payload


def _gateway_state_view(payload: dict[str, object] | None) -> GatewayCheckState | None:
    if payload is None:
        return None
    return GatewayCheckState(
        request_id=str(payload.get("request_id", "")),
        status=str(payload.get("status", "")),
        message=str(payload.get("message", "")),
        response_excerpt=str(payload.get("response_excerpt", "")),
    )


def gateway_check_state(db: Session) -> GatewayCheckState | None:
    row = db.get(Setting, GATEWAY_CHECK_STATE_KEY)
    return _gateway_state_view(_gateway_state_payload(row.value if row else None))


def queue_tally_gateway_check(db: Session) -> GatewayCheckState:
    """Persist one gateway check and collapse clicks while it is queued/running."""
    row = db.get(Setting, GATEWAY_CHECK_STATE_KEY)
    current = _gateway_state_view(_gateway_state_payload(row.value if row else None))
    if current and current.pending:
        notify_retry_worker()
        return current

    settings = get_all_settings(db)
    payload = {
        "request_id": str(uuid4()),
        "status": "queued",
        "message": "Gateway test queued. Waiting for the Tally request worker.",
        "response_excerpt": "",
        "requested_at": utc_now().isoformat(),
        "started_at": None,
        "settings": {
            "tally_host": settings.get("tally_host", ""),
            "tally_port": settings.get("tally_port", ""),
        },
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if row:
        original = row.value
        changed = db.execute(
            update(Setting)
            .where(Setting.key == GATEWAY_CHECK_STATE_KEY, Setting.value == original)
            .values(value=encoded)
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if not changed:
            db.expire_all()
            concurrent = gateway_check_state(db)
            if concurrent and concurrent.pending:
                notify_retry_worker()
                return concurrent
            return queue_tally_gateway_check(db)
    else:
        db.add(Setting(key=GATEWAY_CHECK_STATE_KEY, value=encoded))
        db.commit()

    notify_retry_worker()
    queued = _gateway_state_view(payload)
    if queued is None:  # pragma: no cover - payload is constructed immediately above
        raise RuntimeError("Could not create Tally gateway queue state")
    return queued


def _claim_gateway_check(db: Session) -> tuple[str, dict[str, str]] | None:
    row = db.get(Setting, GATEWAY_CHECK_STATE_KEY)
    payload = _gateway_state_payload(row.value if row else None)
    if not row or payload is None:
        return None

    status = str(payload.get("status", ""))
    if status == "running":
        try:
            started_at = datetime.fromisoformat(str(payload.get("started_at", "")))
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=utc_now().tzinfo)
            if started_at > utc_now() - timedelta(seconds=GATEWAY_CHECK_LEASE_SECONDS):
                return None
        except (TypeError, ValueError):
            pass
    elif status != "queued":
        return None

    request_id = str(payload["request_id"])
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    claimed = dict(payload)
    claimed.update(
        status="running",
        message="Testing the Tally gateway in the request queue...",
        started_at=utc_now().isoformat(),
    )
    encoded = json.dumps(claimed, separators=(",", ":"), sort_keys=True)
    changed = db.execute(
        update(Setting)
        .where(Setting.key == GATEWAY_CHECK_STATE_KEY, Setting.value == row.value)
        .values(value=encoded)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if not changed:
        return None
    return request_id, {str(key): str(value) for key, value in settings.items()}


def _finish_gateway_check(db: Session, request_id: str, result: GatewayCheckResult) -> None:
    row = db.get(Setting, GATEWAY_CHECK_STATE_KEY)
    payload = _gateway_state_payload(row.value if row else None)
    if not row or payload is None or payload.get("request_id") != request_id:
        return
    payload.update(
        status="succeeded" if result.ok else "failed",
        message=result.message,
        response_excerpt=result.response_excerpt,
        completed_at=utc_now().isoformat(),
    )
    row.value = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    db.commit()


def process_pending_gateway_check() -> int:
    with SessionLocal() as db:
        claim = _claim_gateway_check(db)
    if claim is None:
        return 0
    request_id, settings = claim
    try:
        result = test_tally_gateway(settings)
    except Exception as exc:
        logger.exception("Queued Tally gateway check failed")
        result = GatewayCheckResult(False, f"Tally gateway check failed: {exc}")
    with SessionLocal() as db:
        _finish_gateway_check(db, request_id, result)
    return 1


def _retry_interval_seconds(db: Session) -> int:
    settings = get_all_settings(db)
    try:
        return max(MIN_RETRY_INTERVAL_SECONDS, int(settings.get("retry_interval_seconds", "180")))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_INTERVAL_SECONDS


def notify_retry_worker() -> None:
    """Wake the single Tally consumer; safe to call from FastAPI worker threads."""
    loop = _worker_loop
    wake_event = _worker_wake_event
    if loop is None or wake_event is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(wake_event.set)


def queue_batch_for_sync(db: Session, batch: Batch) -> bool:
    """Durably queue one batch and collapse repeated requests for that batch."""
    if batch.status not in {
        BatchStatus.SUBMITTED.value,
        BatchStatus.PENDING_SYNC.value,
        BatchStatus.FAILED.value,
    }:
        return False

    if batch.status == BatchStatus.FAILED.value:
        # Tally confirmed that this payload was rejected, so the next attempt
        # must use current settings and the current XML generator. The status
        # predicate prevents a stale simultaneous click from overwriting a
        # worker that has already claimed this batch as SYNCING.
        db.execute(
            update(Batch)
            .where(Batch.id == batch.id, Batch.status == BatchStatus.FAILED.value)
            .values(
                status=BatchStatus.PENDING_SYNC.value,
                sync_request_xml=None,
                sync_started_at=None,
                last_retry_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        db.refresh(batch)
    elif batch.status == BatchStatus.PENDING_SYNC.value and batch.last_retry_at is not None:
        # An explicit retry bypasses the automatic retry cooldown but preserves
        # the frozen payload in case the prior connection outcome was unknown.
        db.execute(
            update(Batch)
            .where(Batch.id == batch.id, Batch.status == BatchStatus.PENDING_SYNC.value)
            .values(last_retry_at=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        db.refresh(batch)

    notify_retry_worker()
    return True


def retry_pending_batches(limit: int = 10) -> int:
    with SessionLocal() as db:
        enabled_batch_types = enabled_tally_sync_batch_types(db)
        if not enabled_batch_types:
            return 0
        now = utc_now()
        retry_due_before = now - timedelta(seconds=_retry_interval_seconds(db))
        batches = db.scalars(
            select(Batch)
            .where(
                or_(
                    Batch.status == BatchStatus.SUBMITTED.value,
                    and_(
                        Batch.status == BatchStatus.PENDING_SYNC.value,
                        or_(Batch.last_retry_at.is_(None), Batch.last_retry_at <= retry_due_before),
                    ),
                    and_(
                        Batch.status == BatchStatus.SYNCING.value,
                        Batch.sync_started_at < now - timedelta(minutes=SYNC_LEASE_MINUTES),
                    ),
                ),
                Batch.batch_type.in_(TALLY_XML_SUPPORTED_BATCH_TYPES & enabled_batch_types),
            )
            .order_by(Batch.last_retry_at.is_not(None), Batch.last_retry_at, Batch.created_at)
            .limit(limit)
            .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
        ).all()
        for batch in batches:
            sync_batch(db, batch)
        return len(batches)


def process_next_tally_request() -> int:
    """Process one queued gateway check or voucher through one consumer."""
    if process_pending_gateway_check():
        return 1
    return retry_pending_batches(1)


async def retry_worker_loop() -> None:
    wake_event = _worker_wake_event or asyncio.Event()
    while True:
        interval = DEFAULT_RETRY_INTERVAL_SECONDS
        try:
            with SessionLocal() as db:
                interval = _retry_interval_seconds(db)

            # One consumer and one item per pass guarantee that Tally never
            # receives concurrent requests from this process.
            processed = await asyncio.to_thread(process_next_tally_request)
            if processed:
                await asyncio.sleep(TALLY_REQUEST_SPACING_SECONDS)
                continue

            # Clear before the final scan so a notification cannot be lost in
            # the gap between checking the database and starting to wait.
            wake_event.clear()
            processed = await asyncio.to_thread(process_next_tally_request)
            if processed:
                await asyncio.sleep(TALLY_REQUEST_SPACING_SECONDS)
                continue
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=interval)
            except TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending sync retry worker failed")
            # Back off even when a previously set wake event caused this pass.
            # Otherwise a persistent database/configuration error can spin the
            # worker at full speed and add load while Tally is already unhealthy.
            await asyncio.sleep(min(interval, 5))


def start_retry_worker(app: FastAPI) -> None:
    global _worker_loop, _worker_wake_event
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task and not task.done():
        return
    _worker_loop = asyncio.get_running_loop()
    _worker_wake_event = asyncio.Event()
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(retry_worker_loop()))


async def stop_retry_worker(app: FastAPI) -> None:
    global _worker_loop, _worker_wake_event
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if not task:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    setattr(app.state, WORKER_STATE_KEY, None)
    _worker_loop = None
    _worker_wake_event = None
