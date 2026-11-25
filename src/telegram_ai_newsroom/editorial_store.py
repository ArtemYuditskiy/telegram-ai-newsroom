"""Store model audit records and commit validated editorial outcomes."""

from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, Integer, Table, Text, Uuid, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from telegram_ai_newsroom.domain.clock import SystemClock, utc_timestamp
from telegram_ai_newsroom.domain.workflow import CandidateState, DraftState
from telegram_ai_newsroom.editorial import Audit, Result
from telegram_ai_newsroom.ingestion_store import articles
from telegram_ai_newsroom.llm import ModelError
from telegram_ai_newsroom.sqlite import WorkflowTransaction, candidates, metadata

calls = Table(
    "llm_calls",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("candidate_id", Uuid, ForeignKey("candidates.id"), nullable=False, index=True),
    Column("run_id", Uuid, nullable=False),
    Column("operation", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("backend", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", Text),
    Column("recorded_at", Text, nullable=False),
)
results = Table(
    "editorial_results",
    metadata,
    Column("candidate_id", Uuid, ForeignKey("candidates.id"), primary_key=True),
    Column("run_id", Uuid, nullable=False),
    Column("draft_id", Uuid, ForeignKey("draft_revisions.id")),
    Column("score", Integer, nullable=False),
    Column("threshold", Integer, nullable=False),
    Column("score_details", Text, nullable=False),
    Column("draft_details", Text),
    Column("review_details", Text),
)


class EditorialRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine)
        self.clock = SystemClock()

    async def read(self, candidate_id: UUID) -> tuple[str, str]:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(articles.c.title, articles.c.text)
                    .join(candidates, articles.c.candidate_id == candidates.c.id)
                    .where(
                        articles.c.candidate_id == candidate_id,
                        candidates.c.state == CandidateState.ELIGIBLE,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ModelError("candidate_not_eligible")
            return row.title, row.text

    async def audit(self, candidate_id: UUID, run_id: UUID, call: Audit) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                calls.insert().values(
                    id=uuid4(),
                    candidate_id=candidate_id,
                    run_id=run_id,
                    operation=call.operation,
                    prompt_version=call.prompt_version,
                    backend=call.profile.backend,
                    model=call.profile.model,
                    outcome=call.outcome,
                    latency_ms=call.latency_ms,
                    input_tokens=call.usage.input,
                    output_tokens=call.usage.output,
                    cost_usd=str(call.cost_usd) if call.cost_usd is not None else None,
                    recorded_at=utc_timestamp(self.clock.now()),
                )
            )

    async def save(
        self, candidate_id: UUID, run_id: UUID, threshold: int, result: Result
    ) -> UUID | None:
        async with self.sessions.begin() as session:
            tx = WorkflowTransaction(session, self.clock)
            await tx.advance_candidate(candidate_id, CandidateState.SCORED)
            draft_id = None
            if result.draft is None:
                await tx.advance_candidate(candidate_id, CandidateState.REJECTED)
            else:
                assert result.review is not None
                await tx.advance_candidate(candidate_id, CandidateState.SELECTED)
                draft = await tx.create_draft(uuid4(), candidate_id, result.draft.text)
                draft_id = draft.id
                if not result.review.supported:
                    await tx.review(draft.id, DraftState.REJECTED)
            await session.execute(
                results.insert().values(
                    candidate_id=candidate_id,
                    run_id=run_id,
                    draft_id=draft_id,
                    score=result.score.total,
                    threshold=threshold,
                    score_details=result.score.model_dump_json(),
                    draft_details=result.draft.model_dump_json() if result.draft else None,
                    review_details=result.review.model_dump_json() if result.review else None,
                )
            )
            return draft_id
