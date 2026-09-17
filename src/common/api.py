"""Client for the UCAS iClass upstream API.

Ports the three route handlers of the original Next.js project:

* ``/api/course-uuid/query``     -> :meth:`UcasClient.query_courses`
* ``/api/course-uuid/sign``      -> :meth:`UcasClient.sign`
* ``/api/course-uuid/timestamp`` -> :meth:`UcasClient.server_now_ms`

The difference is that every request is issued directly from this machine
instead of going through Vercel, so authentication, same-origin checks and
rate limiting are unnecessary. Only input validation, timeout handling and
the original error-code semantics are kept.

Error contract: see :mod:`common.error`. In short, operational failures raise
:class:`~common.error.UcasOperationalError` subclasses, while response shapes
nobody has written handling for raise :class:`UcasNotImplementedError`, which
callers deliberately let crash.
"""

from __future__ import annotations

import random
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .error import (
    UcasAuthError,
    UcasJsonError,
    UcasNetworkError,
    UcasNotImplementedError,
    UcasServerError,
    UcasTimeError,
    UcasUnrecognizableCourse,
)

# --------------------------------------------------------------------------- #
# Constants (kept in sync with the upstream Android client)
# --------------------------------------------------------------------------- #

# UCAS runs on Beijing time and its course dates / sign-in windows are always in
# China Standard Time. The timezone is hardcoded here instead of following the
# process ``TZ`` so the behaviour never depends on how the container is started.
# Asia/Shanghai has no DST, i.e. a fixed UTC+8 clock.
UCAS_TIMEZONE = ZoneInfo("Asia/Shanghai")

LOGIN_URL = "https://iclass.ucas.edu.cn:8181/app/user/login.action"
SCHEDULE_URL = "https://iclass.ucas.edu.cn:8181/app/course/get_stu_course_sched.action"
SIGN_URL = "https://iclass.ucas.edu.cn:8181/app/course/stu_scan_sign.action"
TIMESTAMP_URL = "https://iclass.ucas.edu.cn:8181/app/common/get_timestamp.do"

LOGIN_UA = "student_5.0.1.2_android_12_20__110000"
API_UA = "student_5.0.1.2_android_12_20_100000000000000_110000"

VERIFICATION_URL_TEMPLATE = (
    "http://iclass.ucas.edu.cn:88/ve/webservices/mobileCheck.shtml"
    "?method=mobileLogin&username=${0}&password=${1}&lx=${2}"
)

REQUEST_TIMEOUT = 10.0
TIMESTAMP_TIMEOUT = 6.0

#: UCAS runs ``get_timestamp.do`` and ``stu_scan_sign.action`` on different
#: servers whose clocks drift by roughly 3 seconds. The sign-in timestamp has
#: to be shifted back a little for the sign-in endpoint to accept it.
SIGN_TIMESTAMP_BUFFER_MS = 3 * 1000

MAX_USERNAME_LENGTH = 40
MAX_PASSWORD_LENGTH = 80

#: Sign-in window opens this long before the class starts.
SIGN_WINDOW_BEFORE_MS = 30 * 60 * 1000


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #


@dataclass
class Course:
    id: str = ""
    uuid: str = ""
    course_name: str = ""
    teacher_name: str = ""
    week_day: str = ""
    class_begin_time: str = ""
    class_end_time: str = ""
    sign_status: str = ""

    @property
    def signed(self) -> bool:
        return self.sign_status == "1"

    @property
    def status_text(self) -> str:
        return "Signed" if self.signed else "Not signed"

    @classmethod
    def from_upstream(cls, item: dict[str, Any]) -> "Course":
        return cls(
            id=str(item.get("id") or ""),
            uuid=str(item.get("uuid") or ""),
            course_name=str(item.get("courseName") or ""),
            teacher_name=str(item.get("teacherName") or ""),
            week_day=str(item.get("weekDay") or ""),
            class_begin_time=str(item.get("classBeginTime") or ""),
            class_end_time=str(item.get("classEndTime") or ""),
            sign_status=str(item.get("signStatus") or ""),
        )


@dataclass
class SignResult:
    success: bool
    message: str
    upstream_status: str = ""
    stu_sign_id: str = ""
    stu_sign_status: str = ""


@dataclass
class LoginResult:
    session_id: str
    user_id: str


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def build_login_body(username: str, password: str) -> str:
    """Build the ``x-www-form-urlencoded`` body for the login endpoint."""
    fields = {
        "phone": username,
        "password": password,
        "verificationType": "1",
        "verificationUrl": VERIFICATION_URL_TEMPLATE,
        "userLevel": "1",
    }
    return urllib.parse.urlencode(fields)


def normalize_date(value: str) -> str:
    """Normalize ``yyyy-MM-dd`` / ``yyyyMMdd`` to ``yyyyMMdd``.

    Raises:
        UcasTimeError: on invalid input (``BAD_DATE``).
    """
    compact = value.replace("-", "").replace("/", "").strip()
    if len(compact) != 8 or not compact.isdigit():
        raise UcasTimeError("Invalid date format; use yyyyMMdd or yyyy-MM-dd", "BAD_DATE")
    return compact


def validate_credentials(username: str, password: str) -> None:
    """Validate credentials with the same constraints as the web frontend.

    Raises:
        UcasAuthError: when the pair is missing or malformed
            (``BAD_CREDENTIALS``).
    """
    if not username or not password:
        raise UcasAuthError("Mail and password are required", "BAD_CREDENTIALS")
    if len(username) > MAX_USERNAME_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise UcasAuthError("Invalid mail or password format", "BAD_CREDENTIALS")
    if any(ch.isspace() for ch in username):
        raise UcasAuthError("Mail must not contain whitespace", "BAD_CREDENTIALS")


def normalize_course_sched_id(raw: str) -> str | None:
    """Return the course schedule ID, or ``None`` if ``raw`` is not one.

    A course schedule ID is exactly 7 digits.
    """
    compact = raw.strip()
    if len(compact) == 7 and compact.isdigit():
        return compact
    return None


def normalize_uuid(raw: str) -> str | None:
    """Return the timetable UUID, or ``None`` if ``raw`` is not one.

    A UUID is a 32-character hex string; hyphens are allowed and case is
    normalized to upper case.
    """
    compact = raw.strip().replace("-", "")
    if len(compact) != 32:
        return None
    try:
        int(compact, 16)
    except ValueError:
        return None
    return compact.upper()


def build_scan_url(course_sched_id: str, timestamp: int) -> str:
    """Build the URL encoded into the sign-in QR code.

    Deliberately carries **no** ``id`` parameter: the phone that scans the code
    is already logged into the UCAS app, so the server learns who is signing in
    from the app's own session. This mirrors the QR code the web version shows.
    """
    params: dict[str, Any] = {"courseSchedId": course_sched_id, "timestamp": timestamp}
    return f"{SIGN_URL}?{urllib.parse.urlencode(params)}"


def build_sign_url(course_sched_id: str, timestamp: int, user_id: str) -> str:
    """Build the URL used to sign in directly (no QR, no browser session).

    Unlike :func:`build_scan_url` there is no app session to identify the
    student, so ``user_id`` is required and sent as the ``id`` parameter.
    """
    params: dict[str, Any] = {
        "courseSchedId": course_sched_id,
        "timestamp": timestamp,
        "id": user_id,
    }
    return f"{SIGN_URL}?{urllib.parse.urlencode(params)}"


def build_timetable_sign_url(uuid: str, timestamp: int, user_id: str) -> str:
    """Like :func:`build_sign_url`, but identifies the course by its timetable
    UUID (``timeTableId``) instead of its course schedule ID.

    Only a direct sign-in variant exists: the QR code is always built from the
    course ID (see :func:`build_scan_url`).
    """
    params: dict[str, Any] = {
        "timeTableId": uuid,
        "timestamp": timestamp,
        "id": user_id,
    }
    return f"{SIGN_URL}?{urllib.parse.urlencode(params)}"


def extract_clock_time(value: str) -> str:
    """Extract ``10:25:00`` from ``2026-03-25 10:25:00``.

    Raises:
        UcasNotImplementedError: the value has a shape we cannot parse (empty,
            non-numeric or out-of-range) -- deliberately a crash, since it
            means the upstream changed its time format.
    """
    tokens = value.strip().split()
    fields = (tokens[-1] if tokens else "").split(":")
    try:
        hour = int(fields[0])
        minute = int(fields[1])
        second = int(fields[2]) if len(fields) > 2 else 0
    except (IndexError, ValueError):
        raise UcasNotImplementedError(f"cannot parse clock time from {value!r}") from None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise UcasNotImplementedError(f"clock time out of range: {value!r}")
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_class_datetime(date: str, value: str) -> datetime:
    """Combine a course date and a timestamp string into a :class:`datetime`.

    The result is timezone-aware (``Asia/Shanghai``), so converting it to a
    timestamp does not depend on the process timezone.
    """
    clock = extract_clock_time(value)
    compact = normalize_date(date)
    return datetime.strptime(f"{compact} {clock}", "%Y%m%d %H:%M:%S").replace(tzinfo=UCAS_TIMEZONE)


def format_time_range(begin: str, end: str) -> str:
    """Human-readable time range, keeping only hours and minutes."""
    return f"{extract_clock_time(begin)[:5]} ~ {extract_clock_time(end)[:5]}"


def format_date_from_ms(timestamp_ms: int) -> str:
    """Format a millisecond timestamp as ``yyyyMMdd`` in UCAS time (UTC+8)."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UCAS_TIMEZONE).strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class UcasClient:
    """Thin wrapper around the upstream iClass API."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=REQUEST_TIMEOUT),
            follow_redirects=False,
        )

    # -- Lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "UcasClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Internal helpers ------------------------------------------------- #

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP request.

        Raises:
            UcasNetworkError: the request timed out or the connection failed.
        """
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise UcasNetworkError(
                "Request timed out; check your network", "NETWORK_TIMEOUT"
            ) from exc
        except httpx.HTTPError as exc:
            raise UcasNetworkError(f"Network error: {exc}", "NETWORK_ERROR") from exc

    @staticmethod
    def _json(response: httpx.Response, code: str, stage: str) -> dict[str, Any]:
        """Decode the response body as a JSON object.

        Raises:
            UcasJsonError: the body is not JSON or not a JSON object.
        """
        try:
            data = response.json()
        except ValueError as exc:
            raise UcasJsonError("Upstream returned non-JSON data", code, stage) from exc
        if not isinstance(data, dict):
            raise UcasJsonError("Upstream returned an unexpected format", code, stage)
        return data

    # -- Login ------------------------------------------------------------ #

    def login(self, username: str, password: str) -> LoginResult:
        """Log in and return ``sessionId`` and the user ``id``.

        Raises:
            UcasAuthError: the pair is malformed or rejected (``BAD_CREDENTIALS``
                / ``AUTH_FAILED``).
            UcasNetworkError: the request failed.
            UcasServerError: the login endpoint answered with an HTTP error.
            UcasJsonError: the answer was not the expected JSON object.
        """
        validate_credentials(username, password)
        response = self._request(
            "POST",
            LOGIN_URL,
            content=build_login_body(username, password),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": LOGIN_UA,
            },
        )
        if response.status_code != 200:
            raise UcasServerError(
                f"Login endpoint HTTP error: {response.status_code}",
                "UPSTREAM_LOGIN_HTTP",
                "login",
            )

        data = self._json(response, "UPSTREAM_LOGIN_BAD_JSON", "login")
        result = data.get("result") or {}
        session_id = str(result.get("sessionId") or "")
        user_id = str(result.get("id") or "")

        if data.get("STATUS") != "0" or not session_id or not user_id:
            raise UcasAuthError(
                "Login failed; check your mail and password", "AUTH_FAILED", "login"
            )

        return LoginResult(session_id=session_id, user_id=user_id)

    # -- Schedule --------------------------------------------------------- #

    def query_courses(self, username: str, password: str, date: str) -> list[Course]:
        """Log in and fetch the course list for the given date.

        Raises:
            UcasAuthError: credentials rejected.
            UcasNetworkError: a request failed.
            UcasServerError: the schedule endpoint answered with an HTTP error
                or an error ``STATUS``.
            UcasJsonError: the answer was not the expected JSON object.
            UcasNotImplementedError: the answer carries an unknown ``STATUS`` or
                a ``result`` that is not a list -- crashes on purpose.
        """
        normalized_date = normalize_date(date)
        login = self.login(username, password)

        url = f"{SCHEDULE_URL}?" + urllib.parse.urlencode(
            {"id": login.user_id, "dateStr": normalized_date}
        )
        response = self._request(
            "GET",
            url,
            headers={"sessionId": login.session_id, "User-Agent": API_UA},
        )
        if response.status_code != 200:
            raise UcasServerError(
                f"Schedule endpoint HTTP error: {response.status_code}",
                "UPSTREAM_SCHEDULE_HTTP",
                "schedule",
            )

        data = self._json(response, "UPSTREAM_SCHEDULE_BAD_JSON", "schedule")
        # STATUS: "0" = ok, "1" = real error, "2" = no courses that day.
        status = str(data.get("STATUS") or "")
        if status == "0":
            result = data.get("result")
        elif status == "2":
            return []
        elif status == "1":
            raise UcasServerError(
                str(data.get("ERRMSG") or data.get("msg") or "Schedule query failed"),
                "UPSTREAM_SCHEDULE_STATUS",
                "schedule",
            )
        else:
            raise UcasNotImplementedError(f"unknown schedule STATUS: {status!r}")

        if not isinstance(result, list):
            raise UcasNotImplementedError(f"schedule result is not a list: {type(result).__name__}")

        return [Course.from_upstream(item) for item in result if isinstance(item, dict)]

    # -- Sign-in ---------------------------------------------------------- #

    def sign(self, username: str, password: str, identifier: str) -> SignResult:
        """Log in and sign in for a course.

        ``identifier`` is either a 7-digit course schedule ID or a 32-character
        hex timetable UUID; the sign-in timestamp is the calibrated server
        clock. Returns a :class:`SignResult` on success.

        Raises:
            UcasUnrecognizableCourse: ``identifier`` is neither a course ID nor
                a UUID (``BAD_IDENTIFIER``).
            UcasAuthError: credentials rejected.
            UcasNetworkError: a request failed.
            UcasServerError: the sign-in endpoint answered with an HTTP error.
            UcasJsonError: the answer was not the expected JSON object.
            UcasNotImplementedError: the answer shape has no handling yet --
                crashes on purpose.
        """
        course_id = normalize_course_sched_id(identifier)
        uuid = normalize_uuid(identifier)
        if course_id is None and uuid is None:
            raise UcasUnrecognizableCourse("Invalid course ID or UUID format", "BAD_IDENTIFIER")

        login = self.login(username, password)
        timestamp = self.sign_timestamp()

        if course_id is not None:
            url = build_sign_url(course_id, timestamp, login.user_id)
        else:
            assert uuid is not None  # guarded above: one of the two matched
            url = build_timetable_sign_url(uuid, timestamp, login.user_id)

        response = self._request(
            "GET",
            url,
            headers={"sessionId": login.session_id, "User-Agent": API_UA},
        )
        if response.status_code != 200:
            raise UcasServerError(
                f"Sign-in endpoint HTTP error: {response.status_code}",
                "UPSTREAM_SIGN_HTTP",
                "sign",
            )

        data = self._json(response, "UPSTREAM_SIGN_BAD_JSON", "sign")
        return parse_sign_response(data)

    # -- Clock calibration ------------------------------------------------ #

    def server_now_ms(self) -> int:
        """Current server time in milliseconds, from the timestamp endpoint.

        The reply's timestamp is taken mid-round-trip, so half of the measured
        latency is added back to estimate the server clock at receipt.

        Raises:
            UcasNetworkError: the timestamp endpoint could not be reached.
            UcasJsonError: the reply body was not the expected JSON object.
            UcasNotImplementedError: the reply was missing a usable ``timestamp``
                or carried a non-``0`` ``STATUS`` -- crashes on purpose.
        """
        start_ms = time.time() * 1000
        response = self._request(
            "POST",
            f"{TIMESTAMP_URL}?id={random.randint(0, 999_999)}",
            headers={"User-Agent": API_UA, "Connection": "Keep-Alive"},
            timeout=TIMESTAMP_TIMEOUT,
        )
        data = self._json(response, "UPSTREAM_TIMESTAMP_BAD_JSON", "timestamp")
        timestamp = data.get("timestamp")
        if data.get("STATUS") == "0" and isinstance(timestamp, (int, float)):
            latency_ms = max(0.0, time.time() * 1000 - start_ms)
            # The timestamp is taken mid-round-trip, so add half of it back.
            return int(float(timestamp) + latency_ms / 2)

        raise UcasNotImplementedError(
            f"timestamp endpoint replied STATUS={data.get('STATUS')!r}, timestamp={timestamp!r}"
        )

    def sign_timestamp(self) -> int:
        """Timestamp accepted by the sign-in endpoint (clock buffer applied)."""
        return self.server_now_ms() - SIGN_TIMESTAMP_BUFFER_MS


def parse_sign_response(data: dict[str, Any]) -> SignResult:
    """Parse the upstream sign-in response, supporting both ``STATUS`` and ``ERRCODE`` styles.

    Returns a :class:`SignResult` on success. The failure shapes have not been
    pinned down yet, so anything else raises :class:`UcasNotImplementedError`
    -- a deliberate crash, not a finished error path.
    """
    result = data.get("result") or {}
    if not isinstance(result, dict):
        result = {}

    upstream_status = str(data.get("STATUS") or data.get("ERRCODE") or "")
    stu_sign_id = str(result.get("stuSignId") or "")
    stu_sign_status = str(result.get("stuSignStatus") or "")

    if upstream_status == "0" and stu_sign_status == "1":
        return SignResult(True, "Sign-in successful", upstream_status, stu_sign_id, stu_sign_status)

    raise UcasNotImplementedError(f"unhandled sign-in response: {str(data)[:200]}")


# --------------------------------------------------------------------------- #
# Sign-in window
# --------------------------------------------------------------------------- #


@dataclass
class SignWindow:
    open_at: int
    close_at: int

    def contains(self, timestamp_ms: int) -> bool:
        return self.open_at <= timestamp_ms <= self.close_at


def compute_sign_window(course: Course, date: str) -> SignWindow:
    """Compute the sign-in window: 30 minutes before class until class ends."""
    begin = parse_class_datetime(date, course.class_begin_time)
    end = parse_class_datetime(date, course.class_end_time)
    open_at = int((begin - timedelta(milliseconds=SIGN_WINDOW_BEFORE_MS)).timestamp() * 1000)
    close_at = int(end.timestamp() * 1000)
    return SignWindow(open_at=open_at, close_at=close_at)


def sign_window_state(course: Course, date: str, now_ms: int) -> str:
    """Return ``open`` / ``before`` / ``after`` for the course's window."""
    window = compute_sign_window(course, date)
    if now_ms < window.open_at:
        return "before"
    if now_ms > window.close_at:
        return "after"
    return "open"
