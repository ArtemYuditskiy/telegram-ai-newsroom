"""CLI smoke tests."""

from typer.testing import CliRunner

from telegram_ai_newsroom import __version__
from telegram_ai_newsroom.cli.main import app, version_callback

runner = CliRunner()


def test_help_is_available_without_external_services() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Review-first AI newsroom" in result.stdout


def test_version_is_available_without_external_services() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_disabled_version_callback_does_nothing() -> None:
    version_callback(False)
