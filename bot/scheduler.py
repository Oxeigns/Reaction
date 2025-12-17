from __future__ import annotations

import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler


class SchedulerManager:
    """Wrapper to ensure the APScheduler instance starts only once."""

    _scheduler: AsyncIOScheduler | None = None

    @classmethod
    def get_scheduler(cls) -> AsyncIOScheduler:
        if cls._scheduler is None:
            cls._scheduler = AsyncIOScheduler()
        return cls._scheduler

    @classmethod
    def start(cls) -> AsyncIOScheduler:
        scheduler = cls.get_scheduler()
        if not scheduler.running:
            scheduler.start()
            logging.info("Background scheduler started.")
        else:
            logging.info("Background scheduler already running; skipping start.")
        return scheduler

    @classmethod
    def ensure_job(
        cls, job_id: str, func: Callable[..., Awaitable[None]] | Callable[..., None], *, trigger: str = "interval", **kwargs
    ) -> None:
        scheduler = cls.start()
        job = scheduler.get_job(job_id)
        if job:
            logging.info("Job %s already registered; skipping new registration.", job_id)
            return

        scheduler.add_job(func, trigger=trigger, id=job_id, **kwargs)
        logging.info("Job %s registered with trigger %s", job_id, trigger)

    @classmethod
    def shutdown(cls) -> None:
        scheduler = cls._scheduler
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
            logging.info("Background scheduler stopped.")


async def log_heartbeat() -> None:
    """Emit a lightweight heartbeat so operators can spot duplicate workers."""

    logging.info("Heartbeat: bot worker and scheduler are active.")


__all__ = ["SchedulerManager", "log_heartbeat"]
