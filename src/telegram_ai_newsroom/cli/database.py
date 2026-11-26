"""Migrate and snapshot a local SQLite database."""

import json
import os
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

database = typer.Typer(help="Manage the SQLite database.")


@database.command()
def upgrade(db: Path = Path("newsroom.db")) -> None:
    """Apply pending migrations."""
    config = Config("alembic.ini")
    url = URL.create("sqlite+aiosqlite", database=str(db.resolve())).render_as_string()
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


@database.command()
def backup(source: Path, destination: Path) -> None:
    """Copy a consistent SQLite snapshot to a new file."""
    try:
        with (
            NamedTemporaryFile(dir=destination.parent, prefix=".newsroom-backup-") as temp,
            closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as live,
            closing(sqlite3.connect(temp.name)) as snapshot,
        ):
            live.backup(snapshot, pages=256)
            snapshot.execute("PRAGMA journal_mode=DELETE")
            if snapshot.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("Snapshot failed the integrity check")
            os.link(temp.name, destination)
    except FileExistsError as exc:
        raise typer.BadParameter("Destination already exists. Choose a new file.") from exc
    except (OSError, sqlite3.Error):
        typer.echo("Could not create a SQLite snapshot. Check the paths and permissions.", err=True)
        raise typer.Exit(1) from None


@database.command()
def status(db: Path = Path("newsroom.db")) -> None:
    """Show recorded publication, feed and model outcomes without calling providers."""
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            feeds = conn.execute(
                "SELECT url, enabled, last_checked_at, last_success_at, last_error "
                "FROM feed_sources ORDER BY url"
            )
            models = conn.execute(
                "SELECT backend, model, operation, outcome, count(*) AS calls, "
                "round(avg(latency_ms), 1) AS mean_latency_ms FROM llm_calls "
                "GROUP BY backend, model, operation, outcome "
                "ORDER BY backend, model, operation, outcome"
            )
            costs = conn.execute("SELECT cost_usd FROM llm_calls WHERE cost_usd IS NOT NULL")
            deliveries = conn.execute(
                "SELECT CASE WHEN cancelled_at IS NULL THEN state ELSE 'CANCELLED' END "
                "AS effective_state, count(*) FROM publications GROUP BY effective_state"
            )
            report = {
                "database": "ok",
                "feeds": [dict(row) for row in feeds],
                "model_calls": [dict(row) for row in models],
                "reported_cost_usd": str(sum((Decimal(row[0]) for row in costs), Decimal(0))),
                "publications": {row[0].lower(): row[1] for row in deliveries},
            }
    except sqlite3.Error:
        typer.echo('{"database":"unavailable"}')
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, indent=2))
