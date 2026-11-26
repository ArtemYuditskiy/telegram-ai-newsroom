"""The editor works against migrated SQLite without external services."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from telegram_ai_newsroom.cli.editorial import EditorialConfig
from telegram_ai_newsroom.cli.main import app as cli
from telegram_ai_newsroom.cli.web import DEMO_FEED, seed
from telegram_ai_newsroom.delivery_store import DeliveryStore
from telegram_ai_newsroom.domain.workflow import DraftState
from telegram_ai_newsroom.feed_http import FetchError
from telegram_ai_newsroom.ingestion import Article, Collector, Source
from telegram_ai_newsroom.ingestion_store import IngestionStore
from telegram_ai_newsroom.llm import ModelProfile
from telegram_ai_newsroom.publisher import FakePublisher
from telegram_ai_newsroom.sqlite import drafts, open_database, publications
from telegram_ai_newsroom.web.app import create_app, local_due_at, safe_url
from telegram_ai_newsroom.web.auth import HASHER, SESSION_COOKIE, validate_admin_hash
from telegram_ai_newsroom.worker import Worker

PASSWORD = "local-integration-test"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "web.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)
    profile = ModelProfile()
    asyncio.run(seed(path, EditorialConfig(score=profile, draft=profile, review=profile)))
    return path


def csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match
    return match[1]


def sign_in(client: TestClient) -> str:
    token = csrf(client.get("/login").text)
    response = client.post("/login", data={"csrf": token, "password": PASSWORD})
    assert response.status_code == 200
    assert "Editorial queue" in response.text
    return csrf(response.text)


def candidate_link(html: str, title: str) -> str:
    match = re.search(r'href="(/candidates/[^"]+)">' + re.escape(title), html)
    assert match
    return match[1]


def action_path(html: str, action: str) -> str:
    match = re.search(r'action="(/candidates/[^"]+/' + re.escape(action) + ')"', html)
    assert match
    return match[1]


def test_editor_flow(database: Path) -> None:
    profile = ModelProfile()
    with TestClient(
        create_app(
            database,
            HASHER.hash(PASSWORD),
            timezone="Europe/Minsk",
            demo=True,
            config=EditorialConfig(score=profile, draft=profile, review=profile),
        ),
        base_url="https://testserver",
    ) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/", headers={"HX-Request": "true"}).headers["HX-Redirect"] == "/login"
        assert (
            client.post(
                "/sources/collect", data={"csrf": "none"}, follow_redirects=False
            ).status_code
            == 303
        )
        assert client.post("/login", data={"password": PASSWORD}).status_code == 403
        assert client.post("/login", json={"password": PASSWORD}).status_code == 415
        invalid = client.post(
            "/login", data={"password": "wrong", "csrf": csrf(client.get("/login").text)}
        )
        assert invalid.status_code == 401 and "was not accepted" in invalid.text
        token = sign_in(client)
        assert client.get("/login", follow_redirects=False).headers["location"] == "/"
        assert client.get("/assets/htmx.min.js").status_code == 200
        assert client.get("/assets/editor.css").headers["x-content-type-options"] == "nosniff"
        assert client.get("/?state=bogus").status_code == 400
        partial = client.get("/?state=review", headers={"HX-Request": "true"})
        assert '<section id="queue-content"' in partial.text and "<html" not in partial.text
        link = candidate_link(partial.text, "Harbor Driver 4.0 changes reconnect behavior")
        detail = client.get(link)
        assert "Source article" in detail.text and "Telegram preview" in detail.text
        assert "Editorial score" in detail.text and "Model review passed" in detail.text
        assert detail.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in detail.headers["content-security-policy"]
        original = action_path(detail.text, "approve")
        assert client.post(original, data={"csrf": "wrong"}).status_code == 403
        edit = action_path(detail.text, "edit")
        invalid = client.post(edit, data={"csrf": token, "headline": "Edit", "body": "x" * 701})
        assert invalid.status_code == 422
        assert "at most 700" in invalid.text
        changed = client.post(
            edit,
            data={
                "csrf": token,
                "headline": "Editor headline",
                "body": "The new driver attempts at most five reconnects.",
            },
        )
        assert changed.status_code == 200 and "Editor headline" in changed.text
        assert "Manual edit" in changed.text
        assert client.post(original, data={"csrf": token}).status_code == 409
        approval = action_path(changed.text, "approve")
        approved = client.post(approval, data={"csrf": token})
        assert "Schedule this revision" in approved.text
        schedule = action_path(approved.text, "schedule")
        assert (
            client.post(schedule, data={"csrf": token, "due": "bad", "timezone": "UTC"}).status_code
            == 409
        )
        due = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        scheduled = client.post(
            schedule,
            data={"csrf": token, "due": due, "timezone": "Europe/Minsk", "destination": "fake"},
        )
        assert "UTC</time> · scheduled" in scheduled.text
        cancel = action_path(scheduled.text, "cancel")
        move = action_path(scheduled.text, "reschedule")
        assert client.post(cancel, data={"csrf": "wrong"}).status_code == 403
        assert (
            client.post(move, data={"csrf": token, "due": "bad", "timezone": "UTC"}).status_code
            == 409
        )
        moved = client.post(move, data={"csrf": token, "due": due, "timezone": "America/New_York"})
        assert moved.status_code == 200 and "UTC</time> · scheduled" in moved.text
        cancelled = client.post(cancel, data={"csrf": token})
        assert cancelled.status_code == 200 and "UTC</time> · cancelled" in cancelled.text
        assert client.post(cancel, data={"csrf": token}).status_code == 409
        assert client.post(move.rsplit("/", 1)[0] + "/bad", data={"csrf": token}).status_code == 404
        assert (
            client.post(
                move.rsplit("/", 2)[0] + f"/{uuid4()}/cancel", data={"csrf": token}
            ).status_code
            == 404
        )
        scheduled = client.post(
            schedule,
            data={"csrf": token, "due": due, "timezone": "Europe/Minsk", "destination": "fake"},
        )
        assert "UTC</time> · scheduled" in scheduled.text
        assert (
            client.post(
                schedule,
                data={"csrf": token, "due": due, "timezone": "Europe/Minsk", "destination": "fake"},
            ).status_code
            == 409
        )

        class AfterDue:
            def now(self) -> datetime:
                return datetime.fromisoformat(due).replace(tzinfo=UTC) + timedelta(hours=1)

        async def deliver() -> None:
            engine = open_database(database)
            try:
                worker = Worker(DeliveryStore(engine, AfterDue()), FakePublisher(), "fake")
                assert await worker.tick() is not None
            finally:
                await engine.dispose()

        asyncio.run(deliver())
        receipt = client.get(scheduled.url)
        assert re.search(r"published\s+· Telegram #\d+", receipt.text)
        assert "Local demo" in receipt.text
        regenerate = action_path(scheduled.text, "regenerate")
        regenerated = client.post(regenerate, data={"csrf": token})
        assert regenerated.status_code == 200 and "Regenerated" in regenerated.text
        assert (
            client.post(action_path(regenerated.text, "reject"), data={"csrf": token}).status_code
            == 200
        )
        assert client.get(f"{link.split('?')[0]}?revision={uuid4()}").status_code == 404
        assert (
            client.post(regenerate.rsplit("/", 1)[0] + "/invalid", data={"csrf": token}).status_code
            == 404
        )
        ready_link = candidate_link(
            client.get("/?state=eligible").text, "Northline CI announces its runtime migration"
        )
        processed = client.post(f"{ready_link}/process", data={"csrf": token})
        assert processed.status_code == 200 and "Model review passed" in processed.text
        assert client.post(f"{ready_link}/process", data={"csrf": token}).status_code == 409
        assert client.post("/logout", data={"csrf": token}).url.path == "/login"
        assert SESSION_COOKIE not in client.cookies

    async def verify() -> None:
        engine = open_database(database)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        select(drafts.c.text, drafts.c.state, publications.c.due_at)
                        .join(publications, drafts.c.id == publications.c.draft_revision_id)
                        .where(publications.c.cancelled_at.is_(None))
                    )
                ).all()
                assert len(rows) == 1
                assert rows[0].text.startswith("Editor headline")
                assert rows[0].state is DraftState.APPROVED
                assert rows[0].due_at.endswith("+00:00")
        finally:
            await engine.dispose()

    asyncio.run(verify())


def test_editor_confirms_destination_for_existing_schedule(database: Path) -> None:
    async def remove_destination() -> None:
        engine = open_database(database)
        try:
            async with engine.begin() as conn:
                await conn.execute(publications.update().values(destination=None))
        finally:
            await engine.dispose()

    with TestClient(
        create_app(database, HASHER.hash(PASSWORD), destination="-10001"),
        base_url="https://testserver",
    ) as client:
        token = sign_in(client)
        link = candidate_link(
            client.get("/?state=review").text, "Harbor Driver 4.0 changes reconnect behavior"
        )
        approved = client.post(action_path(client.get(link).text, "approve"), data={"csrf": token})
        schedule = action_path(approved.text, "schedule")
        due = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        data = {"csrf": token, "due": due, "timezone": "UTC", "destination": "-10002"}
        assert client.post(schedule, data=data).status_code == 409
        data["destination"] = "-10001"
        assert client.post(schedule, data=data).status_code == 200
        asyncio.run(remove_destination())
        detail = client.get(link)
        assert "Destination not confirmed" in detail.text
        bind = action_path(detail.text, "bind")
        assert client.post(bind, data={"csrf": token, "destination": "-10002"}).status_code == 409
        fixed = client.post(bind, data={"csrf": token, "destination": "-10001"})
        assert fixed.status_code == 200 and "Telegram channel -10001" in fixed.text
        assert "Destination not confirmed" not in fixed.text


def test_source_management(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://extra.example.org/rss"

    async def verify(self: Collector, source: Source) -> Article:
        if source.url != url:
            raise FetchError("sample_failed")
        return Article(url, "Verified sample", None)

    monkeypatch.setattr(Collector, "verify", verify)

    async def collect(
        collector: Collector, store: IngestionStore, source: Source
    ) -> dict[str, int]:
        return {"eligible": 1}

    monkeypatch.setattr("telegram_ai_newsroom.web.app.collect_source", collect)
    with TestClient(create_app(database, HASHER.hash(PASSWORD), secure_cookie=False)) as client:
        token = sign_in(client)
        assert DEMO_FEED in client.get("/sources").text
        assert (
            client.post("/sources/disable", data={"csrf": token, "url": DEMO_FEED}).status_code
            == 200
        )
        assert "Paused" in client.get("/sources").text
        assert (
            client.post("/sources/enable", data={"csrf": token, "url": DEMO_FEED}).status_code
            == 200
        )
        assert client.post("/sources/enable", data={"csrf": token, "url": url}).status_code == 404
        assert (
            client.post("/sources/approve", data={"csrf": token, "url": "bad"}).status_code == 422
        )
        assert (
            client.post(
                "/sources/approve",
                data={"csrf": token, "url": url, "hosts": "abc.example.org/path"},
            ).status_code
            == 422
        )
        assert client.post("/sources/approve", data={"csrf": token, "url": url}).status_code == 200
        assert url in client.get("/sources").text
        assert "eligible: 1" in client.post("/sources/collect", data={"csrf": token}).text
        assert client.post("/sources/invalid", data={"csrf": token}).status_code == 404
        failed = client.post("/sources/approve", data={"csrf": token, "url": DEMO_FEED})
        assert failed.status_code == 422 and "sample_failed" in failed.text


def test_security_failures_and_local_schedule() -> None:
    assert safe_url("javascript:alert(1)") is None
    with pytest.raises(ValueError):
        validate_admin_hash(HASHER.hash(PASSWORD).replace("argon2id", "argon2i"))
    now = datetime(2025, 1, 1, tzinfo=UTC)
    assert local_due_at("2025-07-10T08:00", "Europe/Minsk", now).hour == 5
    for value in ("2025-03-09T02:30", "2025-11-02T01:30"):
        with pytest.raises(ValueError, match="ambiguous or does not exist"):
            local_due_at(value, "America/New_York", now)
    with pytest.raises(ValueError, match="future"):
        local_due_at("2024-01-01T12:00", "UTC", now)
    runner = CliRunner()
    assert runner.invoke(cli, ["web", "serve", "--demo", "--host", "0.0.0.0"]).exit_code == 1
    assert runner.invoke(cli, ["web", "serve", "--demo", "--profiles", "ignored"]).exit_code == 1


def test_editor_cli_starts_only_with_valid_settings(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEWSROOM_ADMIN_HASH", raising=False)
    runner = CliRunner()
    serve = ["web", "serve"]
    assert runner.invoke(cli, serve).exit_code == 1
    monkeypatch.setattr("telegram_ai_newsroom.cli.web.getpass", lambda _: PASSWORD)
    result = runner.invoke(cli, ["web", "hash-password"])
    assert result.exit_code == 0
    admin_hash = result.output.strip()
    assert HASHER.verify(admin_hash, PASSWORD)
    monkeypatch.setenv("NEWSROOM_ADMIN_HASH", admin_hash)
    assert runner.invoke(cli, [*serve, "--timezone", "Invalid/Zone"]).exit_code == 1
    assert runner.invoke(cli, [*serve, "--db", str(database), "--demo"]).exit_code == 1

    def run(app: object, **settings: object) -> None:
        assert callable(app)
        assert settings == {
            "host": "127.0.0.1",
            "port": 8000,
            "proxy_headers": False,
            "access_log": False,
        }

    monkeypatch.setattr("telegram_ai_newsroom.cli.web.uvicorn.run", run)
    assert runner.invoke(cli, [*serve, "--db", str(database), "--local-http"]).exit_code == 0
    demo_path = tmp_path / "demo.db"
    result = runner.invoke(cli, [*serve, "--db", str(demo_path), "--demo"])
    assert result.exit_code == 0, result.output
    assert "Local demo password:" in result.output
    assert demo_path.stat().st_mode & 0o777 == 0o600
