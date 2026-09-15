"""Hourly scheduler for automatic sign-in.

The one-pass routine in :mod:`daemon.autosign` performs a single run; this
module keeps calling it **at the top of every hour**, which is what the
deployment wants: run once per hour and only make noise when there is something
to say.

The loop is deliberately simple and stateless: compute the next hour boundary
from the UCAS clock (``Asia/Shanghai``, hardcoded in :mod:`common.api` and also
the timezone the UCAS schedule uses), sleep until then, run, repeat. No cron, no
APScheduler dependency.

On ``SIGTERM`` / ``SIGINT`` the daemon finishes sleeping early and exits, so
``docker stop`` is immediate.
"""

from __future__ import annotations

import signal
import sys
import threading
import traceback
from datetime import datetime, timedelta

from common.api import UCAS_TIMEZONE, UcasClient

from .autosign import AutosignConfig, AutosignConfigError, run_once
from .notify import Message, Notifier, NotifyConfigError, build_notifier


def seconds_until_next_run(now: datetime) -> float:
    """Seconds from ``now`` until the next hour boundary.

    For example at 13:59:40 it returns 20 seconds; at 14:00:00 it returns 3600.
    The result is always strictly positive.
    """
    boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1.0, (boundary - now).total_seconds())


class _Stopper:
    """Cooperative shutdown driven by signals."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def _handle(self, signum: int, frame: object) -> None:  # pragma: no cover - signal
        print(f"\n[autosign] received signal {signum}, shutting down after this wait")
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleep, returning ``True`` if a stop signal arrived."""
        return self._event.wait(seconds)


def _safe_run(config: AutosignConfig, client: UcasClient, notifier: Notifier) -> None:
    """Run one pass, keeping the daemon alive across unexpected errors.

    ``run_once`` already handles expected upstream failures; this is the last
    line of defence so a bug or a surprise response cannot kill an unattended
    server process. The traceback is logged (and pushed) instead.
    """
    try:
        run_once(client, config, notifier)
    except Exception as exc:  # noqa: BLE001 - a daemon must not die here
        traceback.print_exc()
        try:
            notifier.send(
                Message(
                    title="UCAS auto sign-in: runtime error",
                    body=f"{type(exc).__name__}: {exc}",
                )
            )
        except Exception:  # noqa: BLE001 - notification is best-effort
            pass


def run_daemon(
    config: AutosignConfig,
    client: UcasClient,
    notifier: Notifier,
    stopper: _Stopper | None = None,
) -> None:
    """Run until a stop signal arrives."""
    stopper = stopper or _Stopper()
    stopper.install()

    if config.run_on_start:
        print("[autosign] run-on-start enabled: doing an initial pass")
        _safe_run(config, client, notifier)
    else:
        print("[autosign] run-on-start disabled: waiting for the next hour boundary")

    while not stopper.stopped:
        now = datetime.now(UCAS_TIMEZONE)
        delay = seconds_until_next_run(now)
        target = now + timedelta(seconds=delay)
        print(f"[autosign] sleeping {delay:.0f}s until {target.isoformat(timespec='seconds')}")
        if stopper.wait(delay):
            break
        if stopper.stopped:
            break
        _safe_run(config, client, notifier)


def main(argv: list[str] | None = None, *, client: UcasClient | None = None, notifier: Notifier | None = None) -> int:
    """Entry point for the ``daemon`` console script."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in args):
        print(_USAGE, end="")
        return 0

    try:
        config = AutosignConfig.from_env()
    except AutosignConfigError as exc:
        print(f"daemon: {exc}", file=sys.stderr)
        return 2

    active_client = client or UcasClient()
    active_notifier = notifier
    if active_notifier is None:
        try:
            active_notifier = build_notifier()
        except NotifyConfigError as exc:
            print(f"daemon: {exc}", file=sys.stderr)
            active_client.close()
            return 2

    print(
        "[autosign] daemon started "
        f"(timezone={datetime.now(UCAS_TIMEZONE).tzname()})"
    )
    try:
        run_daemon(config, active_client, active_notifier)
    except KeyboardInterrupt:  # pragma: no cover - handled via signal
        pass
    finally:
        active_client.close()
        active_notifier.close()
    print("[autosign] stopped")
    return 0


_USAGE = """\
usage: daemon

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
Notifications are pushed to Feishu; see README.md for details.
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
