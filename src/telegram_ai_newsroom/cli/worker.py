"""Run and reconcile Telegram publication jobs."""

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy.exc import NoResultFound
from telegram import Bot
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

from telegram_ai_newsroom.delivery_store import DeliveryStore
from telegram_ai_newsroom.domain.clock import SystemClock
from telegram_ai_newsroom.domain.workflow import InvalidTransition, PublicationState
from telegram_ai_newsroom.publisher import FakePublisher, TelegramPublisher
from telegram_ai_newsroom.sqlite import open_database
from telegram_ai_newsroom.worker import Worker

worker = typer.Typer(help="Deliver scheduled posts and reconcile uncertain outcomes.")


@worker.command()
def run(
    db: Path | None = None,
    fake: bool = False,
    once: bool = False,
    interval: Annotated[int, typer.Option(min=1, max=300)] = 5,
) -> None:
    """Run the delivery loop or process at most one due post."""
    if fake and db is None:
        raise typer.BadParameter("Use --db with a dedicated demo database in fake mode.")
    db = db or Path("newsroom.db")
    if not db.is_file():
        raise typer.BadParameter("Migrate the database before starting the worker.")
    token = os.environ.get("NEWSROOM_TELEGRAM_TOKEN", "")
    chat_id = 0
    if not fake:
        with suppress(ValueError):
            chat_id = int(os.environ.get("NEWSROOM_TELEGRAM_CHAT_ID", ""))
        if not token or chat_id >= 0:
            raise typer.BadParameter("Set NEWSROOM_TELEGRAM_TOKEN and a numeric channel chat ID.")

    logging.basicConfig(level=logging.INFO)
    # HTTP logs may contain the bot token as part of a request path.
    for name in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)

    async def start() -> None:
        engine = open_database(db)
        try:
            store = DeliveryStore(engine, SystemClock())
            if fake:
                instance = Worker(store, FakePublisher(), "fake")
            else:
                bot = Bot(token, request=HTTPXRequest(httpx_kwargs={"trust_env": False}))
                try:
                    await bot.initialize()
                    instance = Worker(store, TelegramPublisher(bot, chat_id), str(chat_id))
                    await (instance.tick() if once else instance.run(interval))
                finally:
                    await bot.shutdown()
                return
            await (instance.tick() if once else instance.run(interval))
        finally:
            await engine.dispose()

    try:
        asyncio.run(start())
    except TelegramError as exc:
        typer.echo(f"Could not initialize the Telegram bot ({type(exc).__name__}).", err=True)
        raise typer.Exit(1) from None


@worker.command("uncertain")
def list_uncertain(db: Path = Path("newsroom.db")) -> None:
    """List deliveries that need verification in the destination channel."""
    if not db.is_file():
        raise typer.BadParameter("Database file does not exist.")

    async def query() -> None:
        engine = open_database(db)
        try:
            for identity, destination in await DeliveryStore(engine, SystemClock()).uncertain():
                typer.echo(f"{identity}  {destination or 'unknown destination'}")
        finally:
            await engine.dispose()

    asyncio.run(query())


@worker.command()
def resolve(
    publication_id: UUID,
    outcome: PublicationState,
    db: Path = Path("newsroom.db"),
    message_id: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Record the verified outcome, or requeue after confirming the post is absent."""
    if not db.is_file():
        raise typer.BadParameter("Database file does not exist.")
    if outcome not in {
        PublicationState.PUBLISHED,
        PublicationState.RETRY_WAIT,
        PublicationState.FAILED,
    }:
        raise typer.BadParameter("Choose published, retry_wait or failed.")
    if (outcome == PublicationState.PUBLISHED) != (message_id is not None):
        raise typer.BadParameter("Provide --message-id exactly when recording a published post.")

    async def update() -> None:
        engine = open_database(db)
        try:
            await DeliveryStore(engine, SystemClock()).resolve(publication_id, outcome, message_id)
        finally:
            await engine.dispose()

    try:
        asyncio.run(update())
    except (InvalidTransition, NoResultFound):
        typer.echo("Publication is missing or is no longer uncertain.", err=True)
        raise typer.Exit(1) from None
