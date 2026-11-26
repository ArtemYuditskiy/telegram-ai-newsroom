"""Persist source health, ingestion decisions and extracted article text."""

import json
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    select,
    true,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from telegram_ai_newsroom.domain.clock import SystemClock, utc_timestamp
from telegram_ai_newsroom.domain.workflow import Candidate, CandidateState
from telegram_ai_newsroom.feed_http import FetchError
from telegram_ai_newsroom.ingestion import (
    Article,
    Collector,
    Entry,
    Source,
    normalized_title,
    parse_feed,
    similar_title,
)
from telegram_ai_newsroom.sqlite import candidates, metadata

sources = Table(
    "feed_sources",
    metadata,
    Column("url", Text, primary_key=True),
    Column("hosts", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=true()),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("last_checked_at", Text),
    Column("last_success_at", Text),
    Column("last_error", String(100)),
)
items = Table(
    "feed_items",
    metadata,
    Column("source_url", Text, ForeignKey("feed_sources.url"), primary_key=True),
    Column("entry_key", Text, primary_key=True),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("published_at", Text),
    Column("image_url", Text),
    Column("reason", String(100), nullable=False),
    Column("candidate_id", Uuid, ForeignKey("candidates.id")),
)
articles = Table(
    "candidate_articles",
    metadata,
    Column("candidate_id", Uuid, ForeignKey("candidates.id"), primary_key=True),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("title_key", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("published_at", Text),
    Column("image_url", Text),
    UniqueConstraint("url", name="uq_candidate_article_url"),
    UniqueConstraint("fingerprint", name="uq_candidate_article_fingerprint"),
)


class IngestionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine)
        self.record_sessions = async_sessionmaker(engine.execution_options(claim_write=True))

    async def approve(self, source: Source) -> None:
        hosts = json.dumps(sorted(source.hosts))
        async with self.sessions.begin() as session:
            await session.execute(
                insert(sources)
                .values(url=source.url, hosts=hosts)
                .on_conflict_do_update(
                    index_elements=[sources.c.url],
                    set_={"hosts": hosts, "enabled": True, "etag": None, "last_modified": None},
                )
            )

    async def enabled(self) -> list[Source]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(sources.c.url, sources.c.hosts)
                .where(sources.c.enabled.is_(True))
                .order_by(sources.c.url)
            )
            return [Source(row.url, frozenset(json.loads(row.hosts))) for row in rows]

    async def validators(self, source: Source) -> dict[str, str]:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(sources.c.etag, sources.c.last_modified).where(
                        sources.c.url == source.url
                    )
                )
            ).one()
            return {
                key: value
                for key, value in (
                    ("If-None-Match", row.etag),
                    ("If-Modified-Since", row.last_modified),
                )
                if value
            }

    async def health(
        self, source: Source, error: str | None, response_headers: dict[str, str] | None = None
    ) -> None:
        now = utc_timestamp(SystemClock().now())
        values = {"last_checked_at": now, "last_error": error}
        if error is None:
            values["last_success_at"] = now
        if response_headers is not None:
            values.update(
                etag=response_headers.get("etag"),
                last_modified=response_headers.get("last-modified"),
            )
        async with self.sessions.begin() as session:
            await session.execute(
                sources.update().where(sources.c.url == source.url).values(**values)
            )

    async def seen(self, source: Source, entry: Entry) -> bool:
        async with self.sessions() as session:
            status = await session.scalar(
                select(items.c.reason).where(
                    items.c.source_url == source.url,
                    items.c.entry_key == entry.key,
                )
            )
            return status is not None and not status.startswith("retry:")

    async def record(
        self, source: Source, entry: Entry, article: Article | None, reason: str
    ) -> str:
        async with self.record_sessions.begin() as session:
            previous_reason = await session.scalar(
                select(items.c.reason).where(
                    items.c.source_url == source.url, items.c.entry_key == entry.key
                )
            )
            if previous_reason is not None and not previous_reason.startswith("retry:"):
                return "already_seen"
            candidate_id = None
            title_key = normalized_title(entry.title)
            if article is not None:
                previous = (
                    await session.execute(
                        select(
                            articles.c.url, articles.c.fingerprint, articles.c.title_key
                        ).order_by(articles.c.candidate_id)
                    )
                ).all()
                fingerprint = article.fingerprint
                if any(row.url == article.url for row in previous):
                    reason = "duplicate_url"
                elif any(row.fingerprint == fingerprint for row in previous):
                    reason = "duplicate_text"
                elif any(similar_title(title_key, row.title_key) for row in previous):
                    reason = "duplicate_title"
                else:
                    candidate = Candidate(uuid4()).transition(CandidateState.NORMALIZED)
                    candidate = candidate.transition(CandidateState.ELIGIBLE)
                    candidate_id = candidate.id
                    await session.execute(
                        candidates.insert().values(id=candidate.id, state=candidate.state)
                    )
                    await session.execute(
                        articles.insert().values(
                            candidate_id=candidate.id,
                            url=article.url,
                            title=entry.title,
                            title_key=title_key,
                            text=article.text,
                            fingerprint=fingerprint,
                            published_at=entry.published_at,
                            image_url=article.image_url,
                        )
                    )
            values = {
                "url": entry.url,
                "title": entry.title,
                "published_at": entry.published_at,
                "image_url": entry.image_url,
                "reason": reason,
                "candidate_id": candidate_id,
            }
            await session.execute(
                insert(items)
                .values(
                    source_url=source.url,
                    entry_key=entry.key,
                    **values,
                )
                .on_conflict_do_update(
                    index_elements=[items.c.source_url, items.c.entry_key],
                    set_=values,
                )
            )
        return reason


async def collect_source(
    collector: Collector, store: IngestionStore, source: Source
) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        response = await collector.feed(source, await store.validators(source))
        if response.status == 304:
            await store.health(source, None)
            return {"not_modified": 1}
        entries = parse_feed(response)
    except FetchError as exc:
        await store.health(source, exc.code)
        return {exc.code: 1}
    retry_pending = False
    for entry in entries:
        if await store.seen(source, entry):
            reason = "already_seen"
        else:
            try:
                article = await collector.article(source, entry)
            except FetchError as exc:
                retry_pending |= exc.retryable
                reason = f"retry:{exc.code}" if exc.retryable else exc.code
                await store.record(source, entry, None, reason)
            else:
                reason = await store.record(source, entry, article, "eligible")
        counts[reason] = counts.get(reason, 0) + 1
    await store.health(
        source,
        "article_retry_pending" if retry_pending else None,
        {} if retry_pending else response.headers,
    )
    return counts
