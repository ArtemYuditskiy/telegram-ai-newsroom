"""CLI entry point."""

from typing import Annotated

import typer

from telegram_ai_newsroom import __version__
from telegram_ai_newsroom.cli.database import database
from telegram_ai_newsroom.cli.editorial import editorial
from telegram_ai_newsroom.cli.feeds import feeds
from telegram_ai_newsroom.cli.web import web
from telegram_ai_newsroom.cli.worker import worker

app = typer.Typer(
    name="telegram-ai-newsroom",
    help="Review-first AI newsroom for Telegram.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(feeds, name="feeds")
app.add_typer(editorial, name="editorial")
app.add_typer(web, name="web")
app.add_typer(worker, name="worker")
app.add_typer(database, name="db")


def version_callback(value: bool) -> None:
    """Print the installed package version and exit."""
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show the installed version and exit.",
        ),
    ] = False,
) -> None:
    """Run Telegram AI Newsroom commands."""


if __name__ == "__main__":  # pragma: no cover - exercised through the installed entry point
    app()
