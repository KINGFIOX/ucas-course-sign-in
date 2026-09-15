"""Hourly sign-in server.

The one-pass routine in :mod:`server.autosign` performs a single run; the
:class:`Server` keeps calling it **at the top of every hour**, which is what the
deployment wants: run once per hour and only make noise when there is something
to say.

The loop is deliberately simple and stateless: compute the next hour boundary
from the UCAS clock (``Asia/Shanghai``, hardcoded in :mod:`common.api` and also
the timezone the UCAS schedule uses), sleep until then, run, repeat. No cron, no
APScheduler dependency.

Lifecycle follows the usual server shape (as in ``sgl-project/mini-sglang``):
:meth:`Server.run_forever` blocks until shutdown, and shutdown is simply a
``KeyboardInterrupt``. ``SIGTERM`` is turned into one so ``docker stop`` and
``Ctrl+C`` take exactly the same path, and :meth:`Server.shutdown` releases the
resources.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timedelta

from common.api import UCAS_TIMEZONE, UcasClient
from common.error import UcasError

from .autosign import AutosignConfig, AutosignConfigError, run_once
from .logging import attach_notify_handler, configure_logging, get_logger
from .notify import Notifier, NotifyConfigError, build_notifier

logger = get_logger(__name__)


def seconds_until_next_run(now: datetime) -> float:
    """Seconds from ``now`` until the next hour boundary.

    For example at 13:59:40 it returns 20 seconds; at 14:00:00 it returns 3600.
    The result is always strictly positive.
    """
    boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1.0, (boundary - now).total_seconds())


def install_signal_handlers() -> None:
    """Turn ``SIGTERM`` / ``SIGINT`` into the single shutdown path.

    A server shuts down on ``KeyboardInterrupt``; routing the signals there
    keeps ``Ctrl+C`` and ``docker stop`` (SIGTERM) identical.
    """

    def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
        logger.info("received signal %s, shutting down gracefully", signum)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _raise_keyboard_interrupt)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass


class Server:
    """Run one sign-in pass per hour until shut down."""

    def __init__(self, config: AutosignConfig, client: UcasClient, notifier: Notifier) -> None:
        self.config = config
        self.client = client
        self.notifier = notifier

    def run_forever(self) -> None:
        """Run the hourly loop. Only returns on shutdown (``KeyboardInterrupt``)."""
        if self.config.run_on_start:
            logger.info("run-on-start enabled: doing an initial pass")
            self.run_pass()
        else:
            logger.info("run-on-start disabled: waiting for the next hour boundary")

        while True:
            now = datetime.now(UCAS_TIMEZONE)
            delay = seconds_until_next_run(now)
            target = now + timedelta(seconds=delay)
            logger.info("sleeping %.0fs until %s", delay, target.isoformat(timespec="seconds"))
            time.sleep(delay)
            self.run_pass()

    def run_pass(self) -> None:
        """Run one pass, keeping the server alive across unexpected errors.

        ``run_once`` already handles every expected upstream failure; this is the
        last line of defence so a bug or a surprise response cannot kill an
        unattended process. The record is logged at ``ERROR``, so the traceback
        goes to stderr and the notify handler pushes the short message.
        """
        try:
            run_once(self.client, self.config)
        except UcasError as exc:
            # run_once already catches the expected ones, so this is a bug.
            logger.exception(
                "unhandled UCAS error: %s (code %s, stage %s)",
                exc.message,
                exc.code,
                exc.stage,
                extra={"title": "UCAS auto sign-in: runtime error"},
            )
        except Exception as exc:  # noqa: BLE001 - a server must not die here
            logger.exception(
                "unexpected failure: %s",
                exc,
                extra={"title": "UCAS auto sign-in: runtime error"},
            )

    def shutdown(self) -> None:
        """Release the HTTP client and the notifier."""
        self.client.close()
        self.notifier.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``server`` console script."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in args):
        print(_USAGE, end="")
        return 0

    configure_logging()

    try:
        config = AutosignConfig.from_env()
    except AutosignConfigError as exc:
        logger.fatal("configuration error: %s", exc)
        return 2

    client = UcasClient()
    try:
        notifier = build_notifier()
    except NotifyConfigError as exc:
        logger.fatal("notification configuration error: %s", exc)
        client.close()
        return 2

    # Every WARNING or worse is pushed from now on.
    attach_notify_handler(notifier)

    server = Server(config, client, notifier)
    install_signal_handlers()
    logger.info("server started (timezone=%s)", datetime.now(UCAS_TIMEZONE).tzname())
    try:
        server.run_forever()
    except KeyboardInterrupt:
        logger.info("server exiting gracefully")
    finally:
        server.shutdown()
    logger.info("server stopped")
    return 0


_USAGE = """\
usage: server

Run the automatic sign-in every hour (at the top of the hour) until stopped.
The first pass runs immediately unless UCAS_RUN_ON_START=0.

environment:
  UCAS_USERNAME                 UCAS mail (required)
  UCAS_PASSWORD                 password (required)
  UCAS_FEISHU_WEBHOOK           Feishu custom-bot webhook (required)
  UCAS_FEISHU_SECRET            Feishu signature secret (optional)
  UCAS_RUN_ON_START             run a pass immediately (default: 1)

Run with the UCAS clock: the schedule is always evaluated in Asia/Shanghai,
which is hardcoded and does not depend on the container's TZ. No local state
is kept -- every pass re-fetches the courses and trusts the signed flag UCAS
returns.
Notifications are log-driven: every WARNING or worse from this package is
pushed to Feishu. See README.md for details.
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
