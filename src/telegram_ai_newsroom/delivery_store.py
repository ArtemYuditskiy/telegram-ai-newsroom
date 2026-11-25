"""Serialize claims and persist the result of each delivery attempt."""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from telegram_ai_newsroom.domain.clock import Clock, utc_timestamp
from telegram_ai_newsroom.domain.workflow import DraftState, PublicationState
from telegram_ai_newsroom.ingestion_store import articles, items, sources
from telegram_ai_newsroom.sqlite import WorkflowTransaction, attempts, drafts, publications


@dataclass(frozen=True, slots=True)
class Delivery:
    id: UUID
    attempt_id: UUID
    attempt_number: int
    text: str
    source_url: str | None
    image_url: str | None
    image_hosts: frozenset[str]


class DeliveryStore:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self.read = async_sessionmaker(engine)
        # Take the SQLite writer lock before reading the queue, then release it before HTTP.
        self.write = async_sessionmaker(engine.execution_options(claim_write=True))
        self.clock = clock

    async def claim(self, destination: str) -> Delivery | None:
        now = utc_timestamp(self.clock.now())
        async with self.write.begin() as db:
            cooldown = await db.scalar(
                select(attempts.c.id)
                .join(attempts, attempts.c.publication_id == publications.c.id)
                .where(
                    publications.c.destination == destination,
                    attempts.c.retry_after_at > now,
                )
                .limit(1)
            )
            if cooldown is not None:
                return None
            row = (
                await db.execute(
                    select(
                        publications.c.id,
                        drafts.c.text,
                        drafts.c.candidate_id,
                        articles.c.url,
                        articles.c.image_url,
                    )
                    .join(drafts, drafts.c.id == publications.c.draft_revision_id)
                    .outerjoin(articles, articles.c.candidate_id == drafts.c.candidate_id)
                    .where(
                        publications.c.state.in_(
                            [PublicationState.SCHEDULED, PublicationState.RETRY_WAIT]
                        ),
                        publications.c.due_at <= now,
                        drafts.c.state == DraftState.APPROVED,
                        or_(
                            publications.c.destination.is_(None),
                            publications.c.destination == destination,
                        ),
                    )
                    .order_by(publications.c.due_at, publications.c.id)
                    .limit(1)
                )
            ).one_or_none()
            if row is None:
                return None
            attempt_id = uuid4()
            await WorkflowTransaction(db, self.clock).start_attempt(row.id, attempt_id)
            await db.execute(
                publications.update()
                .where(publications.c.id == row.id)
                .values(destination=destination)
            )
            number = await db.scalar(
                select(func.count()).where(attempts.c.publication_id == row.id)
            )
            host_rows = (
                (
                    await db.scalars(
                        select(sources.c.hosts)
                        .join(items, items.c.source_url == sources.c.url)
                        .where(
                            items.c.candidate_id == row.candidate_id,
                            items.c.image_url == row.image_url,
                            sources.c.enabled.is_(True),
                        )
                        .distinct()
                    )
                ).all()
                if row.image_url
                else []
            )
            hosts = frozenset(host for value in host_rows for host in json.loads(value))
            return Delivery(
                row.id, attempt_id, number or 1, row.text, row.url, row.image_url, hosts
            )

    async def finish(
        self,
        job: Delivery,
        state: PublicationState,
        *,
        retry_at: datetime | None = None,
        message_id: int | None = None,
        detail: str | None = None,
        retry_after_at: datetime | None = None,
    ) -> None:
        async with self.write.begin() as db:
            await WorkflowTransaction(db, self.clock).finish_attempt(
                job.attempt_id,
                state,
                retry_at,
                message_id=message_id,
                detail=detail,
                retry_after_at=retry_after_at,
            )

    async def recover(self, before: datetime) -> int:
        async with self.write.begin() as db:
            stale = (
                await db.scalars(
                    select(attempts.c.id)
                    .join(publications, publications.c.id == attempts.c.publication_id)
                    .where(
                        publications.c.state == PublicationState.PUBLISHING,
                        attempts.c.finished_at.is_(None),
                        attempts.c.started_at <= utc_timestamp(before),
                    )
                )
            ).all()
            tx = WorkflowTransaction(db, self.clock)
            for attempt_id in stale:
                await tx.finish_attempt(
                    attempt_id, PublicationState.UNCERTAIN, detail="worker_interrupted"
                )
            return len(stale)

    async def uncertain(self) -> list[tuple[UUID, str | None]]:
        async with self.read() as db:
            rows = await db.execute(
                select(publications.c.id, publications.c.destination)
                .where(publications.c.state == PublicationState.UNCERTAIN)
                .order_by(publications.c.due_at)
            )
            return [(row.id, row.destination) for row in rows]

    async def resolve(
        self, publication_id: UUID, outcome: PublicationState, message_id: int | None
    ) -> None:
        async with self.write.begin() as db:
            await WorkflowTransaction(db, self.clock).reconcile_publication(
                publication_id,
                outcome,
                self.clock.now() if outcome == PublicationState.RETRY_WAIT else None,
                message_id=message_id,
            )
