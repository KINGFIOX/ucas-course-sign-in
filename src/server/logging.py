"""Logging setup for the automatic sign-in server.

The server is a long-running, unattended process, so it talks through the
standard :mod:`logging` module instead of ``print``: levels, timestamps and
logger names make ``docker compose logs`` readable and greppable.

Notifications are log-driven too: :func:`attach_notify_handler` installs a
handler that turns every record at :data:`NOTIFY_LEVEL` (``WARNING``) or above
into a push message. Modules therefore just log a warning or an error where
something went wrong -- they never call the notifier directly.

Timestamps are rendered in the hardcoded UCAS timezone (``Asia/Shanghai``), so a
log line always matches the sign-in window it describes, no matter what clock
the container itself is on.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from common.api import UCAS_TIMEZONE

from .notify import Message, Notifier

#: ``2026-03-25 10:25:00 INFO     server.autosign: ...``
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Records at this level or above become notifications.
NOTIFY_LEVEL = logging.WARNING

#: Markers so re-configuring never doubles a handler.
_HANDLER_MARKER = "_ucas_handler"
_NOTIFY_MARKER = "_ucas_notify_handler"


class _UcasFormatter(logging.Formatter):
    """Formatter whose ``%(asctime)s`` is always in the UCAS timezone (UTC+8)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, tz=UCAS_TIMEZONE)
        return moment.strftime(datefmt or DATE_FORMAT)


class NotifyHandler(logging.Handler):
    """Forward log records at ``level`` or above to a :class:`Notifier`.

    The notification body is the plain log message -- no timestamp, level or
    traceback, which keeps the push short. A record can set
    ``extra={"title": ...}`` to control the title; otherwise
    ``"{prefix}: {levelname}"`` is used.
    """

    def __init__(
        self,
        notifier: Notifier,
        level: int = NOTIFY_LEVEL,
        prefix: str = "UCAS auto sign-in",
    ) -> None:
        super().__init__(level)
        self.notifier = notifier
        self.prefix = prefix

    def emit(self, record: logging.LogRecord) -> None:
        title = getattr(record, "title", "") or f"{self.prefix}: {record.levelname.lower()}"
        self.notifier.send(Message(title=title, body=record.getMessage()))


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


def attach_notify_handler(notifier: Notifier, level: int = NOTIFY_LEVEL) -> None:
    """Push every record at ``level`` or above through ``notifier``.

    Any handler installed by a previous call is replaced, so calling this again
    (or from a test) never doubles the pushes.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _NOTIFY_MARKER, False):
            root.removeHandler(handler)
    notify_handler = NotifyHandler(notifier, level)
    setattr(notify_handler, _NOTIFY_MARKER, True)
    root.addHandler(notify_handler)


def get_logger(name: str) -> logging.Logger:
    """Return the logger for ``name`` (normally a module's ``__name__``)."""
    return logging.getLogger(name)
