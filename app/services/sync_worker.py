from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
import logging

from fastapi import FastAPI
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.database import SessionLocal
from app.models import Batch, BatchItem, BatchStatus, Serial, utc_now
from app.services.settings import enabled_tally_sync_batch_types, get_all_settings
from app.services.tally import SYNC_LEASE_MINUTES, TALLY_XML_SUPPORTED_BATCH_TYPES, sync_batch


WORKER_STATE_KEY = "setuora_retry_worker_task"
TALLY_REQUEST_SPACING_SECONDS = 1.0
DEFAULT_RETRY_INTERVAL_SECONDS = 180
MIN_RETRY_INTERVAL_SECONDS = 30
logger = logging.getLogger("setuora")
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_wake_event: asyncio.Event | None = None


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


async def retry_worker_loop() -> None:
    wake_event = _worker_wake_event or asyncio.Event()
    while True:
        interval = DEFAULT_RETRY_INTERVAL_SECONDS
        try:
            with SessionLocal() as db:
                interval = _retry_interval_seconds(db)

            # One consumer and one item per pass guarantee that Tally never
            # receives concurrent requests from this process.
            processed = await asyncio.to_thread(retry_pending_batches, 1)
            if processed:
                await asyncio.sleep(TALLY_REQUEST_SPACING_SECONDS)
                continue

            # Clear before the final scan so a notification cannot be lost in
            # the gap between checking the database and starting to wait.
            wake_event.clear()
            processed = await asyncio.to_thread(retry_pending_batches, 1)
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
