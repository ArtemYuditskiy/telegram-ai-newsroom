"""Exercise transaction boundaries on migrated SQLite."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import NoResultFound

from telegram_ai_newsroom.domain.repository import EditorialStore
from telegram_ai_newsroom.domain.workflow import (
    CandidateState,
    DraftState,
    InvalidTransition,
    PublicationState,
)
from telegram_ai_newsroom.sqlite import WorkflowStore, open_database


class FixedClock:
    now_value = datetime(2026, 11, 18, 13, tzinfo=UTC)

    def now(self) -> datetime:
        return self.now_value


def test_workflow_on_migrated_database(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)

    async def exercise() -> None:
        engine = open_database(path)
        try:
            clock = FixedClock()
            store: EditorialStore = WorkflowStore(engine, clock)
            due_at = datetime(2026, 11, 18, 16, 1, tzinfo=timezone(timedelta(hours=3)))
            async with store.transaction() as tx:
                candidate = await tx.discover(uuid4())
                with pytest.raises(InvalidTransition):
                    await tx.advance_candidate(candidate.id, CandidateState.SELECTED)
                for state in (
                    CandidateState.NORMALIZED,
                    CandidateState.ELIGIBLE,
                    CandidateState.SCORED,
                    CandidateState.SELECTED,
                ):
                    await tx.advance_candidate(candidate.id, state)
            draft_id = uuid4()
            with pytest.raises(RuntimeError, match="abort"):
                async with store.transaction() as tx:
                    await tx.create_draft(draft_id, candidate.id, "First")
                    raise RuntimeError("abort")
            async with store.transaction() as tx:
                assert (await tx.candidate(candidate.id)).state is CandidateState.SELECTED
                await tx.create_draft(draft_id, candidate.id, "First")
                with pytest.raises(InvalidTransition):
                    await tx.schedule(uuid4(), draft_id, due_at)
                await tx.review(draft_id, DraftState.APPROVED)
                publication = await tx.schedule(uuid4(), draft_id, due_at)
                revised = await tx.create_draft(uuid4(), candidate.id, "Second")
                assert revised.state is DraftState.REVIEW_PENDING
                assert publication.draft_revision_id == draft_id
                assert (await tx.draft(draft_id)).text == "First"
                assert (await tx.candidate(candidate.id)).state is CandidateState.DRAFTED
                with pytest.raises(InvalidTransition, match="not due"):
                    await tx.start_attempt(publication.id, uuid4())
            clock.now_value += timedelta(minutes=1)
            attempt_id = uuid4()
            async with store.transaction() as tx:
                assert (await tx.start_attempt(publication.id, attempt_id)).state is (
                    PublicationState.PUBLISHING
                )
            async with store.transaction() as tx:
                assert (await tx.finish_attempt(attempt_id, PublicationState.UNCERTAIN)).state is (
                    PublicationState.UNCERTAIN
                )
                with pytest.raises(InvalidTransition, match="already finished"):
                    await tx.finish_attempt(attempt_id, PublicationState.PUBLISHED)
                with pytest.raises(InvalidTransition):
                    await tx.start_attempt(publication.id, uuid4())
                retry_at = clock.now() + timedelta(minutes=1)
                await tx.reconcile_publication(
                    publication.id, PublicationState.RETRY_WAIT, retry_at
                )
                with pytest.raises(InvalidTransition, match="not due"):
                    await tx.start_attempt(publication.id, uuid4())
            clock.now_value = retry_at
            retry_id = uuid4()
            async with store.transaction() as tx:
                await tx.start_attempt(publication.id, retry_id)
            async with store.transaction() as tx:
                result = await tx.finish_attempt(retry_id, PublicationState.PUBLISHED)
                assert result.state is PublicationState.PUBLISHED
            async with store.transaction() as writer:
                other = await writer.discover(uuid4())
                async with store.transaction() as reader:
                    assert (await reader.candidate(candidate.id)).state is CandidateState.DRAFTED
                    # An independent session cannot see the writer's uncommitted row.
                    with pytest.raises(NoResultFound):
                        await reader.candidate(other.id)
            async with store.transaction() as reader:
                assert (await reader.candidate(other.id)).state is CandidateState.DISCOVERED
            uncertain = publication.transition(PublicationState.PUBLISHING).transition(
                PublicationState.UNCERTAIN
            )
            with pytest.raises(InvalidTransition):
                uncertain.transition(PublicationState.PUBLISHING)
            assert (
                uncertain.reconcile(PublicationState.PUBLISHED).state is PublicationState.PUBLISHED
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    command.downgrade(config, "base")
    command.upgrade(config, "head")
