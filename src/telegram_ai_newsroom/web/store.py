"""Queries and transactions for the editor workspace."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, Table, Text, Uuid, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from telegram_ai_newsroom.domain.clock import SystemClock, utc_timestamp
from telegram_ai_newsroom.domain.workflow import (
    CandidateState,
    DraftState,
    InvalidTransition,
    PublicationState,
)
from telegram_ai_newsroom.editorial import PostText, Review
from telegram_ai_newsroom.editorial_store import calls, results
from telegram_ai_newsroom.ingestion_store import articles, sources
from telegram_ai_newsroom.sqlite import (
    PENDING_PUBLICATION_STATES,
    WorkflowTransaction,
    candidates,
    drafts,
    metadata,
    publications,
)

sessions = Table(
    "editor_sessions",
    metadata,
    Column("token_hash", Text, primary_key=True),
    Column("csrf", Text, nullable=False),
    Column("expires_at", Text, nullable=False, index=True),
)
revision_context = Table(
    "editor_revision_context",
    metadata,
    Column("id", Uuid, ForeignKey("draft_revisions.id"), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("parent_id", Uuid, ForeignKey("draft_revisions.id"), nullable=False),
    Column("run_id", Uuid),
    Column("review_details", Text),
)


class EditorStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.db = async_sessionmaker(engine)
        self.schedule_db = async_sessionmaker(engine.execution_options(claim_write=True))
        self.clock = SystemClock()

    async def queue(
        self, filter_state: str, page: int
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        revisions = (
            select(drafts.c.id)
            .outerjoin(revision_context, drafts.c.id == revision_context.c.id)
            .where(drafts.c.candidate_id == candidates.c.id)
        )
        if filter_state == "review":
            revisions = revisions.where(drafts.c.state == DraftState.REVIEW_PENDING)
        latest = (
            revisions.order_by(
                revision_context.c.created_at.desc().nulls_last(), drafts.c.id.desc()
            )
            .limit(1)
            .correlate(candidates)
            .scalar_subquery()
        )
        async with self.db() as db:
            counts = dict(
                (
                    await db.execute(
                        select(candidates.c.state, func.count()).group_by(candidates.c.state)
                    )
                )
                .tuples()
                .all()
            )
            statement = (
                select(
                    articles.c.candidate_id,
                    articles.c.title,
                    articles.c.url,
                    articles.c.published_at,
                    candidates.c.state,
                    results.c.score,
                    drafts.c.id.label("revision_id"),
                    drafts.c.state.label("draft_state"),
                )
                .join(candidates, articles.c.candidate_id == candidates.c.id)
                .outerjoin(results, candidates.c.id == results.c.candidate_id)
                .outerjoin(drafts, drafts.c.id == latest)
                .order_by(articles.c.published_at.desc().nulls_last(), articles.c.candidate_id)
                .limit(26)
                .offset((page - 1) * 25)
            )
            if filter_state == "review":
                statement = statement.where(drafts.c.state == DraftState.REVIEW_PENDING)
            elif filter_state != "all":
                statement = statement.where(candidates.c.state == CandidateState(filter_state))
            rows = (await db.execute(statement)).mappings().all()
            return [dict(row) for row in rows], counts

    async def candidate(self, identity: UUID, revision: UUID | None) -> dict[str, Any]:
        async with self.db() as db:
            entry = (
                (
                    await db.execute(
                        select(
                            articles,
                            candidates.c.state,
                            results.c.score,
                            results.c.threshold,
                            results.c.score_details,
                            results.c.run_id.label("initial_run"),
                            results.c.draft_id,
                            results.c.review_details.label("initial_review"),
                        )
                        .join(candidates, candidates.c.id == articles.c.candidate_id)
                        .outerjoin(results, results.c.candidate_id == candidates.c.id)
                        .where(articles.c.candidate_id == identity)
                    )
                )
                .mappings()
                .one()
            )
            rows = (
                (
                    await db.execute(
                        select(
                            drafts,
                            revision_context.c.kind,
                            revision_context.c.created_at,
                            revision_context.c.run_id,
                            revision_context.c.review_details,
                        )
                        .outerjoin(revision_context, drafts.c.id == revision_context.c.id)
                        .where(drafts.c.candidate_id == identity)
                        .order_by(
                            revision_context.c.created_at.desc().nulls_last(), drafts.c.id.desc()
                        )
                    )
                )
                .mappings()
                .all()
            )
            revision_rows = [dict(row) for row in rows]
            selected = next((row for row in revision_rows if row["id"] == revision), None)
            if revision is not None and selected is None:
                raise LookupError("Revision not found")
            if selected is None and revision_rows:
                selected = revision_rows[0]
            if selected:
                headline, separator, body = selected["text"].partition("\n\n")
                selected["headline"] = headline if separator else ""
                selected["body"] = body if separator else selected["text"]
                selected["review"] = json.loads(selected["review_details"] or "null")
                if selected["id"] == entry["draft_id"]:
                    selected["kind"] = "generated"
                    selected["review"] = json.loads(entry["initial_review"] or "null")
                    selected["run_id"] = entry["initial_run"]
            history = (
                (
                    await db.execute(
                        select(
                            calls.c.operation,
                            calls.c.outcome,
                            calls.c.model,
                            calls.c.backend,
                            calls.c.prompt_version,
                            calls.c.recorded_at,
                            calls.c.run_id,
                        )
                        .where(calls.c.candidate_id == identity)
                        .order_by(calls.c.recorded_at.desc())
                        .limit(30)
                    )
                )
                .mappings()
                .all()
            )
            scheduled = (
                (
                    await db.execute(
                        select(
                            publications.c.id,
                            publications.c.draft_revision_id,
                            publications.c.due_at,
                            publications.c.state,
                            publications.c.cancelled_at,
                            publications.c.destination,
                            publications.c.message_id,
                        )
                        .where(publications.c.candidate_id == identity)
                        .order_by(publications.c.due_at)
                    )
                )
                .mappings()
                .all()
                if selected
                else []
            )
            return {
                "entry": dict(entry),
                "draft": selected,
                "revisions": revision_rows,
                "score": json.loads(entry["score_details"] or "null"),
                "history": history,
                "scheduled": scheduled,
            }

    async def decide(self, candidate_id: UUID, revision: UUID, state: DraftState) -> None:
        async with self.db.begin() as db:
            tx = WorkflowTransaction(db, self.clock)
            draft = await tx.draft(revision)
            if draft.candidate_id != candidate_id:
                raise LookupError("Revision not found")
            await tx.review(revision, state)

    async def revise(
        self,
        candidate_id: UUID,
        parent: UUID,
        revision: PostText,
        kind: str,
        review: Review | None = None,
        run_id: UUID | None = None,
    ) -> UUID:
        async with self.db.begin() as db:
            tx = WorkflowTransaction(db, self.clock)
            previous = await tx.draft(parent)
            if previous.candidate_id != candidate_id:
                raise LookupError("Revision not found")
            if previous.state is DraftState.REVIEW_PENDING and (review is None or review.supported):
                await tx.review(parent, DraftState.REJECTED)
            created = await tx.create_draft(uuid4(), candidate_id, revision.text)
            if review is not None and not review.supported:
                await tx.review(created.id, DraftState.REJECTED)
            await db.execute(
                revision_context.insert().values(
                    id=created.id,
                    parent_id=parent,
                    kind=kind,
                    run_id=run_id,
                    review_details=review.model_dump_json() if review else None,
                    created_at=utc_timestamp(self.clock.now()),
                )
            )
            return created.id

    async def schedule(
        self, candidate_id: UUID, revision: UUID, due: datetime, destination: str
    ) -> None:
        async with self.schedule_db.begin() as db:
            tx = WorkflowTransaction(db, self.clock)
            draft = await tx.draft(revision)
            if draft.candidate_id != candidate_id:
                raise LookupError("Revision not found")
            await tx.schedule(uuid4(), revision, due, destination=destination)

    async def change_schedule(
        self,
        candidate_id: UUID,
        publication_id: UUID,
        due: datetime | None,
        *,
        bind_destination: str | None = None,
    ) -> None:
        async with self.schedule_db.begin() as db:
            row = (
                await db.execute(
                    select(publications).where(
                        publications.c.id == publication_id,
                        publications.c.candidate_id == candidate_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise LookupError("Publication not found")
            if row.state == PublicationState.PUBLISHING:
                raise InvalidTransition("The worker has started sending this publication")
            if row.cancelled_at or row.state not in PENDING_PUBLICATION_STATES:
                raise InvalidTransition("This publication can no longer be changed")
            if bind_destination is not None:
                if row.destination is not None:
                    raise InvalidTransition("This publication already has a destination")
                changes = {"destination": bind_destination}
            elif due is None:
                changes = {"cancelled_at": utc_timestamp(self.clock.now())}
            else:
                timestamp = utc_timestamp(due)
                if row.state == PublicationState.RETRY_WAIT:
                    timestamp = max(timestamp, row.due_at)
                changes = {"due_at": timestamp}
            await db.execute(
                publications.update().where(publications.c.id == publication_id).values(**changes)
            )

    async def sources(self) -> list[dict[str, Any]]:
        async with self.db() as db:
            rows = (await db.execute(select(sources).order_by(sources.c.url))).mappings()
            return [dict(row) for row in rows]

    async def toggle_source(self, url: str, enabled: bool) -> None:
        async with self.db.begin() as db:
            result = await db.execute(
                sources.update()
                .where(sources.c.url == url)
                .values(enabled=enabled)
                .returning(sources.c.url)
            )
            if result.scalar_one_or_none() is None:
                raise LookupError("Source not found")
