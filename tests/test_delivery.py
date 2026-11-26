"""Verify delivery boundaries with migrated SQLite and a simulated Bot API."""

import asyncio
import signal
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import select
from telegram import Bot, InputFile
from telegram.request import HTTPXRequest
from typer.testing import CliRunner

from telegram_ai_newsroom.cli.main import app
from telegram_ai_newsroom.delivery_store import Delivery, DeliveryStore
from telegram_ai_newsroom.domain.clock import SystemClock
from telegram_ai_newsroom.domain.workflow import (
    CandidateState,
    DraftState,
    InvalidTransition,
    PublicationState,
)
from telegram_ai_newsroom.feed_http import FeedHttp, FetchError, Response
from telegram_ai_newsroom.ingestion import Collector, Source
from telegram_ai_newsroom.publisher import (
    DeliveryError,
    FakePublisher,
    TelegramPublisher,
    build_payload,
    download_photo,
    valid_photo,
)
from telegram_ai_newsroom.sqlite import WorkflowStore, attempts, open_database, publications
from telegram_ai_newsroom.web.store import EditorStore
from telegram_ai_newsroom.worker import MAX_ATTEMPTS, RECOVERY_AGE, Worker


class FixedClock:
    instant = datetime(2026, 11, 20, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "queue.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")
    return path


async def schedule(store: WorkflowStore, when: datetime, destination: str | None = "fake") -> UUID:
    async with store.transaction() as tx:
        candidate = await tx.discover(uuid4())
        for state in (
            CandidateState.NORMALIZED,
            CandidateState.ELIGIBLE,
            CandidateState.SCORED,
            CandidateState.SELECTED,
        ):
            await tx.advance_candidate(candidate.id, state)
        draft = await tx.create_draft(uuid4(), candidate.id, "Headline\n\nApproved body")
        await tx.review(draft.id, DraftState.APPROVED)
        return (await tx.schedule(uuid4(), draft.id, when, destination=destination)).id


def test_schedule_changes_and_competing_claim(database: Path) -> None:
    async def exercise() -> None:
        engine = open_database(database)
        peer = open_database(database)
        try:
            clock = FixedClock()
            editor = EditorStore(engine)
            delivery = DeliveryStore(peer, clock)
            first = await schedule(WorkflowStore(engine, clock), clock.now())
            async with engine.connect() as db:
                candidate, draft = (
                    await db.execute(
                        select(
                            publications.c.candidate_id,
                            publications.c.draft_revision_id,
                        )
                    )
                ).one()
            await editor.change_schedule(candidate, first, clock.now() + timedelta(hours=1))
            assert await delivery.claim("fake") is None
            await editor.change_schedule(candidate, first, None)
            clock.instant += timedelta(hours=2)
            assert await delivery.claim("fake") is None

            outcomes = await asyncio.gather(
                editor.schedule(candidate, draft, clock.now(), "fake"),
                EditorStore(peer).schedule(candidate, draft, clock.now(), "fake"),
                return_exceptions=True,
            )
            assert outcomes.count(None) == 1
            assert sum(isinstance(item, InvalidTransition) for item in outcomes) == 1
            async with engine.connect() as db:
                second = (
                    await db.execute(
                        select(publications.c.id).where(publications.c.cancelled_at.is_(None))
                    )
                ).scalar_one()
            claim, cancel = await asyncio.gather(
                delivery.claim("fake"),
                editor.change_schedule(candidate, second, None),
                return_exceptions=True,
            )
            if claim is None:
                assert cancel is None
            else:
                assert isinstance(claim, Delivery)
                assert isinstance(cancel, InvalidTransition)
                assert "started sending" in str(cancel)
        finally:
            await peer.dispose()
            await engine.dispose()

    asyncio.run(exercise())


def test_schedule_migration_reports_legacy_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "0005_delivery")
    candidate, draft, first, second = (uuid4().hex for _ in range(4))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO candidates VALUES (?, 'DRAFTED')", (candidate,))
        db.execute(
            "INSERT INTO draft_revisions (id, candidate_id, text, state) "
            "VALUES (?, ?, 'body', 'APPROVED')",
            (draft, candidate),
        )
        db.executemany(
            "INSERT INTO publications (id, draft_revision_id, due_at, state) "
            "VALUES (?, ?, '2026-11-20T00:00:00+00:00', 'SCHEDULED')",
            [(first, draft), (second, draft)],
        )
    with pytest.raises(RuntimeError, match=f"conflicts before upgrading: {candidate}"):
        command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE publications SET state = 'FAILED' WHERE id = ?", (second,))
    command.upgrade(config, "head")
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT candidate_id FROM publications").fetchall()
        assert rows == [(candidate,), (candidate,)]


def test_legacy_destination_requires_confirmation(database: Path) -> None:
    async def exercise() -> None:
        engine = open_database(database)
        try:
            clock = FixedClock()
            store = DeliveryStore(engine, clock)
            identity = await schedule(WorkflowStore(engine, clock), clock.now(), None)
            async with engine.connect() as db:
                candidate = await db.scalar(select(publications.c.candidate_id))
            assert isinstance(candidate, UUID)
            assert await store.claim("-10001") is None
            editor = EditorStore(engine)
            await editor.change_schedule(candidate, identity, None, bind_destination="-10001")
            with pytest.raises(InvalidTransition, match="already has a destination"):
                await editor.change_schedule(candidate, identity, None, bind_destination="-10002")
            assert await store.claim("-10002") is None
            worker = Worker(store, FakePublisher(), "-10001")
            assert await worker.tick() == identity
            assert await worker.tick() is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_concurrent_claim_recovery_and_reconciliation(database: Path) -> None:
    async def exercise() -> None:
        engine = open_database(database)
        peer = open_database(database)
        clock = FixedClock()
        try:
            initial = clock.now()
            identity = await schedule(WorkflowStore(engine, clock), initial, "-10001")
            later = await schedule(WorkflowStore(engine, clock), initial + timedelta(hours=1))
            store = DeliveryStore(engine, clock)
            another = DeliveryStore(peer, clock)
            claims = await asyncio.gather(store.claim("-10001"), another.claim("-10001"))
            assert [job.id for job in claims if job] == [identity]
            assert await another.recover(initial - RECOVERY_AGE) == 0
            clock.instant += RECOVERY_AGE
            restored = DeliveryStore(peer, clock)
            assert await restored.recover(clock.now() - RECOVERY_AGE) == 1
            assert await Worker(restored, FakePublisher(), "-10001").tick() is None
            assert await restored.uncertain() == [(identity, "-10001")]
            await restored.resolve(identity, PublicationState.PUBLISHED, 17)
            assert not await restored.uncertain()
            async with peer.connect() as conn:
                row = (
                    await conn.execute(
                        select(publications.c.state, publications.c.message_id).where(
                            publications.c.id == identity
                        )
                    )
                ).one()
                assert row == (PublicationState.PUBLISHED, 17)
                audit = (
                    await conn.execute(
                        select(attempts.c.detail).where(attempts.c.publication_id == identity)
                    )
                ).scalar_one()
                assert audit == "worker_interrupted"
            clock.instant = initial + timedelta(hours=1)
            assert await Worker(store, FakePublisher(), "fake").tick() == later
            assert await Worker(store, FakePublisher(), "fake").tick() is None
        finally:
            await engine.dispose()
            await peer.dispose()

    asyncio.run(exercise())


def test_retry_wait_and_attempt_cap(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("telegram_ai_newsroom.worker.uniform", lambda *_: 0)

    class RateLimited:
        async def send(self, job: Delivery) -> int:
            raise DeliveryError(PublicationState.RETRY_WAIT, "rate_limited", 300)

    async def exercise() -> None:
        clock = FixedClock()
        engine = open_database(database)
        try:
            identity = await schedule(WorkflowStore(engine, clock), clock.now(), "-10001")
            later = await schedule(
                WorkflowStore(engine, clock),
                clock.now() + timedelta(seconds=MAX_ATTEMPTS * 300 - 1),
                "-10001",
            )
            store = DeliveryStore(engine, clock)
            worker = Worker(store, RateLimited(), "-10001")
            for _ in range(MAX_ATTEMPTS):
                assert await worker.tick() == identity
                clock.instant += timedelta(seconds=299)
                assert await worker.tick() is None
                clock.instant += timedelta(seconds=1)
            async with engine.connect() as conn:
                row = await conn.scalar(
                    select(publications.c.state).where(publications.c.id == identity)
                )
                assert row == PublicationState.FAILED
            assert await Worker(store, FakePublisher(), "-10001").tick() == later
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_bot_api_photo_fallback_and_ambiguous_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://news.example.org/photo.png"
    job = Delivery(
        uuid4(),
        uuid4(),
        1,
        "🚀 Title\n\nBody <literal>",
        "https://news.example.org/article",
        url,
        frozenset({"news.example.org"}),
    )
    payload = build_payload(job)
    assert payload.entities[0].length == 8
    assert payload.entities[1].offset == 26
    assert "Body <literal>" in payload.text
    photo_calls: list[bytes] = []
    mode = "fallback"

    async def download(image_url: str, hosts: frozenset[str]) -> InputFile:
        assert image_url == url and hosts == job.image_hosts
        return InputFile(b"fixture", filename="photo.png")

    monkeypatch.setattr("telegram_ai_newsroom.publisher.download_photo", download)

    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"id": 10, "is_bot": True, "first_name": "Fixture"},
                },
            )
        if mode == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if mode == "connect":
            raise httpx.ConnectTimeout("cannot connect", request=request)
        if mode == "limit":
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 50},
                },
            )
        if mode == "forbidden":
            return httpx.Response(403, json={"ok": False, "description": "Forbidden"})
        if request.url.path.endswith("/sendPhoto"):
            photo_calls.append(request.read())
            if len(photo_calls) == 1:
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "description": "Bad Request: failed to get HTTP URL content",
                    },
                )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 99,
                    "date": 0,
                    "chat": {"id": -10001, "type": "channel"},
                },
            },
        )

    async def exercise() -> None:
        nonlocal mode
        request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(response)})
        async with Bot("10:fixture", request=request) as bot:
            publisher = TelegramPublisher(bot, -10001)
            assert await publisher.send(job) == 99
            assert len(photo_calls) == 2
            assert b'filename="photo.png"' in photo_calls[-1]
            long_text = Delivery(
                uuid4(),
                uuid4(),
                1,
                "🚀" * 600,
                None,
                url,
                job.image_hosts,
            )
            assert await publisher.send(long_text) == 99
            for mode, state in (
                ("timeout", PublicationState.UNCERTAIN),
                ("connect", PublicationState.RETRY_WAIT),
                ("limit", PublicationState.RETRY_WAIT),
                ("forbidden", PublicationState.FAILED),
            ):
                with pytest.raises(DeliveryError) as error:
                    await publisher.send(job)
                assert error.value.state == state
                if mode == "limit":
                    assert error.value.retry_after == 50
            assert len(photo_calls) == 2

    asyncio.run(exercise())


def test_image_download_checks_type_and_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    png = BytesIO()
    Image.new("RGB", (10, 10)).save(png, format="PNG")
    assert valid_photo(png.getvalue(), "image/png")
    assert not valid_photo(png.getvalue(), "image/jpeg")
    assert not valid_photo(b"<html>error</html>", "image/png")
    data = png.getvalue()

    async def robots(collector: Collector, url: str, source: Source) -> None:
        if "news.example.org" not in source.hosts:
            raise FetchError("unapproved_host")

    async def fetch(http: FeedHttp, url: str, hosts: frozenset[str], **options: object) -> Response:
        assert options["limit"] == 5_000_000 and options["allow_redirects"] is False
        return Response(200, url, {"content-type": "image/png"}, data)

    monkeypatch.setattr(Collector, "check_robots", robots)
    monkeypatch.setattr(FeedHttp, "fetch", fetch)
    url = "https://news.example.org/photo.png"
    hosts = frozenset({"news.example.org"})
    assert isinstance(asyncio.run(download_photo(url, hosts)), InputFile)
    assert asyncio.run(download_photo(url, frozenset())) is None
    data = b"wrong format"
    assert asyncio.run(download_photo(url, hosts)) is None


def test_scheduler_ticks_recovers_after_job_error_and_drains(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        engine = open_database(database)
        loop = asyncio.get_running_loop()
        handlers: dict[signal.Signals, Callable[[], None]] = {}
        seen = 0
        finalized = False

        def register(sig: signal.Signals, handler: Callable[[], None]) -> None:
            handlers[sig] = handler

        monkeypatch.setattr(loop, "add_signal_handler", register)
        monkeypatch.setattr(loop, "remove_signal_handler", handlers.pop)
        try:
            clock = SystemClock()
            store = DeliveryStore(engine, clock)
            identity = await schedule(WorkflowStore(engine, clock), clock.now())
            worker = Worker(store, FakePublisher(), "fake")
            original = worker.tick

            async def tick() -> UUID | None:
                nonlocal seen, finalized
                seen += 1
                if seen == 1:
                    raise RuntimeError("temporary polling error")
                handlers[signal.SIGTERM]()
                result = await original()
                finalized = True
                return result

            monkeypatch.setattr(worker, "tick", tick)
            await asyncio.wait_for(worker.run(1), timeout=5)
            assert seen == 2 and finalized and not handlers
            async with engine.connect() as conn:
                state = await conn.scalar(
                    select(publications.c.state).where(publications.c.id == identity)
                )
                assert state == PublicationState.PUBLISHED
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_deadline_moves_job_to_uncertain(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("telegram_ai_newsroom.worker.ATTEMPT_TIMEOUT", 0)

    class SlowPublisher:
        async def send(self, job: Delivery) -> int:
            await asyncio.sleep(60)
            return 1

    async def exercise() -> None:
        clock = FixedClock()
        engine = open_database(database)
        try:
            store = DeliveryStore(engine, clock)
            identity = await schedule(WorkflowStore(engine, clock), clock.now())
            worker = Worker(store, SlowPublisher(), "fake")
            assert await worker.tick() == identity
            assert await store.uncertain() == [(identity, "fake")]
            assert await worker.tick() is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_worker_cli_fake_and_resolve(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWSROOM_TELEGRAM_TOKEN", raising=False)
    runner = CliRunner()
    db = ["--db", str(database)]

    async def seed() -> tuple[UUID, UUID]:
        engine = open_database(database)
        clock = FixedClock()
        try:
            store = WorkflowStore(engine, clock)
            uncertain = await schedule(store, clock.now() - timedelta(seconds=1))
            pending = await schedule(store, clock.now())
            delivery = await DeliveryStore(engine, clock).claim("fake")
            assert delivery and delivery.id == uncertain
            await DeliveryStore(engine, clock).finish(delivery, PublicationState.UNCERTAIN)
            return uncertain, pending
        finally:
            await engine.dispose()

    uncertain, pending = asyncio.run(seed())
    assert runner.invoke(app, ["worker", "run", "--fake", "--once"]).exit_code != 0
    assert runner.invoke(app, ["worker", "run", *db, "--once"]).exit_code != 0
    listed = runner.invoke(app, ["worker", "uncertain", *db])
    assert str(uncertain) in listed.output
    result = runner.invoke(app, ["worker", "run", *db, "--fake", "--once"])
    assert result.exit_code == 0, result.output
    args = ["worker", "resolve", str(uncertain), "published", *db]
    assert runner.invoke(app, args).exit_code != 0
    assert runner.invoke(app, [*args, "--message-id", "91"]).exit_code == 0
    assert runner.invoke(app, [*args, "--message-id", "91"]).exit_code != 0

    async def verify() -> None:
        engine = open_database(database)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    select(publications.c.id, publications.c.state, publications.c.message_id)
                )
                outcomes = {row.id: (row.state, row.message_id) for row in rows}
                assert outcomes[uncertain] == (PublicationState.PUBLISHED, 91)
                assert outcomes[pending][0] == PublicationState.PUBLISHED
                assert outcomes[pending][1] > 0
        finally:
            await engine.dispose()

    asyncio.run(verify())
