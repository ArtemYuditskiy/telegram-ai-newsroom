"""Commands for approving and collecting RSS feeds."""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal

import typer
from sqlalchemy.exc import OperationalError

from telegram_ai_newsroom.feed_http import FeedHttp, FetchError, checked_host
from telegram_ai_newsroom.ingestion import Collector, Source
from telegram_ai_newsroom.ingestion_store import IngestionStore, collect_source
from telegram_ai_newsroom.sqlite import open_database

feeds = typer.Typer(help="Approve and collect RSS/Atom sources.")
Database = Annotated[Path, typer.Option(help="Path to a migrated SQLite database.")]


async def run(
    db: Path, action: Literal["verify", "approve", "collect"], source: Source | None = None
) -> None:
    async with FeedHttp.session_context() as session:
        collector = Collector(FeedHttp(session))
        if source is not None:
            article = await collector.verify(source)
            typer.echo(f"{article.url} — {len(article.text.split())} words")
            if action == "verify":
                return
        engine = open_database(db)
        try:
            store = IngestionStore(engine)
            if source is not None:
                await store.approve(source)
                typer.echo("Source approved")
            else:
                for enabled in await store.enabled():
                    counts = await collect_source(collector, store, enabled)
                    typer.echo(json.dumps({"source": enabled.url, "results": counts}))
        finally:
            await engine.dispose()


def _check(url: str, db: Path, hosts: list[str] | None, *, approve: bool) -> None:
    try:
        source = Source(url, frozenset([checked_host(url), *(hosts or [])]))
        asyncio.run(run(db, "approve" if approve else "verify", source))
    except (FetchError, OperationalError) as exc:
        typer.echo(
            str(exc) if isinstance(exc, FetchError) else "SQLite error; check migrations", err=True
        )
        raise typer.Exit(1) from exc


@feeds.command()
def verify(
    url: str,
    host: Annotated[list[str] | None, typer.Option(help="Additional approved hostname.")] = None,
) -> None:
    """Check a feed and sample article."""
    _check(url, Path("newsroom.db"), host, approve=False)


@feeds.command()
def approve(
    url: str,
    host: Annotated[list[str] | None, typer.Option(help="Additional approved hostname.")] = None,
    db: Database = Path("newsroom.db"),
) -> None:
    """Check a feed and enable it for subsequent runs."""
    _check(url, db, host, approve=True)


@feeds.command()
def collect(db: Database = Path("newsroom.db")) -> None:
    """Collect articles from approved sources."""
    try:
        asyncio.run(run(db, "collect"))
    except OperationalError as exc:
        typer.echo("SQLite error; check migrations", err=True)
        raise typer.Exit(1) from exc
