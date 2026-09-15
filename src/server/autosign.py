"""Non-interactive, one-shot automatic sign-in.

This is the piece that makes the tool usable as a scheduled job: it logs in,
loads *today's* courses, signs in for every course whose sign-in window is
currently open, and pushes the outcome to a phone.

Design goals, in the order the request stated them:

* **There is a class and it is in the sign-in window** -- sign in and push the
  result (success *and* failure are both pushed).
* **There is no class, or nothing is actionable** -- stay quiet.
* **Fetching the course list fails** -- push the error.

The run is *idempotent per day*: the course list is re-fetched from UCAS on
every run and a course that is already marked as signed upstream is never
signed again or pushed twice. Failures are retried on the next run, and inside
the sign-in window there are usually one or two hourly runs left to recover.

This module is an internal building block: the ``server`` console script calls
:func:`run_once` every hour (see :mod:`server.server`). A standalone one-shot
command is intentionally not exposed; the Docker image runs the server.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Mapping

from common.api import (
    Course,
    SignResult,
    UcasClient,
    UcasError,
    format_date_from_ms,
    sign_window_state,
)

from .logging_setup import get_logger
from .notify import Message, Notifier, NotifyConfigError

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


def run_once(
    client: UcasClient,
    config: AutosignConfig,
    notifier: Notifier | None = None,
    log: Callable[[str], None] | None = None,
) -> AutosignResult:
    """Fetch today's courses and sign in for everything actionable.

    Never raises for expected upstream problems -- those become an
    :class:`AutosignResult` and (where required) a push. ``log`` overrides the
    default logger (used by tests).
    """
    emit = log if log is not None else logger.info

    # 1. Which day are we signing for? The calibrated UCAS server clock decides.
    try:
        date = format_date_from_ms(client.server_now_ms())
    except UcasError as exc:
        return _fail_fetch(notifier, "", exc, emit)

    # 2. Load the schedule. A non-zero STATUS is ambiguous (real error or "no
    #    courses today"), so it is always surfaced as an error and pushed.
    try:
        courses = client.query_courses(config.username, config.password, date)
    except UcasError as exc:
        return _fail_fetch(notifier, date, exc, emit)

    if not courses:
        emit(f"{date}: no courses -- staying silent")
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
            emit(f"{date}: skip {course.course_name or course.id} (window: {window})")
            continue
        pending.append(course)

    if not pending:
        emit(f"{date}: {len(courses)} course(s), nothing to sign in -- staying silent")
        return AutosignResult(date=date, status="idle")

    # 4. Sign in.
    result = AutosignResult(date=date, status="signed")
    for course in pending:
        outcome = _sign_one(client, config, course)
        if outcome.ok:
            result.signed.append(outcome)
            emit(f"{date}: signed in for {course.course_name or course.id}")
        else:
            result.failed.append(outcome)
            emit(f"{date}: sign-in failed for {course.course_name or course.id}: {outcome.reason}")

    if result.failed:
        result.status = "failed"

    # 5. Push the outcome(s).
    if result.signed:
        _push(notifier, _success_message(date, result.signed), emit)
    if result.failed:
        _push(notifier, _failure_message(date, result.failed), emit)

    return result


def _sign_one(client: UcasClient, config: AutosignConfig, course: Course) -> SignDetail:
    try:
        sign_result = client.sign(config.username, config.password, course.id)
    except UcasError as exc:
        return SignDetail(course=course, error=exc.message)
    return SignDetail(course=course, result=sign_result)


def _fail_fetch(
    notifier: Notifier | None,
    date: str,
    exc: UcasError,
    emit: Callable[[str], None],
) -> AutosignResult:
    label = date or "today"
    emit(f"{label}: could not fetch courses: {exc.message}")
    _push(
        notifier,
        Message(
            title="UCAS auto sign-in: failed to fetch courses",
            body=f"Date: {_pretty_date(label)}\nError: {exc.message}\nCode: {exc.code}",
        ),
        emit,
    )
    return AutosignResult(date=date, status="error", error=exc.message)


def _push(notifier: Notifier | None, message: Message, emit: Callable[[str], None]) -> None:
    if notifier is None:
        return
    try:
        notifier.send(message)
    except NotifyConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - a push must never break the run
        emit(f"notification failed: {exc}")


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


def _success_message(date: str, signed: list[SignDetail]) -> Message:
    lines = [f"Date: {_pretty_date(date)}", "", *(_format_course_line(item) for item in signed)]
    return Message(title=f"UCAS auto sign-in succeeded ({len(signed)})", body="\n".join(lines))


def _failure_message(date: str, failed: list[SignDetail]) -> Message:
    lines = [
        f"Date: {_pretty_date(date)}",
        "",
        "The following courses could not be signed in -- please handle them manually:",
        "",
        *(_format_course_line(item) for item in failed),
    ]
    return Message(title=f"UCAS auto sign-in failed ({len(failed)})", body="\n".join(lines))


def _pretty_date(compact: str) -> str:
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return compact or "today"
