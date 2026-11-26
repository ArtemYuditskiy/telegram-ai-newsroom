"""Exercise approved-source ingestion using synthetic RSS and article fixtures."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from typer.testing import CliRunner

from telegram_ai_newsroom.cli.main import app
from telegram_ai_newsroom.feed_http import FeedHttp, FetchError, Response, checked_host
from telegram_ai_newsroom.ingestion import Article, Collector, Entry, Source
from telegram_ai_newsroom.ingestion_store import IngestionStore, articles, collect_source, items
from telegram_ai_newsroom.sqlite import open_database

HOST = "news.example.com"
BASE = f"https://{HOST}"
FEED = f"{BASE}/rss"
TITLE = "A new release changes how Python services report errors"


def rss() -> bytes:
    entries = [
        ("1", "/article?utm_source=rss", TITLE),
        ("2", "/article#fragment", TITLE),
        ("3", "/mirrored", "Another headline"),
        ("4", "/short", "Short article"),
        ("5", "/temporary", "Article temporarily unavailable"),
        ("6", "/restricted", "Article blocked by robots rules"),
        ("7", "/similar", f"{TITLE}!"),
    ]
    xml = "".join(
        f"<item><guid isPermaLink='false'>{key}</guid><title>{title}</title>"
        f"<link>{BASE}{path}</link><description>RSS summary</description>"
        f'<enclosure url="{BASE}/image.png" type="image/png" length="2000"/>'
        "<pubDate>Wed, 18 Nov 2026 12:00:00 GMT</pubDate></item>"
        for key, path, title in entries
    )
    return f"<rss version='2.0'><channel><title>Fixture</title>{xml}</channel></rss>".encode()


def html(phrase: str) -> bytes:
    paragraphs = "".join(
        f"<p>Section {i}. {phrase} This release gives maintainers additional context when "
        "investigating issues in production. The team also documented migration steps and "
        "tested how older integrations behave when they receive the new fields.</p>"
        for i in range(10)
    )
    return f"<html><body><article>{paragraphs}</article></body></html>".encode()


class FixtureHttp:
    outage = True
    feed_requests: list[dict[str, str]]

    def __init__(self) -> None:
        self.feed_requests = []

    async def fetch(
        self,
        url: str,
        hosts: frozenset[str],
        *,
        mime: set[str],
        limit: int,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        allow_missing: bool = False,
        allow_redirects: bool = True,
    ) -> Response:
        assert checked_host(url) in hosts
        if url.endswith("/robots.txt"):
            return Response(200, url, {}, b"User-agent: *\nDisallow: /restricted")
        if url == FEED:
            self.feed_requests.append(headers or {})
            if headers and headers.get("If-None-Match") == '"v1"':
                return Response(304, url, {}, b"")
            return Response(
                200, url, {"etag": '"v1"', "last-modified": "Wed, 18 Nov 2026 12:00:00 GMT"}, rss()
            )
        if url.endswith("/broken"):
            raise FetchError("http_503", retryable=True)
        if url.endswith("/temporary") and self.outage:
            raise FetchError("http_503", retryable=True)
        if url.endswith("/short"):
            return Response(200, url, {}, b"<html><p>Too short.</p></html>")
        if url.endswith("/image.png"):
            assert method == "HEAD"
            return Response(200, url, {}, b"")
        assert not url.endswith("/restricted")
        phrase = {
            f"{BASE}/similar": "A different release is described in this article.",
            f"{BASE}/temporary": "A database engine now supports a new query syntax.",
        }.get(url, "The new release includes improvements to reporting service errors.")
        return Response(200, url, {}, html(phrase))


def test_collection_deduplication_and_retry(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)

    async def exercise() -> None:
        engine = open_database(path)
        store = IngestionStore(engine)
        source = Source(FEED, frozenset({HOST}))
        broken = Source(f"{BASE}/broken", frozenset({HOST}))
        http = FixtureHttp()
        collector = Collector(http)
        try:
            await store.approve(source)
            await store.approve(broken)
            assert await store.enabled() == [broken, source]
            assert await collect_source(collector, store, broken) == {"http_503": 1}
            assert await collect_source(collector, store, source) == {
                "eligible": 1,
                "duplicate_url": 1,
                "duplicate_text": 1,
                "insufficient_article_text": 1,
                "retry:http_503": 1,
                "robots_disallowed": 1,
                "duplicate_title": 1,
            }
            http.outage = False
            assert await collect_source(collector, store, source) == {
                "already_seen": 6,
                "eligible": 1,
            }
            assert await collect_source(collector, store, source) == {"not_modified": 1}
            assert http.feed_requests == [
                {},
                {},
                {"If-None-Match": '"v1"', "If-Modified-Since": "Wed, 18 Nov 2026 12:00:00 GMT"},
            ]
            async with engine.connect() as conn:
                rows = (await conn.execute(select(articles.c.url, articles.c.text))).all()
                assert len(rows) == 2
                assert all(
                    "RSS summary" not in row.text and len(row.text.split()) >= 80 for row in rows
                )
                reasons = (await conn.execute(select(items.c.reason))).scalars().all()
                assert len(reasons) == 7
                linked = await conn.scalar(
                    select(items.c.candidate_id).where(items.c.entry_key == "5")
                )
                assert linked is not None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_overlapping_collection_preserves_candidate_link(tmp_path: Path) -> None:
    path = tmp_path / "ingestion.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine, peer = open_database(path), open_database(path)
        try:
            store, other = IngestionStore(engine), IngestionStore(peer)
            source = Source(FEED, frozenset({HOST}))
            await store.approve(source)
            entry = Entry("shared", f"{BASE}/article", TITLE, None, None)
            article = Article(entry.url, "Extracted article", None)
            assert not any(
                await asyncio.gather(store.seen(source, entry), other.seen(source, entry))
            )
            results = await asyncio.gather(
                store.record(source, entry, article, "eligible"),
                other.record(source, entry, article, "eligible"),
            )
            assert sorted(results) == ["already_seen", "eligible"]
            async with engine.connect() as db:
                candidate = (await db.execute(select(articles.c.candidate_id))).scalar_one()
                assert (await db.execute(select(items.c.reason, items.c.candidate_id))).one() == (
                    "eligible",
                    candidate,
                )
        finally:
            await peer.dispose()
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a",
        "http://news.example.com/",
        "https://user@news.example.com/",
        "https://news.example.com:8443/",
    ],
)
def test_url_validation(url: str) -> None:
    with pytest.raises(FetchError):
        checked_host(url)


def test_feed_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cli.db"
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")
    http = FixtureHttp()

    monkeypatch.setattr(FeedHttp, "fetch", AsyncMock(wraps=http.fetch))
    runner = CliRunner()
    result = runner.invoke(app, ["feeds", "verify", FEED])
    assert result.exit_code == 0, result.output
    assert "words" in result.output
    result = runner.invoke(app, ["feeds", "approve", FEED, "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "Source approved" in result.output
    result = runner.invoke(app, ["feeds", "collect", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert '"eligible": 1' in result.output
    result = runner.invoke(app, ["feeds", "verify", "http://bad.example.com"])
    assert result.exit_code == 1 and "invalid_url" in result.output
    result = runner.invoke(app, ["feeds", "collect", "--db", str(tmp_path / "unmigrated.db")])
    assert result.exit_code == 1 and "check migrations" in result.output
