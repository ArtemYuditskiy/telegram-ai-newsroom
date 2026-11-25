"""Poll SQLite for due posts and deliver each claimed revision."""

import asyncio
import logging
import signal
from datetime import UTC, timedelta
from random import uniform
from time import perf_counter
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from telegram_ai_newsroom.delivery_store import DeliveryStore
from telegram_ai_newsroom.domain.workflow import PublicationState
from telegram_ai_newsroom.publisher import DeliveryError, Publisher
from telegram_ai_newsroom.telemetry import event

ATTEMPT_TIMEOUT = 75
RECOVERY_AGE = timedelta(minutes=3)
MAX_ATTEMPTS = 5
logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, store: DeliveryStore, publisher: Publisher, destination: str) -> None:
        self.store = store
        self.publisher = publisher
        self.destination = destination

    async def tick(self) -> UUID | None:
        recovered = await self.store.recover(self.store.clock.now() - RECOVERY_AGE)
        if recovered:
            event(logger, logging.WARNING, "delivery_recovered", count=recovered)
        job = await self.store.claim(self.destination)
        if job is None:
            return None
        started = perf_counter()
        context = {"publication_id": str(job.id), "attempt_id": str(job.attempt_id)}
        try:
            async with asyncio.timeout(ATTEMPT_TIMEOUT):
                message_id = await self.publisher.send(job)
        except DeliveryError as exc:
            state, code = exc.state, exc.code
            retry_at = None
            now = self.store.clock.now()
            retry_after_at = now + timedelta(seconds=exc.retry_after) if exc.retry_after else None
            if state == PublicationState.RETRY_WAIT:
                if job.attempt_number >= MAX_ATTEMPTS:
                    state = PublicationState.FAILED
                else:
                    delay = max(exc.retry_after, 15 * 2 ** (job.attempt_number - 1))
                    retry_at = now + timedelta(seconds=delay + uniform(0, 3))
            await self.store.finish(
                job, state, retry_at=retry_at, detail=code, retry_after_at=retry_after_at
            )
            event(
                logger,
                logging.WARNING,
                "delivery_attempt",
                **context,
                state=state,
                code=code,
                latency_ms=round((perf_counter() - started) * 1_000, 1),
            )
        except TimeoutError:
            await self.store.finish(job, PublicationState.UNCERTAIN, detail="attempt_deadline")
            event(
                logger,
                logging.WARNING,
                "delivery_attempt",
                **context,
                state="uncertain",
                code="attempt_deadline",
                latency_ms=round((perf_counter() - started) * 1_000, 1),
            )
        else:
            await self.store.finish(job, PublicationState.PUBLISHED, message_id=message_id)
            event(
                logger,
                logging.INFO,
                "delivery_attempt",
                **context,
                state="published",
                message_id=message_id,
                latency_ms=round((perf_counter() - started) * 1_000, 1),
            )
        return job.id

    async def run(self, interval: int) -> None:
        stopped = asyncio.Event()
        active = asyncio.Lock()
        loop = asyncio.get_running_loop()
        signals = (signal.SIGINT, signal.SIGTERM)

        async def poll() -> None:
            async with active:
                if stopped.is_set():
                    return
                try:
                    await self.tick()
                except Exception as exc:
                    event(logger, logging.ERROR, "delivery_tick_failed", error=type(exc).__name__)

        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(
            poll,
            "interval",
            seconds=interval,
            max_instances=1,
            coalesce=True,
            next_run_time=self.store.clock.now(),
        )
        for sig in signals:
            loop.add_signal_handler(sig, stopped.set)
        scheduler.start()
        try:
            await stopped.wait()
        finally:
            scheduler.pause()
            async with active:
                scheduler.shutdown()
            for sig in signals:
                loop.remove_signal_handler(sig)
