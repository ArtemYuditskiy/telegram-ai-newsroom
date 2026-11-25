"""Write structured events with explicitly selected, non-secret fields."""

import json
import logging
from datetime import UTC, datetime


def event(logger: logging.Logger, level: int, name: str, **fields: object) -> None:
    if logger.isEnabledFor(level):
        logger.log(
            level,
            json.dumps(
                {"time": datetime.now(UTC).isoformat(), "event": name, **fields},
                separators=(",", ":"),
            ),
        )
