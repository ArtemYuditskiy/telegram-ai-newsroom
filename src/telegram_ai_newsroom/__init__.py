"""Telegram AI Newsroom."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("telegram-ai-newsroom")
except PackageNotFoundError:  # pragma: no cover - only possible outside an installed project
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
