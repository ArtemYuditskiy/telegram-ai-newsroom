"""Check the editor's decisions and persistence using offline model responses."""

import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from typer.testing import CliRunner

import telegram_ai_newsroom.cli.editorial as cli
from telegram_ai_newsroom.cli.main import app
from telegram_ai_newsroom.domain.workflow import CandidateState, DraftState
from telegram_ai_newsroom.editorial import Audit, EditorialPipeline
from telegram_ai_newsroom.editorial_store import calls, results
from telegram_ai_newsroom.ingestion import Article, Entry, Source
from telegram_ai_newsroom.ingestion_store import IngestionStore, articles
from telegram_ai_newsroom.llm import (
    FakeProvider,
    ModelError,
    ModelProfile,
    Operation,
    Reply,
    Request,
)
from telegram_ai_newsroom.sqlite import candidates, drafts, open_database

ARTICLE = "A new database release adds a documented query feature. " * 12
HOST = "example.com"
URL = f"https://{HOST}/article"


class ScriptedProvider(FakeProvider):
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.rate_limited = False

    async def complete(self, profile: ModelProfile, request: Request) -> Reply:
        if self.scenario == "invalid" and request.operation == "draft":
            return Reply("{}")
        if self.scenario == "probe" and request.operation == "draft":
            return Reply(
                json.dumps(
                    {
                        "headline": 123,
                        "body": "A draft",
                        "tags": [],
                        "supporting_facts": ["A quote", {"private": "sensitive-value"}],
                        "untrusted-field-name": "sensitive-value",
                    }
                )
            )
        if self.scenario == "probe" and request.operation == "review":
            assert "ten reconnect" in request.data["draft"]
            assert "five reconnect" in request.data["article"]
            return Reply('{"supported":false,"issues":["The article says five, not ten."]}')
        if self.scenario == "unsupported" and request.operation == "review":
            return Reply('{"supported":true,"issues":["A claim is unsupported"]}')
        if self.scenario == "unquoted" and request.operation == "draft":
            return Reply(
                '{"headline":"Release","body":"New feature","tags":[],'
                '"supporting_facts":["made up"]}'
            )
        if self.scenario == "linked" and request.operation == "draft":
            return Reply(
                json.dumps(
                    {
                        "headline": "Release",
                        "body": "Visit https://example.com",
                        "tags": [],
                        "supporting_facts": ["A new database release"],
                    }
                )
            )
        if self.scenario == "rate_limit" and not self.rate_limited:
            self.rate_limited = True
            raise ModelError("rate_limit")
        return await super().complete(profile, request)


@pytest.mark.parametrize(
    ("scenario", "audit_names"),
    [
        ("rate_limit", ["rate_limit", "ok", "ok", "ok"]),
        ("linked", ["ok", "ok"]),
        ("unquoted", ["ok", "ok"]),
        ("unsupported", ["ok", "ok", "ok"]),
    ],
)
def test_pipeline(scenario: str, audit_names: list[str]) -> None:
    profile = ModelProfile()
    provider = ScriptedProvider(scenario)
    operations: tuple[Operation, ...] = ("score", "draft", "review")
    audit: list[str] = []

    async def record(call: Audit) -> None:
        audit.append(call.outcome)

    pipeline = EditorialPipeline(
        dict.fromkeys(operations, profile), dict.fromkeys(operations, provider), record
    )
    result = asyncio.run(pipeline.run("Database release", ARTICLE))
    assert audit == audit_names
    assert result.score.total == 76
    assert result.review is not None
    assert result.review.supported is (scenario == "rate_limit")
    with pytest.raises(ModelError, match="article_exceeds_input_limit"):
        asyncio.run(pipeline.run("Long article", "x" * 60_001))


@pytest.mark.parametrize("scenario", ["valid", "invalid", "unsupported", "low_score"])
def test_editorial_cli_on_migrated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    path = tmp_path / "editorial.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        json.dumps(
            {
                "score": {},
                "draft": {},
                "review": {},
                "threshold": 90 if scenario == "low_score" else 65,
            }
        )
    )

    async def seed() -> UUID:
        engine = open_database(path)
        try:
            store = IngestionStore(engine)
            source = Source(f"https://{HOST}/rss", frozenset({HOST}))
            await store.approve(source)
            entry = Entry("id", URL, "Database release", None, None)
            await store.record(source, entry, Article(URL, ARTICLE, None), "eligible")
            async with engine.connect() as conn:
                identity = (await conn.execute(select(articles.c.candidate_id))).scalar_one()
                assert isinstance(identity, UUID)
                return identity
        finally:
            await engine.dispose()

    candidate_id = asyncio.run(seed())
    if scenario in {"invalid", "unsupported"}:
        monkeypatch.setattr(cli, "provider", lambda *_: ScriptedProvider(scenario))
    runner = CliRunner()
    args = ["editorial", "run", str(candidate_id), str(profiles), "--db", str(path)]
    response = runner.invoke(app, args)
    if scenario == "invalid":
        assert response.exit_code == 1 and "invalid_output_schema" in response.output
    else:
        assert response.exit_code == 0, response.output
        assert json.loads(response.stdout)["score"] == 76
        assert "candidate_not_eligible" in runner.invoke(app, args).output

    async def read() -> None:
        engine = open_database(path)
        try:
            async with engine.connect() as conn:
                states = {
                    "valid": CandidateState.DRAFTED,
                    "invalid": CandidateState.ELIGIBLE,
                    "unsupported": CandidateState.DRAFTED,
                    "low_score": CandidateState.REJECTED,
                }
                assert (await conn.execute(select(candidates.c.state))).scalar_one() is states[
                    scenario
                ]
                draft_state = (await conn.execute(select(drafts.c.state))).scalar_one_or_none()
                assert draft_state is {
                    "valid": DraftState.REVIEW_PENDING,
                    "unsupported": DraftState.REJECTED,
                }.get(scenario)
                assert len((await conn.execute(select(results))).all()) == (scenario != "invalid")
                outcomes = (await conn.execute(select(calls.c.outcome))).scalars().all()
                assert ("invalid_output_schema" in outcomes) is (scenario == "invalid")
        finally:
            await engine.dispose()

    asyncio.run(read())


def test_config_and_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["editorial", "eval"])
    assert result.exit_code == 0
    assert all(case["passed"] for case in json.loads(result.stdout)["cases"])
    config_path = tmp_path / "broken.json"
    config_path.write_text("{}")
    identity = "00000000-0000-0000-0000-000000000001"
    assert (
        "Invalid model configuration"
        in runner.invoke(app, ["editorial", "run", identity, str(config_path)]).output
    )
    assert (
        "verify paths"
        in runner.invoke(app, ["editorial", "run", identity, str(tmp_path / "missing.json")]).output
    )

    async def check_profiles() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ModelError, match="unsupported_structured_output"):
                cli.provider(ModelProfile(structured_output=False), client)
            gemini = ModelProfile(backend="gemini", model="test", api_key_env="TEST_MODEL_KEY")
            monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
            with pytest.raises(ModelError, match="missing_provider_api_key"):
                cli.provider(gemini, client)
            monkeypatch.setenv("TEST_MODEL_KEY", "test")
            assert type(cli.provider(gemini, client)).__name__ == "GeminiProvider"
            compatible = ModelProfile(
                backend="openai_compatible",
                base_url="https://example.com/v1",
                api_key_env="TEST_MODEL_KEY",
            )
            assert type(cli.provider(compatible, client)).__name__ == "OpenAICompatibleProvider"

    asyncio.run(check_profiles())


def test_eval_reports_selection_errors_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = Path(__file__).resolve().parents[1] / "evals/editorial-smoke.json"
    config = tmp_path / "profiles.json"
    config.write_text('{"score":{},"draft":{},"review":{}}')
    runner = CliRunner()
    result = runner.invoke(
        app, ["editorial", "eval", "--dataset", str(dataset), "--config", str(config)]
    )
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["summary"] == {
        "matched_labels": 2,
        "false_positives": 1,
        "false_negatives": 1,
        "errors": 0,
    }
    assert report["cases"][0]["draft"] and report["cases"][0]["review"]
    assert runner.invoke(app, ["editorial", "eval", "--config", str(config)]).exit_code == 1

    monkeypatch.setattr(cli, "provider", lambda *_: ScriptedProvider("invalid"))
    failed = runner.invoke(app, ["editorial", "eval", "--dataset", str(dataset)])
    assert failed.exit_code == 1
    report = json.loads(failed.stdout)
    assert report["summary"]["errors"] == 2
    assert report["cases"][0]["error"] == "invalid_output_schema"
    assert report["cases"][0]["operation"] == "draft"
    assert {"field": "headline", "type": "missing"} in report["cases"][0]["schema_errors"]


def test_stage_probe_reviews_fixed_draft_when_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = Path(__file__).resolve().parents[1] / "evals/editorial-smoke.json"
    monkeypatch.setattr(cli, "provider", lambda *_: ScriptedProvider("probe"))
    result = CliRunner().invoke(
        app, ["editorial", "eval", "--dataset", str(dataset), "--probe-stages"]
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["calls"] == [
        {"operation": "draft", "outcome": "invalid_output_schema"},
        {"operation": "review", "outcome": "ok"},
    ]
    assert report["draft"]["schema_errors"] == [
        {"field": "headline", "type": "string_type"},
        {"field": "supporting_facts[1]", "type": "string_type"},
        {"field": "<unexpected_field>", "type": "extra_forbidden"},
    ]
    assert report["review"]["passed"] is True
    assert report["passed"] is False
    assert "sensitive-value" not in result.stdout
    assert "untrusted-field-name" not in result.stdout


def test_compare_reports_paired_results_and_prevents_overbudget_run(tmp_path: Path) -> None:
    config = tmp_path / "profiles.json"
    config.write_text('{"score":{},"draft":{},"review":{}}')
    dataset = Path(__file__).resolve().parents[1] / "evals/editorial-quality.json"
    args = ["editorial", "compare", str(config), str(dataset), "--interval", "0"]
    runner = CliRunner()
    blocked = runner.invoke(app, [*args, "--request-budget", "23"])
    assert blocked.exit_code == 1
    assert "comparison_exceeds_request_budget" in blocked.stderr
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["calls"] == report["planned_requests"] == 24
    assert report["stopped"] is None
    assert report["summary"]["v2"] == report["summary"]["v3"]
    assert {row["prompt_version"] for row in report["cases"]} == {
        f"{stage}.{version}" for stage in ("score", "draft", "review") for version in ("v2", "v3")
    }
    filtered = runner.invoke(app, [*args, "--candidate", "v4", "--stage", "review"])
    assert filtered.exit_code == 0, filtered.output
    report = json.loads(filtered.stdout)
    assert report["calls"] == 8
    assert {row["prompt_version"] for row in report["cases"]} == {"review.v2", "review.v4"}


def test_compare_stops_after_repeated_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TimingOutProvider(FakeProvider):
        async def complete(self, profile: ModelProfile, request: Request) -> Reply:
            raise ModelError("timeout")

    config = tmp_path / "profiles.json"
    config.write_text('{"score":{},"draft":{},"review":{}}')
    dataset = Path(__file__).resolve().parents[1] / "evals/editorial-quality.json"
    monkeypatch.setattr(cli, "provider", lambda *_: TimingOutProvider())
    result = CliRunner().invoke(
        app, ["editorial", "compare", str(config), str(dataset), "--interval", "0"]
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["stopped"] == "repeated_timeout"
    assert report["calls"] == 2
    assert report["summary"]["v2"]["score"]["other_errors"] == 1
