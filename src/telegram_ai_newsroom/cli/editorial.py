"""Run or evaluate the model pipeline."""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy.exc import OperationalError

from telegram_ai_newsroom.editorial import Audit, EditorialPipeline, OutputSchemaError
from telegram_ai_newsroom.editorial_store import EditorialRepository
from telegram_ai_newsroom.llm import (
    FakeProvider,
    GeminiProvider,
    ModelError,
    ModelProfile,
    OpenAICompatibleProvider,
    Operation,
    Provider,
)
from telegram_ai_newsroom.quality import QualityDataset, compare_prompts
from telegram_ai_newsroom.sqlite import open_database

editorial = typer.Typer(help="Score, draft and review verified articles.")


class EditorialConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: ModelProfile
    draft: ModelProfile
    review: ModelProfile
    threshold: int = Field(default=65, ge=0, le=100)

    def profiles(self) -> dict[Operation, ModelProfile]:
        return {"score": self.score, "draft": self.draft, "review": self.review}


def provider(profile: ModelProfile, client: httpx.AsyncClient) -> Provider:
    if not profile.structured_output:
        raise ModelError("unsupported_structured_output")
    if profile.backend == "fake":
        return FakeProvider()
    default_key = "GEMINI_API_KEY" if profile.backend == "gemini" else "OPENROUTER_API_KEY"
    variable = profile.api_key_env or default_key
    if not (key := os.environ.get(variable)):
        raise ModelError("missing_provider_api_key")
    if profile.backend == "gemini":
        return GeminiProvider(client, SecretStr(key))
    return OpenAICompatibleProvider(client, SecretStr(key))


async def execute(db: Path, candidate: UUID, config: EditorialConfig) -> dict[str, object]:
    engine = open_database(db)
    try:
        repo = EditorialRepository(engine)
        title, article = await repo.read(candidate)
        run_id = uuid4()
        profiles = config.profiles()

        async def audit(call: Audit) -> None:
            await repo.audit(candidate, run_id, call)

        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            providers = {
                operation: provider(profile, client) for operation, profile in profiles.items()
            }
            result = await EditorialPipeline(profiles, providers, audit, config.threshold).run(
                title, article
            )
        draft_id = await repo.save(candidate, run_id, config.threshold, result)
        return {
            "candidate_id": str(candidate),
            "run_id": str(run_id),
            "score": result.score.total,
            "draft_id": str(draft_id) if draft_id else None,
            "supported": result.review.supported if result.review else None,
        }
    finally:
        await engine.dispose()


@editorial.command()
def run(candidate: UUID, config: Path, db: Path = Path("newsroom.db")) -> None:
    """Process one eligible candidate with explicit provider profiles."""
    try:
        settings = EditorialConfig.model_validate_json(config.read_text(encoding="utf-8"))
        typer.echo(json.dumps(asyncio.run(execute(db, candidate, settings))))
    except ValidationError as exc:
        fields = [".".join(map(str, error["loc"])) or "root" for error in exc.errors()]
        typer.echo(f"Invalid model configuration at: {', '.join(fields)}", err=True)
        raise typer.Exit(1) from None
    except ModelError as exc:
        typer.echo(exc.code, err=True)
        raise typer.Exit(1) from None
    except (OSError, OperationalError):
        typer.echo("Cannot read configuration or database; verify paths and migrations", err=True)
        raise typer.Exit(1) from None


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    article: str = Field(min_length=1, max_length=60_000)
    expected_selected: bool


class StageProbe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1)
    article: str = Field(min_length=1, max_length=60_000)
    review_draft: str = Field(min_length=1, max_length=2_000)
    expected_supported: bool


class EvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[EvalCase] = Field(min_length=1, max_length=30)
    stage_probe: StageProbe | None = None


def eval_error(exc: ModelError) -> dict[str, object]:
    report: dict[str, object] = {"error": exc.code}
    if isinstance(exc, OutputSchemaError):
        report["operation"] = exc.operation
        report["schema_errors"] = [
            {"field": issue.field, "type": issue.kind} for issue in exc.issues
        ]
    return report


async def evaluate(
    cases: list[EvalCase],
    config: EditorialConfig,
    client: httpx.AsyncClient,
) -> list[dict[str, object]]:
    """Evaluate profiles against labels without touching the production database."""
    profiles = config.profiles()
    providers = {key: provider(profile, client) for key, profile in profiles.items()}
    operations: list[str] = []
    outcomes: list[str] = []

    async def audit(call: Audit) -> None:
        operations.append(call.operation)
        outcomes.append(call.outcome)

    pipeline = EditorialPipeline(profiles, providers, audit, config.threshold)
    report = []
    for case in cases:
        operations.clear()
        outcomes.clear()
        try:
            result = await pipeline.run(case.title, case.article)
        except ModelError as exc:
            report.append(
                {
                    "case": case.name,
                    "expected_selected": case.expected_selected,
                    **eval_error(exc),
                    "passed": False,
                    "calls": list(operations),
                    "outcomes": list(outcomes),
                }
            )
            continue
        selected = result.draft is not None
        report.append(
            {
                "case": case.name,
                "score": result.score.total,
                "selected": selected,
                "expected_selected": case.expected_selected,
                "passed": selected == case.expected_selected,
                "calls": list(operations),
                "outcomes": list(outcomes),
                "score_details": result.score.model_dump(),
                "draft": result.draft.model_dump() if result.draft else None,
                "review": result.review.model_dump() if result.review else None,
            }
        )
    return report


async def _eval_report(config: EditorialConfig, cases: list[EvalCase]) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        rows = await evaluate(cases, config, client)
    return {
        "threshold": config.threshold,
        "profiles": {
            phase: {"backend": profile.backend, "model": profile.model}
            for phase, profile in config.profiles().items()
        },
        "summary": {
            "matched_labels": sum(row["passed"] is True for row in rows),
            "false_positives": sum(
                row.get("selected") is True and row["expected_selected"] is False for row in rows
            ),
            "false_negatives": sum(
                row.get("selected") is False and row["expected_selected"] is True for row in rows
            ),
            "errors": sum("error" in row for row in rows),
        },
        "cases": rows,
    }


async def _probe_report(config: EditorialConfig, probe: StageProbe) -> dict[str, object]:
    profiles = config.profiles()
    phases: tuple[Operation, ...] = ("draft", "review")
    events: list[dict[str, str]] = []

    async def audit(call: Audit) -> None:
        events.append({"operation": call.operation, "outcome": call.outcome})

    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        providers = {phase: provider(profiles[phase], client) for phase in phases}
        pipeline = EditorialPipeline(profiles, providers, audit, config.threshold)
        draft_check: dict[str, object]
        try:
            draft = await pipeline.generate_draft(probe.title, probe.article)
            issue = pipeline.draft_issue(probe.article, draft)
            draft_check = {"passed": issue is None, "issue": issue, "output": draft.model_dump()}
        except ModelError as exc:
            draft_check = {"passed": False, **eval_error(exc)}

        review_check: dict[str, object] = {"expected_supported": probe.expected_supported}
        try:
            review = await pipeline.review_draft(probe.title, probe.article, probe.review_draft)
            review_check.update(
                passed=review.supported == probe.expected_supported, output=review.model_dump()
            )
        except ModelError as exc:
            review_check.update(passed=False, **eval_error(exc))
    return {
        "profiles": {
            phase: {"backend": profiles[phase].backend, "model": profiles[phase].model}
            for phase in phases
        },
        "draft": draft_check,
        "review": review_check,
        "calls": events,
        "passed": draft_check["passed"] is True and review_check["passed"] is True,
    }


@editorial.command("eval")
def eval_command(
    config: Path | None = None, dataset: Path | None = None, probe_stages: bool = False
) -> None:
    """Evaluate labeled examples using fake or explicitly configured providers."""
    if config is not None and dataset is None:
        typer.echo("--dataset is required with --config", err=True)
        raise typer.Exit(1)
    try:
        settings = (
            EditorialConfig.model_validate_json(config.read_text(encoding="utf-8"))
            if config
            else EditorialConfig(score=ModelProfile(), draft=ModelProfile(), review=ModelProfile())
        )
        fixture = (
            EvalDataset.model_validate_json(dataset.read_text(encoding="utf-8"))
            if dataset is not None
            else None
        )
        if probe_stages:
            if fixture is None or fixture.stage_probe is None:
                raise ModelError("stage_probe_missing_from_dataset")
            probe_report = asyncio.run(_probe_report(settings, fixture.stage_probe))
            typer.echo(json.dumps(probe_report, indent=2, ensure_ascii=False))
            if not probe_report["passed"]:
                raise typer.Exit(1)
            return
        cases = (
            fixture.cases
            if fixture is not None
            else [
                EvalCase(
                    name="long_source",
                    title="Database release",
                    article="A database release adds a documented query feature. " * 12,
                    expected_selected=True,
                ),
                EvalCase(
                    name="short_source",
                    title="Brief notice",
                    article="A short notice.",
                    expected_selected=False,
                ),
            ]
        )
        report = asyncio.run(_eval_report(settings, cases))
    except (OSError, ValidationError):
        typer.echo("Cannot read eval config or dataset; verify paths and JSON schema", err=True)
        raise typer.Exit(1) from None
    except ModelError as exc:
        typer.echo(exc.code, err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    summary = report["summary"]
    assert isinstance(summary, dict)
    if summary["matched_labels"] != len(cases):
        raise typer.Exit(1)


@editorial.command("compare")
def compare_command(
    config: Path,
    dataset: Path,
    request_budget: Annotated[int, typer.Option(min=1, max=60)] = 24,
    interval: Annotated[float, typer.Option(min=0, max=60)] = 6,
    candidate: Literal["v3", "v4"] = "v3",
    stage: Literal["score", "draft", "review"] | None = None,
) -> None:
    """Compare v2 and candidate prompts on the same labeled data and model profiles."""

    async def run_comparison(
        settings: EditorialConfig, fixture: QualityDataset
    ) -> dict[str, object]:
        profiles = settings.profiles()

        async def progress(row: dict[str, object]) -> None:
            status = row.get("error") or ("match" if row["passed"] else "mismatch")
            typer.echo(f"{row['variant']} {row['case']}: {status}", err=True)

        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            providers = {phase: provider(profile, client) for phase, profile in profiles.items()}
            report = await compare_prompts(
                fixture,
                profiles,
                providers,
                threshold=settings.threshold,
                request_budget=request_budget,
                interval=interval,
                candidate=candidate,
                describe_error=eval_error,
                on_result=progress,
            )
        report["profiles"] = {
            phase: {"backend": profile.backend, "model": profile.model}
            for phase, profile in profiles.items()
        }
        return report

    try:
        settings = EditorialConfig.model_validate_json(config.read_text(encoding="utf-8"))
        fixture = QualityDataset.model_validate_json(dataset.read_text(encoding="utf-8"))
        if stage is not None:
            fixture = QualityDataset(cases=[case for case in fixture.cases if case.stage == stage])
        report = asyncio.run(run_comparison(settings, fixture))
    except (OSError, ValidationError):
        typer.echo("Cannot read comparison config or dataset; verify paths and schema", err=True)
        raise typer.Exit(1) from None
    except ModelError as exc:
        typer.echo(exc.code, err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    if report["stopped"]:
        raise typer.Exit(1)
