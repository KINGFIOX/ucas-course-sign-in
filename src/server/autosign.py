"""Non-interactive, one-shot automatic sign-in.

This is the piece that makes the tool usable as a scheduled job: it logs in,
loads *today's* courses, signs in for every course whose sign-in window is
currently open, and pushes the outcome to a phone.

Design goals, in the order the request stated them:

* **There is a class and it is in the sign-in window** -- sign in; a failure is
  logged at ``WARNING`` and therefore pushed by the notify handler.
* **There is no class, or nothing is actionable** -- log at ``INFO``, stay quiet.
* **Fetching the course list fails** -- log at ``ERROR`` and push the error.

Notifications are not sent from here directly: the module only logs, and
:mod:`server.logging` turns every ``WARNING`` or worse into a push.

The run is *idempotent per day*: the course list is re-fetched from UCAS on
every run and a course that is already marked as signed upstream is never
signed again or pushed twice. Failures are retried on the next run, and inside
the sign-in window there are usually one or two hourly runs left to recover.

This module is an internal building block: the ``server`` console script calls
:func:`run_once` every hour (see :mod:`server.server`). A standalone one-shot
command is intentionally not exposed; the Docker image runs the server.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Mapping

from common.api import (
    Course,
    SignResult,
    UcasClient,
    format_date_from_ms,
    sign_window_state,
)
from common.error import (
    UcasAuthError,
    UcasError,
    UcasJsonError,
    UcasNetworkError,
    UcasServerError,
    UcasUnrecognizableCourse,
)

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

    Never raises for expected upstream problems -- those are logged (and, at
    ``WARNING``/``ERROR``, pushed by the notify handler) and turned into an
    :class:`AutosignResult`.
    """
    # 1. Which day are we signing for? The calibrated UCAS server clock decides.
    try:
        date = format_date_from_ms(client.server_now_ms())
    except UcasError as exc:
        return _fail_fetch("", exc, logging.FATAL)

    # 2. Load the schedule. The endpoint now tells "no courses today" (STATUS 2,
    #    returned as an empty list) apart from a real error (raised).
    try:
        courses = client.query_courses(config.username, config.password, date)
    except UcasError as exc:
        return _fail_fetch(date, exc, logging.WARNING)

    if not courses:
        logger.info("%s: no courses -- staying silent", date)
        return AutosignResult(date=date, status="no-courses")

    # 3. Pick the courses that still need a sign-in. UCAS itself is the source
    #    of truth: a course already signed in (`signStatus == "1"`) is skipped,
    #    and a course is only signed inside its sign-in window.
    now_ms = client.server_now_ms()
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

    # 4. Sign in. Each course is logged at INFO; the run as a whole is rolled up
    #    into one WARNING (success) / ERROR (failure) below, so the notify
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
        logger.error(body, extra={"title": title})

    return result


def _sign_one(client: UcasClient, config: AutosignConfig, course: Course) -> SignDetail:
    """Sign in for one course, turning every failure into a precise reason."""
    try:
        sign_result = client.sign(config.username, config.password, course.id)
    except UcasAuthError as exc:
        return SignDetail(course=course, error=f"credentials rejected: {exc.message}")
    except UcasUnrecognizableCourse as exc:
        return SignDetail(course=course, error=f"unrecognized course identifier: {exc.message}")
    except UcasNetworkError as exc:
        return SignDetail(course=course, error=f"network error: {exc.message}")
    except UcasServerError as exc:
        return SignDetail(course=course, error=f"UCAS server error: {exc.message}")
    except UcasJsonError as exc:
        return SignDetail(course=course, error=f"unexpected UCAS reply: {exc.message}")
    except UcasError as exc:
        return SignDetail(course=course, error=exc.message)
    return SignDetail(course=course, result=sign_result)


def _fail_fetch(date: str, exc: UcasError, level: int = logging.WARNING) -> AutosignResult:
    """Report a failed course fetch, naming the failure precisely.

    A network or upstream problem is transient and the next hourly run retries
    it; rejected credentials will keep failing until ``.env`` is fixed, so the
    notification says so instead of looking like a random glitch. A failure to
    even determine today's date is logged at ``CRITICAL``.
    """
    label = date or format_date_from_ms(int(time.time() * 1000))
    title, kind = _fetch_error(exc)
    logger.log(
        level,
        "%s: %s\nDate: %s\nError: %s\nCode: %s\nStage: %s",
        kind,
        _describe(exc),
        _pretty_date(label),
        exc.message,
        exc.code,
        exc.stage,
        extra={"title": f"UCAS auto sign-in: {title}"},
    )
    return AutosignResult(date=date, status="error", error=exc.message)


def _fetch_error(exc: UcasError) -> tuple[str, str]:
    """Return ``(notification title, log label)`` for a failed course fetch."""
    if isinstance(exc, UcasAuthError):
        return "credentials rejected", "authentication failed"
    if isinstance(exc, UcasNetworkError):
        return "network error (will retry)", "network error"
    if isinstance(exc, UcasServerError):
        return "UCAS server error (will retry)", "upstream error"
    if isinstance(exc, UcasJsonError):
        return "unexpected UCAS response", "bad upstream response"
    return "failed to fetch courses", "could not fetch courses"


def _describe(exc: UcasError) -> str:
    """Error text with its machine-readable code, so logs stay greppable."""
    return f"{exc.message} ({exc.code})" if exc.code else exc.message


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
