"""Launch the editor with an explicitly configured admin or local demo."""

import asyncio
import os
from getpass import getpass
from pathlib import Path
from secrets import token_urlsafe
from tempfile import mkstemp
from typing import Annotated
from zoneinfo import ZoneInfoNotFoundError

import typer
import uvicorn
from argon2.exceptions import InvalidHashError

from telegram_ai_newsroom.cli.database import upgrade
from telegram_ai_newsroom.cli.editorial import EditorialConfig, execute
from telegram_ai_newsroom.ingestion import Article, Entry, Source
from telegram_ai_newsroom.ingestion_store import IngestionStore, articles
from telegram_ai_newsroom.llm import ModelProfile
from telegram_ai_newsroom.sqlite import open_database
from telegram_ai_newsroom.web.app import create_app
from telegram_ai_newsroom.web.auth import HASHER

web = typer.Typer(help="Run the editor workspace.")
DEMO_FEED = "https://newsroom.example.org/feed.xml"
SAMPLE = (
    "The Harbor Team released Harbor Driver 4.0, capping reconnects at five attempts. "
    "The new limit applies only to clients on version 4.0. The migration guide recommends "
    "testing the retry behavior in staging before updating production clients.\n\n"
    "Previous versions retried indefinitely by default. With Driver 4.0, the client returns an "
    "error after the fifth attempt. Connection strings and authentication settings still work. "
    "Maintainers recommend updating integration tests that depend on background reconnects. "
    "The notes describe no changes to query performance. A release checklist also explains how "
    "to test service behavior during an unavailable database and restore the previous driver."
)
RUNTIME_SAMPLE = (
    "Northline CI published a migration notice for its hosted Python runners. The Ubuntu image "
    "will stop preinstalling Python 3.10 on January 15. Projects that explicitly install a "
    "supported runtime will continue to run. Teams using the preinstalled version should pin "
    "their runtime in the workflow before the cutoff.\n\n"
    "Existing jobs will not be rewritten automatically. The notice includes an example workflow "
    "and a migration checklist for maintainers. The change only affects the hosted Ubuntu image. "
    "Self-hosted runners follow their own administrator settings and are outside the change."
)


async def seed(path: Path, config: EditorialConfig) -> None:
    engine = open_database(path)
    source = Source(DEMO_FEED, frozenset({"newsroom.example.org"}))
    try:
        store = IngestionStore(engine)
        await store.approve(source)
        for slug, title, text in (
            ("harbor-driver", "Harbor Driver 4.0 changes reconnect behavior", SAMPLE),
            (
                "ci-runtime",
                "Northline CI announces its runtime migration",
                RUNTIME_SAMPLE,
            ),
        ):
            url = f"https://newsroom.example.org/{slug}"
            entry = Entry(slug, url, title, "2026-11-19T12:00:00+00:00", None)
            if not await store.seen(source, entry):
                await store.record(source, entry, Article(url, text, None), "eligible")
        async with engine.connect() as conn:
            first = await conn.scalar(
                articles.select()
                .with_only_columns(articles.c.candidate_id)
                .where(articles.c.url.endswith("/harbor-driver"))
            )
        await store.health(source, None)
    finally:
        await engine.dispose()
    if first is not None:
        from telegram_ai_newsroom.llm import ModelError

        try:
            await execute(path, first, config)
        except ModelError as exc:
            if exc.code != "candidate_not_eligible":
                raise


@web.command("hash-password")
def hash_password() -> None:
    """Prompt for a password and print its Argon2id hash."""
    password = getpass("Admin password: ")
    if not 12 <= len(password) <= 256 or password != getpass("Confirm password: "):
        typer.echo("Use matching passwords of 12 to 256 characters.", err=True)
        raise typer.Exit(1)
    typer.echo(HASHER.hash(password))


@web.command()
def serve(
    db: Path | None = None,
    profiles: Annotated[Path | None, typer.Option(envvar="NEWSROOM_PROFILES")] = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    timezone: str = "UTC",
    demo: bool = False,
    local_http: bool = False,
) -> None:
    """Serve the editor UI. Demo mode uses a separate database and a temporary password."""
    if (demo or local_http) and host != "127.0.0.1":
        typer.echo("Local HTTP and demo mode require --host 127.0.0.1.", err=True)
        raise typer.Exit(1)
    if demo and profiles is not None:
        typer.echo("Demo mode uses the offline FakeProvider.", err=True)
        raise typer.Exit(1)
    config = EditorialConfig.model_validate_json(profiles.read_text()) if profiles else None
    if demo:
        if db is not None and (db.exists() or db.is_symlink()):
            typer.echo("Use a new database file for demo mode or omit --db.", err=True)
            raise typer.Exit(1)
        if db is None:
            fd, filename = mkstemp(prefix="newsroom-demo-", suffix=".db")
            os.close(fd)
            db = Path(filename)
        else:
            fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        profile = ModelProfile()
        config = EditorialConfig(score=profile, draft=profile, review=profile)
        upgrade(db)
        asyncio.run(seed(db, config))
        password = token_urlsafe(15)
        admin_hash = HASHER.hash(password)
        typer.echo(f"Local demo password: {password}")
        typer.echo(f"Local demo database: {db}")
    else:
        db = db or Path("newsroom.db")
        admin_hash = os.environ.get("NEWSROOM_ADMIN_HASH", "")
    destination = os.environ.get("NEWSROOM_TELEGRAM_CHAT_ID", "")
    try:
        app = create_app(
            db,
            admin_hash,
            secure_cookie=not (demo or local_http),
            timezone=timezone,
            config=config,
            demo=demo,
            destination=destination or None,
        )
    except (ValueError, InvalidHashError, ZoneInfoNotFoundError) as exc:
        typer.echo(f"Invalid admin hash or time zone: {exc}", err=True)
        raise typer.Exit(1) from None
    uvicorn.run(app, host=host, port=port, proxy_headers=False, access_log=False)
