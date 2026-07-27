from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

from fastapi import FastAPI

from app.config import get_settings
from app.services.backup import create_scheduled_backup


logger = logging.getLogger("setuora")

WORKER_STATE_KEY = "setuora_backup_worker_task"


def run_scheduled_backup_once() -> bool:
    settings = get_settings()
    if not getattr(settings, "automatic_backups_enabled", True):
        return False
    backup = create_scheduled_backup()
    logger.info("Verified SQLite backup created at %s", backup.path)
    if backup.offsite_path:
        logger.info("Verified off-machine SQLite backup copy created at %s", backup.offsite_path)
    return True


async def backup_worker_loop() -> None:
    settings = get_settings()
    await asyncio.sleep(max(0, int(getattr(settings, "backup_startup_delay_seconds", 60))))
    while True:
        try:
            await asyncio.to_thread(run_scheduled_backup_once)
        except Exception:
            logger.exception("Scheduled backup failed")
        settings = get_settings()
        interval_seconds = max(60, int(getattr(settings, "backup_interval_hours", 24)) * 60 * 60)
        await asyncio.sleep(interval_seconds)


def start_backup_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task and not task.done():
        return
    if not getattr(get_settings(), "automatic_backups_enabled", True):
        setattr(app.state, WORKER_STATE_KEY, None)
        return
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(backup_worker_loop()))


async def stop_backup_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if not task:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    setattr(app.state, WORKER_STATE_KEY, None)
