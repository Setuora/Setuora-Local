from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
import logging

from fastapi import FastAPI
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import Batch, BatchItem, BatchStatus, Serial, utc_now
from app.services.settings import get_all_settings, is_tally_enabled
from app.services.tally import SYNC_LEASE_MINUTES, TALLY_XML_SUPPORTED_BATCH_TYPES, sync_batch


WORKER_STATE_KEY = "setuora_retry_worker_task"
logger = logging.getLogger("setuora")


def retry_pending_batches(limit: int = 10) -> int:
    with SessionLocal() as db:
        if not is_tally_enabled(db):
            return 0
        batches = db.scalars(
            select(Batch)
            .where(
                or_(
                    Batch.status == BatchStatus.PENDING_SYNC.value,
                    (
                        (Batch.status == BatchStatus.SYNCING.value)
                        & (Batch.sync_started_at < utc_now() - timedelta(minutes=SYNC_LEASE_MINUTES))
                    ),
                ),
                Batch.batch_type.in_(TALLY_XML_SUPPORTED_BATCH_TYPES),
            )
            .order_by(Batch.last_retry_at.is_not(None), Batch.last_retry_at, Batch.created_at)
            .limit(limit)
            .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
        ).all()
        for batch in batches:
            sync_batch(db, batch)
        return len(batches)


async def retry_worker_loop() -> None:
    while True:
        interval = 180
        try:
            with SessionLocal() as db:
                settings = get_all_settings(db)
                try:
                    interval = max(30, int(settings.get("retry_interval_seconds", "180")))
                except ValueError:
                    interval = 180
            await asyncio.sleep(interval)
            await asyncio.to_thread(retry_pending_batches)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending sync retry worker failed")
            await asyncio.sleep(interval)


def start_retry_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task and not task.done():
        return
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(retry_worker_loop()))


async def stop_retry_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if not task:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    setattr(app.state, WORKER_STATE_KEY, None)
