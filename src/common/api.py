"""Client for the UCAS iClass upstream API.

Ports the three Route Handlers of the original Next.js project:

* ``/api/course-uuid/query``     -> :meth:`UcasClient.query_courses`
* ``/api/course-uuid/sign``      -> :meth:`UcasClient.sign`
* ``/api/course-uuid/timestamp`` -> :meth:`UcasClient.get_server_offset_ms`

The difference is that every request is issued directly from this machine
instead of going through Vercel, so authentication, same-origin checks and
rate limiting are unnecessary. Only input validation, timeout handling and
the original error-code semantics are kept.
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

#: How long a cached server-time offset stays valid.
TIME_OFFSET_TTL_MS = 30 * 1000

MAX_USERNAME_LENGTH = 40
MAX_PASSWORD_LENGTH = 80

#: Sign-in window opens this long before the class starts.
SIGN_WINDOW_BEFORE_MS = 30 * 60 * 1000


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class UcasError(Exception):
    """Unified exception carrying an error code and the stage that failed."""

    def __init__(self, message: str, code: str = "UNEXPECTED_ERROR", stage: str = "request") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage

    def __str__(self) -> str:  # pragma: no cover - convenience for logging
        return self.message


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

    Raises :class:`UcasError` on invalid input.
    """
    compact = value.replace("-", "").replace("/", "").strip()
    if len(compact) != 8 or not compact.isdigit():
        raise UcasError("Invalid date format; use yyyyMMdd or yyyy-MM-dd", "BAD_DATE", "request")
    return compact


def validate_credentials(username: str, password: str) -> None:
    """Validate credentials with the same constraints as the web frontend."""
    if not username or not password:
        raise UcasError("Mail and password are required", "BAD_CREDENTIALS", "request")
    if len(username) > MAX_USERNAME_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise UcasError("Invalid mail or password format", "BAD_CREDENTIALS", "request")
    if any(ch.isspace() for ch in username):
        raise UcasError("Mail must not contain whitespace", "BAD_CREDENTIALS", "request")


def normalize_course_sched_id(raw: str) -> str | None:
    """A course ID must be exactly 7 digits."""
    compact = raw.strip()
    return compact if len(compact) == 7 and compact.isdigit() else None


def normalize_uuid(raw: str) -> str | None:
    """A UUID is a 32-character hex string; hyphens are allowed."""
    compact = raw.strip().replace("-", "")
    if len(compact) == 32:
        try:
            int(compact, 16)
        except ValueError:
            return None
        return compact.upper()
    return None


def build_sign_url(course_sched_id: str, timestamp: int, user_id: str | None = None) -> str:
    """Sign-in URL using a course ID."""
    params: dict[str, Any] = {"courseSchedId": course_sched_id, "timestamp": timestamp}
    if user_id:
        params["id"] = user_id
    return f"{SIGN_URL}?{urllib.parse.urlencode(params)}"


def build_timetable_sign_url(uuid: str, timestamp: int, user_id: str | None = None) -> str:
    """Sign-in URL using a UUID (``timeTableId``)."""
    params: dict[str, Any] = {"timeTableId": uuid, "timestamp": timestamp}
    if user_id:
        params["id"] = user_id
    return f"{SIGN_URL}?{urllib.parse.urlencode(params)}"


def extract_clock_time(value: str) -> str | None:
    """Extract ``10:25:00`` from ``2026-03-25 10:25:00``."""
    if not value:
        return None
    tail = value.strip().split()[-1]
    parts = tail.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_class_datetime(date: str, value: str) -> datetime | None:
    """Combine a course date and a timestamp string into a :class:`datetime`.

    The result is timezone-aware (``Asia/Shanghai``), so converting it to a
    timestamp does not depend on the process timezone.
    """
    clock = extract_clock_time(value)
    if not clock:
        return None
    compact = normalize_date(date)
    try:
        return datetime.strptime(f"{compact} {clock}", "%Y%m%d %H:%M:%S").replace(
            tzinfo=UCAS_TIMEZONE
        )
    except ValueError:
        return None


def format_time_range(begin: str, end: str) -> str:
    """Human-readable time range, keeping only hours and minutes."""
    left = extract_clock_time(begin) or "--"
    right = extract_clock_time(end) or "--"
    if left == "--" and right == "--":
        return "--"
    return f"{left[:5]} ~ {right[:5]}"


def format_date_from_ms(timestamp_ms: int) -> str:
    """Format a millisecond timestamp as ``yyyyMMdd`` in UCAS time (UTC+8)."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UCAS_TIMEZONE).strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class UcasClient:
    """Thin wrapper around the upstream iClass API.

    An ``http_client`` can be injected for testing; otherwise a
    :class:`httpx.Client` with timeouts is created automatically.
    """

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=REQUEST_TIMEOUT),
            follow_redirects=False,
        )
        self._offset_ms: int = 0
        self._offset_valid = False
        self._offset_fetched_at: float = 0.0

    # -- Lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "UcasClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Internal helpers ------------------------------------------------- #

    def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.post(url, **kwargs)
        except httpx.TimeoutException as exc:
            raise UcasError("Request timed out; check your network", "NETWORK_TIMEOUT", "request") from exc
        except httpx.HTTPError as exc:
            raise UcasError(f"Network error: {exc}", "NETWORK_ERROR", "request") from exc

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.get(url, **kwargs)
        except httpx.TimeoutException as exc:
            raise UcasError("Request timed out; check your network", "NETWORK_TIMEOUT", "request") from exc
        except httpx.HTTPError as exc:
            raise UcasError(f"Network error: {exc}", "NETWORK_ERROR", "request") from exc

    @staticmethod
    def _json(response: httpx.Response, code: str, stage: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise UcasError("Upstream returned non-JSON data", code, stage) from exc
        if not isinstance(data, dict):
            raise UcasError("Upstream returned an unexpected format", code, stage)
        return data

    # -- Login ------------------------------------------------------------ #

    def login(self, username: str, password: str) -> LoginResult:
        """Log in and return ``sessionId`` and the user ``id``."""
        validate_credentials(username, password)
        response = self._post(
            LOGIN_URL,
            content=build_login_body(username, password),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": LOGIN_UA,
            },
        )
        if response.status_code != 200:
            raise UcasError(
                f"Login endpoint HTTP error: {response.status_code}",
                "UPSTREAM_LOGIN_HTTP",
                "login",
            )

        data = self._json(response, "UPSTREAM_LOGIN_BAD_JSON", "login")
        result = data.get("result") or {}
        session_id = str(result.get("sessionId") or "")
        user_id = str(result.get("id") or "")

        if data.get("STATUS") != "0" or not session_id or not user_id:
            raise UcasError("Login failed; check your mail and password", "AUTH_FAILED", "login")

        return LoginResult(session_id=session_id, user_id=user_id)

    # -- Schedule --------------------------------------------------------- #

    def query_courses(self, username: str, password: str, date: str) -> list[Course]:
        """Log in and fetch the course list for the given date."""
        normalized_date = normalize_date(date)
        login = self.login(username, password)

        url = (
            f"{SCHEDULE_URL}?"
            + urllib.parse.urlencode({"id": login.user_id, "dateStr": normalized_date})
        )
        response = self._get(
            url,
            headers={"sessionId": login.session_id, "User-Agent": API_UA},
        )
        if response.status_code != 200:
            raise UcasError(
                f"Schedule endpoint HTTP error: {response.status_code}",
                "UPSTREAM_SCHEDULE_HTTP",
                "schedule",
            )

        data = self._json(response, "UPSTREAM_SCHEDULE_BAD_JSON", "schedule")
        if data.get("STATUS") != "0":
            raise UcasError(
                "Schedule query failed or there are no courses that day",
                "UPSTREAM_SCHEDULE_STATUS",
                "schedule",
            )

        result = data.get("result")
        if not isinstance(result, list):
            return []
        return [Course.from_upstream(item) for item in result if isinstance(item, dict)]

    # -- Sign-in ---------------------------------------------------------- #

    def sign(
        self,
        username: str,
        password: str,
        identifier: str,
        timestamp: int | None = None,
    ) -> SignResult:
        """Log in and sign in for a course.

        ``identifier`` may be a 7-digit course ID or a 32-character hex UUID.
        When ``timestamp`` is omitted, a clock-calibrated server timestamp is
        used.
        """
        course_id = normalize_course_sched_id(identifier)
        timetable_id = normalize_uuid(identifier)
        if course_id is None and timetable_id is None:
            raise UcasError(
                "Invalid course ID or UUID format",
                "BAD_IDENTIFIER",
                "request",
            )

        login = self.login(username, password)

        if timestamp is None:
            timestamp = self.sign_timestamp()

        if course_id is not None:
            url = build_sign_url(course_id, timestamp, login.user_id)
        else:
            url = build_timetable_sign_url(timetable_id or "", timestamp, login.user_id)

        response = self._get(
            url,
            headers={"sessionId": login.session_id, "User-Agent": API_UA},
        )
        if response.status_code != 200:
            raise UcasError(
                f"Sign-in endpoint HTTP error: {response.status_code}",
                "UPSTREAM_SIGN_HTTP",
                "sign",
            )

        data = self._json(response, "UPSTREAM_SIGN_BAD_JSON", "sign")
        return parse_sign_response(data)

    # -- Clock calibration ------------------------------------------------ #

    def get_server_offset_ms(self, force: bool = False) -> int:
        """Return ``server time - local time`` in milliseconds.

        The result is cached for :data:`TIME_OFFSET_TTL_MS`. On failure it
        falls back to the previous cached value, then to 0.
        """
        now_ms = time.time() * 1000
        if not force and self._offset_valid and now_ms - self._offset_fetched_at < TIME_OFFSET_TTL_MS:
            return self._offset_ms

        try:
            start_ms = time.time() * 1000
            response = self._post(
                f"{TIMESTAMP_URL}?id={random.randint(0, 999_999)}",
                headers={"User-Agent": API_UA, "Connection": "Keep-Alive"},
                timeout=TIMESTAMP_TIMEOUT,
            )
            data = self._json(response, "UPSTREAM_TIMESTAMP_BAD_JSON", "timestamp")
            timestamp = data.get("timestamp")
            if data.get("STATUS") == "0" and isinstance(timestamp, (int, float)):
                latency_ms = max(0.0, time.time() * 1000 - start_ms)
                server_time = float(timestamp) + latency_ms / 2
                self._offset_ms = int(server_time - time.time() * 1000)
                self._offset_valid = True
                self._offset_fetched_at = time.time() * 1000
                return self._offset_ms
        except UcasError:
            pass

        if self._offset_valid:
            return self._offset_ms

        self._offset_ms = 0
        self._offset_valid = True
        self._offset_fetched_at = time.time() * 1000
        return 0

    def server_now_ms(self) -> int:
        """Calibrated current time in milliseconds; may trigger a sync request."""
        return int(time.time() * 1000) + self.get_server_offset_ms()

    def cached_now_ms(self) -> int:
        """Current time in milliseconds using the cached offset (no network)."""
        offset = self._offset_ms if self._offset_valid else 0
        return int(time.time() * 1000) + offset

    def sign_timestamp(self) -> int:
        """Timestamp accepted by the sign-in endpoint (clock buffer applied)."""
        return self.server_now_ms() - SIGN_TIMESTAMP_BUFFER_MS


def parse_sign_response(data: dict[str, Any]) -> SignResult:
    """Parse the upstream sign-in response, supporting both ``STATUS`` and ``ERRCODE`` styles."""
    result = data.get("result") or {}
    if not isinstance(result, dict):
        result = {}

    upstream_status = str(data.get("STATUS") or data.get("ERRCODE") or "")
    stu_sign_id = str(result.get("stuSignId") or "")
    stu_sign_status = str(result.get("stuSignStatus") or "")
    # Upstream messages are passed through verbatim (they may be in Chinese).
    upstream_message = str(
        result.get("msg")
        or data.get("ERRMSG")
        or data.get("msg")
        or data.get("message")
        or ""
    )

    if upstream_status == "0" and stu_sign_status == "1":
        return SignResult(True, "Sign-in successful", upstream_status, stu_sign_id, stu_sign_status)

    if upstream_status == "0" and stu_sign_status and stu_sign_status != "1":
        return SignResult(
            False,
            "Sign-in request submitted but not completed",
            upstream_status,
            stu_sign_id,
            stu_sign_status,
        )

    return SignResult(
        False,
        upstream_message or "Sign-in failed, please try again later",
        upstream_status,
        stu_sign_id,
        stu_sign_status,
    )


# --------------------------------------------------------------------------- #
# Sign-in window
# --------------------------------------------------------------------------- #


@dataclass
class SignWindow:
    open_at: int
    close_at: int

    def contains(self, timestamp_ms: int) -> bool:
        return self.open_at <= timestamp_ms <= self.close_at


def compute_sign_window(course: Course, date: str) -> SignWindow | None:
    """Compute the sign-in window: 30 minutes before class until class ends."""
    begin = parse_class_datetime(date, course.class_begin_time)
    end = parse_class_datetime(date, course.class_end_time)
    if begin is None or end is None:
        return None
    open_at = int((begin - timedelta(milliseconds=SIGN_WINDOW_BEFORE_MS)).timestamp() * 1000)
    close_at = int(end.timestamp() * 1000)
    return SignWindow(open_at=open_at, close_at=close_at)


def sign_window_state(course: Course, date: str, now_ms: int) -> str:
    """Return ``open`` / ``before`` / ``after`` / ``unknown``."""
    window = compute_sign_window(course, date)
    if window is None:
        return "unknown"
    if now_ms < window.open_at:
        return "before"
    if now_ms > window.close_at:
        return "after"
    return "open"
