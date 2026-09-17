"""Non-interactive, one-shot automatic sign-in.

This is the piece that makes the tool usable as a scheduled job: it logs in,
loads *today's* courses, signs in for every course whose sign-in window is
currently open, and reports the outcome.

Error contract (see :mod:`common.error`):

* :class:`~common.error.UcasOperationalError` -- bad credentials, network and
  upstream trouble -- never escapes :func:`run_once`. It is logged at
  ``CRITICAL`` (``logger.fatal``), which the notify handler turns into a push,
  and the server keeps running.
* :class:`UcasNotImplementedError` is deliberately **not** caught: it escapes
  ``run_once``, crashes the process and takes the fatal log (and thus one last
  notification) with it. An unimplemented path must be loud.
* Notifier failures are not caught either: if a push cannot be delivered the
  operator must notice, so the process is allowed to die.

Notifications are not sent from here directly: the module only logs, and
:mod:`server.logging` turns every ``WARNING`` or worse into a push.

The run is *idempotent per day*: the course list is re-fetched from UCAS on
every run and a course that is already marked as signed upstream is never
signed again or pushed twice. Failures are retried on the next run, and a
later class period usually still falls inside the window, leaving a chance
to recover.

This module is an internal building block: the ``server`` console script calls
:func:`run_once` at every class-period start (see :mod:`server.server`). A
standalone one-shot command is intentionally not exposed; the Docker image
runs the server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from common.api import (
    Course,
    SignResult,
    UcasClient,
    format_date_from_ms,
    sign_window_state,
)
from common.error import UcasOperationalError

from .logging import get_logger

logger = get_logger(__name__)


class AutosignConfigError(Exception):
    """Raised when the runner is not configured correctly."""


def _env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class AutosignConfig:
    username: str
    password: str
    run_on_start: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AutosignConfig":
        """Build the configuration from environment variables.

        Raises:
            AutosignConfigError: a required variable is missing.
        """
        env = env if env is not None else os.environ
        username = (env.get("UCAS_USERNAME") or "").strip()
        password = (env.get("UCAS_PASSWORD") or "").strip()

        if not username:
            raise AutosignConfigError("UCAS_USERNAME is required for automatic sign-in")
        if not password:
            raise AutosignConfigError("a password is required: set UCAS_PASSWORD")

        return cls(
            username=username,
            password=password,
            run_on_start=_env_bool(env, "UCAS_RUN_ON_START", True),
        )


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class SignDetail:
    """Outcome of one sign-in attempt: success, or the error that stopped it."""

    course: Course
    result: SignResult | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.success

    @property
    def label(self) -> str:
        name = self.course.course_name or self.course.id
        return f"{name} [{self.course.id}]" if self.course.id else name

    @property
    def reason(self) -> str:
        if self.error:
            return self.error
        if self.result is not None:
            return self.result.message
        return "unknown error"


@dataclass
class AutosignResult:
    date: str
    status: str  # no-courses | idle | signed | failed | error
    signed: list[SignDetail] = field(default_factory=list)
    failed: list[SignDetail] = field(default_factory=list)
    error: str = ""

    @property
    def to_sign(self) -> int:
        return len(self.signed) + len(self.failed)


# --------------------------------------------------------------------------- #
# One run
# --------------------------------------------------------------------------- #


def run_once(client: UcasClient, config: AutosignConfig) -> AutosignResult:
    """Fetch today's courses and sign in for everything actionable.

    Never raises :class:`~common.error.UcasOperationalError`: those are caught,
    logged at ``CRITICAL`` (and therefore pushed) and turned into an
    :class:`AutosignResult`. :class:`UcasNotImplementedError` and genuine bugs
    do escape, on purpose -- see the module docstring.
    """
    # 1. Which day are we signing for, and what courses are there? The
    #    calibrated UCAS server clock decides. All errors here are operational
    #    and abort the whole run; UcasNotImplementedError escapes and crashes.
    try:
        now_ms = client.server_now_ms()
        date = format_date_from_ms(now_ms)
        courses = client.query_courses(config.username, config.password, date)
    except UcasOperationalError as exc:
        logger.fatal(
            "could not load the schedule for today: %s",
            exc.describe(),
            extra={"title": "UCAS auto sign-in: fetch failed"},
        )
        return AutosignResult(date="", status="error", error=exc.message)

    if not courses:
        logger.info("%s: no courses -- staying silent", date)
        return AutosignResult(date=date, status="no-courses")

    # 2. Pick the courses that still need a sign-in. UCAS itself is the source
    #    of truth: a course already signed in (`signStatus == "1"`) is skipped,
    #    and a course is only signed inside its sign-in window.
    pending: list[Course] = []
    for course in courses:
        if course.signed:
            continue
        window = sign_window_state(course, date, now_ms)
        if window != "open":
            logger.info("%s: skip %s (window: %s)", date, course.course_name or course.id, window)
            continue
        pending.append(course)

    if not pending:
        logger.info("%s: %d course(s), nothing to sign in -- staying silent", date, len(courses))
        return AutosignResult(date=date, status="idle")

    # 3. Sign in. Each course is logged at INFO; the run as a whole is rolled up
    #    into one WARNING (success) / CRITICAL (failure) below, so the notify
    #    handler pushes at most one message per outcome instead of per course.
    result = AutosignResult(date=date, status="signed")
    for course in pending:
        outcome = _sign_one(client, config, course)
        if outcome.ok:
            result.signed.append(outcome)
            logger.info("%s: signed in for %s", date, course.course_name or course.id)
        else:
            result.failed.append(outcome)
            logger.info(
                "%s: sign-in failed for %s: %s",
                date,
                course.course_name or course.id,
                outcome.reason,
            )

    if result.signed:
        title, body = _success_message(date, result.signed)
        logger.warning(body, extra={"title": title})

    if result.failed:
        result.status = "failed"
        title, body = _failure_message(date, result.failed)
        logger.fatal(body, extra={"title": title})

    return result


def _sign_one(client: UcasClient, config: AutosignConfig, course: Course) -> SignDetail:
    """Sign in for one course, turning every operational failure into a reason.

    :class:`UcasNotImplementedError` deliberately escapes -- see the module
    docstring.
    """
    identifier = course.id or course.uuid
    try:
        sign_result = client.sign(config.username, config.password, identifier)
    except UcasOperationalError as exc:
        return SignDetail(course=course, error=exc.describe())
    return SignDetail(course=course, result=sign_result)


# --------------------------------------------------------------------------- #
# Message formatting
# --------------------------------------------------------------------------- #


def _format_course_line(detail: SignDetail) -> str:
    course = detail.course
    window = ""
    if course.class_begin_time and course.class_end_time:
        window = f" {_clock(course.class_begin_time)}~{_clock(course.class_end_time)}"
    return f"- {detail.label}{window}: {detail.reason}"


def _clock(value: str) -> str:
    tail = value.strip().split()[-1] if value.strip() else ""
    return tail[:5] if tail else "--"


def _success_message(date: str, signed: list[SignDetail]) -> tuple[str, str]:
    lines = [f"Date: {_pretty_date(date)}", "", *(_format_course_line(item) for item in signed)]
    return f"UCAS auto sign-in succeeded ({len(signed)})", "\n".join(lines)


def _failure_message(date: str, failed: list[SignDetail]) -> tuple[str, str]:
    lines = [
        f"Date: {_pretty_date(date)}",
        "",
        "The following courses could not be signed in -- please handle them manually:",
        "",
        *(_format_course_line(item) for item in failed),
    ]
    return f"UCAS auto sign-in failed ({len(failed)})", "\n".join(lines)


def _pretty_date(compact: str) -> str:
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return compact
