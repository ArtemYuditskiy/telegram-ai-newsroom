"""Paired prompt evaluation with human labels and a bounded call count."""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from telegram_ai_newsroom.editorial import PROMPTS, PROMPTS_V3, PROMPTS_V4, Audit, EditorialPipeline
from telegram_ai_newsroom.llm import ModelError, ModelProfile, Operation, Provider


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    article: str = Field(min_length=1, max_length=60_000)
    rationale: str = Field(min_length=1)


class SelectionCase(Case):
    stage: Literal["score"]
    expected_selected: bool


class DraftCase(Case):
    stage: Literal["draft"]
    required_text: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class ReviewCase(Case):
    stage: Literal["review"]
    draft: str = Field(min_length=1, max_length=2_000)
    expected_supported: bool


QualityCase = Annotated[SelectionCase | DraftCase | ReviewCase, Field(discriminator="stage")]


class QualityDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[QualityCase] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_cases(self) -> "QualityDataset":
        if len({case.name for case in self.cases}) != len(self.cases):
            raise ValueError("Case names must be unique")
        return self


async def assess(pipeline: EditorialPipeline, case: QualityCase) -> dict[str, object]:
    if isinstance(case, SelectionCase):
        score = await pipeline.score_article(case.title, case.article)
        selected = score.total >= pipeline.threshold
        return {
            "score": score.total,
            "output": score.model_dump(),
            "selected": selected,
            "expected": case.expected_selected,
            "passed": selected == case.expected_selected,
        }
    if isinstance(case, DraftCase):
        draft = await pipeline.generate_draft(case.title, case.article)
        issue = pipeline.draft_issue(case.article, draft)
        text = " ".join(draft.text.casefold().split())
        missing = [item for item in case.required_text if item.casefold() not in text]
        return {
            "local_issue": issue,
            "missing_text": missing,
            "output": draft.model_dump(),
            "passed": issue is None and not missing,
        }
    review = await pipeline.review_draft(case.title, case.article, case.draft)
    return {
        "supported": review.supported,
        "expected": case.expected_supported,
        "output": review.model_dump(),
        "passed": review.supported == case.expected_supported,
    }


async def compare_prompts(
    dataset: QualityDataset,
    profiles: dict[Operation, ModelProfile],
    providers: dict[Operation, Provider],
    *,
    threshold: int,
    request_budget: int,
    interval: float,
    candidate: Literal["v3", "v4"] = "v3",
    describe_error: Callable[[ModelError], dict[str, object]],
    on_result: Callable[[dict[str, object]], Awaitable[None]],
) -> dict[str, object]:
    planned = 2 * len(dataset.cases)
    if planned > request_budget:
        raise ModelError("comparison_exceeds_request_budget")

    calls: list[Audit] = []
    rows: list[dict[str, object]] = []

    async def audit(call: Audit) -> None:
        calls.append(call)

    versions = {
        name: EditorialPipeline(
            profiles, providers, audit, threshold, prompts=prompts, retry_rate_limit=False
        )
        for name, prompts in (
            ("v2", PROMPTS),
            (candidate, PROMPTS_V3 if candidate == "v3" else PROMPTS_V4),
        )
    }
    last_start: float | None = None
    timeouts = 0
    stop_reason = None
    for index, case in enumerate(dataset.cases):
        # Alternate order to balance transient provider load across variants.
        for revision in ("v2", candidate) if index % 2 == 0 else (candidate, "v2"):
            if last_start is not None:
                await asyncio.sleep(max(0, interval - (monotonic() - last_start)))
            last_start = monotonic()
            row: dict[str, object] = {
                "case": case.name,
                "stage": case.stage,
                "variant": revision,
                "prompt_version": versions[revision].prompts[case.stage][0],
                "rationale": case.rationale,
            }
            before = len(calls)
            try:
                row.update(await assess(versions[revision], case))
            except ModelError as exc:
                row.update(describe_error(exc))
                if exc.code == "timeout":
                    timeouts += 1
                if exc.code in {"rate_limit", "authentication"}:
                    stop_reason = exc.code
                elif timeouts >= 2:
                    stop_reason = "repeated_timeout"
            if len(calls) > before:
                call = calls[-1]
                row.update(
                    latency_ms=call.latency_ms,
                    input_tokens=call.usage.input,
                    output_tokens=call.usage.output,
                )
            rows.append(row)
            await on_result(row)
            if stop_reason:
                break
        if stop_reason:
            break
    summary = {}
    for variant in versions:
        per_stage = {}
        for stage in ("score", "draft", "review"):
            results = [row for row in rows if row["variant"] == variant and row["stage"] == stage]
            per_stage[stage] = {
                "passed": sum(row.get("passed") is True for row in results),
                "mismatched": sum(row.get("passed") is False for row in results),
                "schema_errors": sum(
                    row.get("error") == "invalid_output_schema" for row in results
                ),
                "other_errors": sum(
                    "error" in row and row["error"] != "invalid_output_schema" for row in results
                ),
                "planned": sum(case.stage == stage for case in dataset.cases),
            }
        summary[variant] = per_stage
    return {
        "planned_requests": planned,
        "calls": len(calls),
        "stopped": stop_reason,
        "threshold": threshold,
        "summary": summary,
        "cases": rows,
    }
