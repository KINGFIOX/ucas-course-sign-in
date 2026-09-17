"""Sign-in server aligned to the UCAS class calendar.

The one-pass routine in :mod:`server.autosign` performs a single run; the
:class:`Server` keeps calling it **at every class-period start** of the UCAS
academic calendar (hardcoded below as ``CLASS_PERIOD_STARTS``), which is what
the deployment wants: check in exactly when a class begins and only make noise
when there is something to say.

The loop is deliberately simple and stateless: compute the next period start
from the UCAS clock (``Asia/Shanghai``, hardcoded in :mod:`common.api` and also
the timezone the UCAS schedule uses), sleep until then, run, repeat. No cron,
no APScheduler dependency.

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

from .autosign import AutosignConfig, AutosignConfigError, run_once
from .logging import attach_notify_handler, configure_logging, get_logger
from .notify import Notifier, build_notifier

logger = get_logger(__name__)


#: Class-period start times of the UCAS academic calendar, in Beijing time
#: (``UCAS_TIMEZONE``). A sign-in pass runs at each of these moments: the
#: sign-in window opens 30 minutes before class begins
#: (``SIGN_WINDOW_BEFORE_MS`` in :mod:`common.api`), so the pass at the exact
#: period start always lands inside the window of a class beginning then.
CLASS_PERIOD_STARTS: tuple[tuple[int, int], ...] = (
    (8, 25),
    (9, 15),
    (10, 20),
    (11, 10),
    (13, 25),
    (14, 15),
    (15, 20),
    (16, 10),
    (17, 0),
    (18, 25),
    (19, 15),
    (20, 10),
    (21, 0),
)


def next_run_at(now: datetime) -> datetime:
    """Earliest class-period start strictly after ``now``.

    ``now`` must be timezone-aware in UCAS time. After the last period of the
day the answer is the first period of tomorrow.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (midnight, midnight + timedelta(days=1)):
        for hour, minute in CLASS_PERIOD_STARTS:
            candidate = day.replace(hour=hour, minute=minute)
            if candidate > now:
                return candidate
    raise AssertionError("unreachable: tomorrow's first period always follows now")


def seconds_until_next_run(now: datetime) -> float:
    """Seconds from ``now`` until the next class-period start.

    For example at 08:24:40 it returns 20 seconds; at 08:25:00 it skips to
    09:15:00 and returns 3300. The result is always strictly positive.
    """
    return max(1.0, (next_run_at(now) - now).total_seconds())


def install_signal_handlers() -> None:
    """Turn ``SIGTERM`` / ``SIGINT`` into the single shutdown path.

    A server shuts down on ``KeyboardInterrupt``; routing the signals there
    keeps ``Ctrl+C`` and ``docker stop`` (SIGTERM) identical.
    """

    def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
        logger.warning("received signal %s, shutting down gracefully", signum)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _raise_keyboard_interrupt)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass


class Server:
    """Run one sign-in pass per class period until shut down."""

    def __init__(self, config: AutosignConfig, client: UcasClient, notifier: Notifier) -> None:
        self.config = config
        self.client = client
        self.notifier = notifier

    def run_forever(self) -> None:
        """Run the class-period loop. Only returns on shutdown (``KeyboardInterrupt``)."""
        if self.config.run_on_start:
            logger.warning("run-on-start enabled: doing an initial pass")
            self.run_pass()
        else:
            logger.warning("run-on-start disabled: waiting for the next class period")

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
        run_once(self.client, self.config)

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
    notifier = build_notifier()

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

Run the automatic sign-in at every UCAS class-period start
(08:25 09:15 10:20 11:10 13:25 14:15 15:20 16:10 17:00 18:25 19:15 20:10 21:00,
Beijing time, hardcoded from the academic calendar) until stopped.
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
