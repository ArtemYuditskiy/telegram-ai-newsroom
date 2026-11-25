"""Run migrations on a SQLite database."""

import asyncio
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection, make_url

# Register application tables with the shared migration metadata.
import telegram_ai_newsroom.web.store  # noqa: F401
from telegram_ai_newsroom.sqlite import metadata, open_database


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


async def run() -> None:
    url = make_url(context.config.get_main_option("sqlalchemy.url"))
    if url.drivername != "sqlite+aiosqlite" or not url.database or url.database == ":memory:":
        raise ValueError("A file-backed sqlite+aiosqlite URL is required")
    engine = open_database(Path(url.database))
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


asyncio.run(run())
