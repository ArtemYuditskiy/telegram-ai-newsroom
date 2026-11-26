"""Async persistence for editorial workflow state."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import (
    Column,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    event,
    select,
)
from sqlalchemy.engine import URL, Connection
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telegram_ai_newsroom.domain.clock import Clock, SystemClock, utc_timestamp
from telegram_ai_newsroom.domain.workflow import (
    Candidate,
    CandidateState,
    DraftRevision,
    DraftState,
    InvalidTransition,
    Publication,
    PublicationState,
)

metadata = MetaData()
candidates = Table(
    "candidates",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "state", Enum(CandidateState, native_enum=False, create_constraint=True), nullable=False
    ),
)
drafts = Table(
    "draft_revisions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("candidate_id", Uuid, ForeignKey("candidates.id"), nullable=False, index=True),
    Column("text", Text, nullable=False),
    Column("state", Enum(DraftState, native_enum=False, create_constraint=True), nullable=False),
)
publications = Table(
    "publications",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("draft_revision_id", Uuid, ForeignKey("draft_revisions.id"), nullable=False, index=True),
    Column("candidate_id", Uuid, ForeignKey("candidates.id")),
    Column("cancelled_at", Text),
    Column("due_at", Text, nullable=False),
    Column("destination", Text),
    Column("message_id", Integer),
    Column(
        "state", Enum(PublicationState, native_enum=False, create_constraint=True), nullable=False
    ),
)
attempts = Table(
    "publication_attempts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("publication_id", Uuid, ForeignKey("publications.id"), nullable=False, index=True),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("detail", Text),
    Column("retry_after_at", Text),
    Column("outcome", Enum(PublicationState, native_enum=False, create_constraint=True)),
)
Index("ix_publications_state_due_at", publications.c.state, publications.c.due_at)
PENDING_PUBLICATION_STATES = (PublicationState.SCHEDULED, PublicationState.RETRY_WAIT)
ACTIVE_PUBLICATION = publications.c.cancelled_at.is_(None) & publications.c.state.in_(
    (
        *PENDING_PUBLICATION_STATES,
        PublicationState.PUBLISHING,
        PublicationState.UNCERTAIN,
    )
)
Index(
    "uq_publications_active_candidate",
    publications.c.candidate_id,
    unique=True,
    sqlite_where=ACTIVE_PUBLICATION,
)
Index(
    "uq_publication_open_attempt",
    attempts.c.publication_id,
    unique=True,
    sqlite_where=attempts.c.finished_at.is_(None),
)


def open_database(path: Path) -> AsyncEngine:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(path)),
        connect_args={"isolation_level": None},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection: object, _: object) -> None:
        # SQLAlchemy exposes a synchronous DBAPI adapter for aiosqlite.
        adapter = cast(DBAPIConnection, connection)
        cursor = adapter.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection: Connection) -> None:
        connection.exec_driver_sql(
            "BEGIN IMMEDIATE" if connection.get_execution_options().get("claim_write") else "BEGIN"
        )

    return engine


class WorkflowStore:
    def __init__(self, engine: AsyncEngine, clock: Clock | None = None) -> None:
        self._sessions = async_sessionmaker(engine)
        self._clock = SystemClock() if clock is None else clock

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["WorkflowTransaction"]:
        async with self._sessions.begin() as session:
            yield WorkflowTransaction(session, self._clock)


class WorkflowTransaction:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def discover(self, id: UUID) -> Candidate:
        candidate = Candidate(id)
        await self._session.execute(candidates.insert().values(id=id, state=candidate.state))
        return candidate

    async def candidate(self, id: UUID) -> Candidate:
        state = (
            await self._session.execute(select(candidates.c.state).where(candidates.c.id == id))
        ).scalar_one()
        return Candidate(id, state)

    async def advance_candidate(self, id: UUID, target: CandidateState) -> Candidate:
        if target is CandidateState.DRAFTED:
            raise InvalidTransition("Use create_draft to record the revision and advance together")
        current = await self.candidate(id)
        return await self._advance(current, target)

    async def _advance(self, current: Candidate, target: CandidateState) -> Candidate:
        updated = current.transition(target)
        result = await self._session.execute(
            candidates.update()
            .where(candidates.c.id == current.id, candidates.c.state == current.state)
            .values(state=updated.state)
            .returning(candidates.c.id)
        )
        if result.scalar_one_or_none() is None:
            raise InvalidTransition("Candidate state changed concurrently")
        return updated

    async def reconcile_publication(
        self,
        id: UUID,
        outcome: PublicationState,
        retry_at: datetime | None = None,
        *,
        message_id: int | None = None,
    ) -> Publication:
        if (outcome is PublicationState.RETRY_WAIT) != (retry_at is not None):
            raise ValueError("A retry time is required exactly when waiting to retry")
        current, due_at = await self._publication(id)
        updated = current.reconcile(outcome)
        await self._save_publication(
            current, updated, due_at if retry_at is None else utc_timestamp(retry_at), message_id
        )
        return updated

    async def create_draft(self, id: UUID, candidate_id: UUID, text: str) -> DraftRevision:
        async with self._session.begin_nested():
            candidate = await self.candidate(candidate_id)
            draft = DraftRevision.create(id, candidate, text)
            if candidate.state is CandidateState.SELECTED:
                await self._advance(candidate, CandidateState.DRAFTED)
            await self._session.execute(
                drafts.insert().values(
                    id=draft.id, candidate_id=draft.candidate_id, text=draft.text, state=draft.state
                )
            )
        return draft

    async def draft(self, id: UUID) -> DraftRevision:
        row = (await self._session.execute(select(drafts).where(drafts.c.id == id))).one()
        return DraftRevision(row.id, row.candidate_id, row.text, row.state)

    async def review(self, id: UUID, target: DraftState) -> DraftRevision:
        current = await self.draft(id)
        reviewed = current.review(target)
        result = await self._session.execute(
            drafts.update()
            .where(drafts.c.id == id, drafts.c.state == current.state)
            .values(state=reviewed.state)
            .returning(drafts.c.id)
        )
        if result.scalar_one_or_none() is None:
            raise InvalidTransition("Draft state changed concurrently")
        return reviewed

    async def schedule(self, id: UUID, draft_id: UUID, due_at: datetime) -> Publication:
        draft = await self.draft(draft_id)
        publication = Publication.schedule(id, draft)
        if await self._session.scalar(
            select(publications.c.id).where(
                publications.c.candidate_id == draft.candidate_id, ACTIVE_PUBLICATION
            )
        ):
            raise InvalidTransition("This candidate already has an active publication")
        await self._session.execute(
            publications.insert().values(
                id=publication.id,
                draft_revision_id=draft_id,
                candidate_id=draft.candidate_id,
                state=publication.state,
                due_at=utc_timestamp(due_at),
            )
        )
        return publication

    async def _publication(self, id: UUID) -> tuple[Publication, str]:
        row = (
            await self._session.execute(select(publications).where(publications.c.id == id))
        ).one()
        return Publication(row.id, row.draft_revision_id, row.state), row.due_at

    async def _save_publication(
        self,
        current: Publication,
        updated: Publication,
        due_at: str,
        message_id: int | None = None,
    ) -> None:
        result = await self._session.execute(
            publications.update()
            .where(
                publications.c.id == current.id,
                publications.c.state == current.state,
                publications.c.cancelled_at.is_(None),
            )
            .values(state=updated.state, due_at=due_at, message_id=message_id)
            .returning(publications.c.id)
        )
        if result.scalar_one_or_none() is None:
            raise InvalidTransition("Publication state changed concurrently")

    async def start_attempt(self, publication_id: UUID, attempt_id: UUID) -> Publication:
        async with self._session.begin_nested():
            current, due_at = await self._publication(publication_id)
            now = utc_timestamp(self._clock.now())
            if due_at > now:
                raise InvalidTransition("Publication is not due")
            updated = current.transition(PublicationState.PUBLISHING)
            await self._save_publication(current, updated, due_at)
            await self._session.execute(
                attempts.insert().values(
                    id=attempt_id, publication_id=publication_id, started_at=now
                )
            )
        return updated

    async def finish_attempt(
        self,
        attempt_id: UUID,
        outcome: PublicationState,
        retry_at: datetime | None = None,
        *,
        message_id: int | None = None,
        detail: str | None = None,
        retry_after_at: datetime | None = None,
    ) -> Publication:
        if (outcome is PublicationState.RETRY_WAIT) != (retry_at is not None):
            raise ValueError("A retry time is required exactly when waiting to retry")
        async with self._session.begin_nested():
            result = await self._session.execute(
                attempts.update()
                .where(attempts.c.id == attempt_id, attempts.c.finished_at.is_(None))
                .values(
                    outcome=outcome,
                    detail=detail,
                    finished_at=utc_timestamp(self._clock.now()),
                    retry_after_at=utc_timestamp(retry_after_at) if retry_after_at else None,
                )
                .returning(attempts.c.publication_id)
            )
            publication_id = result.scalar_one_or_none()
            if publication_id is None:
                raise InvalidTransition("Attempt is missing or already finished")
            current, due_at = await self._publication(publication_id)
            updated = current.transition(outcome)
            await self._save_publication(
                current,
                updated,
                due_at if retry_at is None else utc_timestamp(retry_at),
                message_id,
            )
        return updated
