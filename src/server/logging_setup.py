"""Logging setup for the automatic sign-in server.

The server is a long-running, unattended process, so it talks through the
standard :mod:`logging` module instead of ``print``: levels, timestamps and
logger names make ``docker compose logs`` readable and greppable.

Timestamps are rendered in the hardcoded UCAS timezone (``Asia/Shanghai``), so a
log line always matches the sign-in window it describes, no matter what clock
the container itself is on.
"""

from __future__ import annotations

import logging
from datetime import datetime

from common.api import UCAS_TIMEZONE

#: ``2026-03-25 10:25:00 INFO     server.autosign: ...``
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Marks the handler this module installed, so re-configuring never doubles it.
_HANDLER_MARKER = "_ucas_handler"


class _UcasFormatter(logging.Formatter):
    """Formatter whose ``%(asctime)s`` is always in the UCAS timezone (UTC+8)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, tz=UCAS_TIMEZONE)
        return moment.strftime(datefmt or DATE_FORMAT)


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the stderr handler on the root logger.

    Safe to call more than once (for example from a test): the handler is added
    only on the first call, later calls just adjust the level.
    """
    root = logging.getLogger()
    handler = next(
        (h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(_UcasFormatter(LOG_FORMAT, DATE_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)

    # httpx/httpcore log every request at INFO; the server reports what matters.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return the logger for ``name`` (normally a module's ``__name__``)."""
    return logging.getLogger(name)
