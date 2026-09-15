"""Interactive command-line interface.

There is no full-screen UI. The program asks a short sequence of questions in
the terminal -- in the spirit of FreeBSD's ``adduser`` -- and then runs a small
command loop over today's courses:

* :func:`_login` prompts for the mail and password until the credentials
  are accepted (the password is read with :func:`getpass.getpass`, so it is
  never echoed).
* :func:`_course_loop` lists today's courses and reads commands such as a
  course number, ``qr <number>``, ``r``, ``?`` or ``q``.

The date is never asked for: it is taken from the calibrated UCAS server clock,
so it is always "today" as the upstream understands it.
"""

from __future__ import annotations

import getpass
import os
import sys
import time
import unicodedata
from dataclasses import dataclass

import qrcode

from common import __version__
from common.api import (
    Course,
    SignResult,
    UcasClient,
    UcasError,
    build_sign_url,
    format_date_from_ms,
    format_time_range,
    normalize_course_sched_id,
    normalize_uuid,
    sign_window_state,
)

try:  # POSIX single-key input, used by the live QR refresh
    import select
    import termios
    import tty

    _HAS_TERMIOS = True
except ImportError:  # pragma: no cover - Windows
    _HAS_TERMIOS = False

WINDOW_TEXT = {
    "open": "Open",
    "before": "Not open",
    "after": "Closed",
    "unknown": "--",
}

HELP_LINES = (
    "  <number>     sign in for the course with that number",
    "  qr <number>  show a scannable sign-in QR code for that course",
    "  r            reload today's courses",
    "  ?            show this help",
    "  q            quit",
)

USAGE = """\
usage: tui

Sign in for UCAS courses from the terminal. Prompts for your mail and
password, lists today's courses, and signs in on request.

options:
  -h, --help     show this help and exit
  -V, --version  show the version and exit
"""


class _Quit(Exception):
    """Raised internally when the user asks to stop."""


@dataclass
class Session:
    """Credentials captured at login and reused for the whole session."""

    username: str
    password: str


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #


def _char_width(char: str) -> int:
    """Display width of a single character (CJK glyphs are two columns wide)."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    padding = max(0, width - _width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def _ask(label: str, default: str = "") -> str:
    """Prompt for a line of input, honoring an optional default."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{label}{suffix}: ").strip()
    except EOFError:
        raise _Quit from None
    return answer or default


def _ask_password(label: str = "Password") -> str:
    try:
        return getpass.getpass(f"{label}: ")
    except EOFError:
        raise _Quit from None


def _pretty_date(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


def _login(client: UcasClient) -> Session:
    """Prompt for the mail and password until the upstream accepts them.

    The mail is always entered interactively; neither a command-line argument
    nor an environment variable pre-fills it.
    """
    while True:
        username = _ask("Mail")
        if not username:
            print("Mail is required.")
            continue
        password = _ask_password()
        if not password:
            print("Password is required.")
            continue

        print("Signing in... ", end="", flush=True)
        try:
            client.login(username, password)
        except UcasError as exc:
            print("failed.")
            print(f"  {exc.message}")
            print()
            continue
        print("done.")
        return Session(username=username, password=password)


# --------------------------------------------------------------------------- #
# Course list
# --------------------------------------------------------------------------- #


def _today(client: UcasClient) -> str:
    """Today's date (``yyyyMMdd``) from the calibrated server clock."""
    return format_date_from_ms(client.server_now_ms())


def _print_courses(client: UcasClient, courses: list[Course], date: str) -> None:
    if not courses:
        print(f"  No courses found for {_pretty_date(date)}.")
        return

    now = client.cached_now_ms()
    rows = [("#", "Course", "Teacher", "Time", "Status", "Sign-in window")]
    for index, course in enumerate(courses, start=1):
        state = sign_window_state(course, date, now)
        rows.append(
            (
                str(index),
                course.course_name or "--",
                course.teacher_name or "--",
                format_time_range(course.class_begin_time, course.class_end_time),
                course.status_text,
                WINDOW_TEXT.get(state, "--"),
            )
        )

    widths = [max(_width(row[column]) for row in rows) for column in range(len(rows[0]))]
    print()
    for row_index, row in enumerate(rows):
        cells = [
            _pad(cell, widths[column], align="right" if column == 0 else "left")
            for column, cell in enumerate(row)
        ]
        print("  " + "  ".join(cells).rstrip())
        if row_index == 0:
            print("  " + "  ".join("-" * width for width in widths))
    print()


def _load_courses(client: UcasClient, session: Session, date: str) -> list[Course]:
    print(f"Fetching courses for {_pretty_date(date)}... ", end="", flush=True)
    try:
        courses = client.query_courses(session.username, session.password, date)
    except UcasError as exc:
        print("failed.")
        print(f"  {exc.message}")
        return []
    print("done.")
    _print_courses(client, courses, date)
    return courses


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #


def _report_sign(result: SignResult) -> bool:
    if result.success:
        print("done.")
        detail = f" (sign-in record {result.stu_sign_id})" if result.stu_sign_id else ""
        print(f"  ✓ {result.message}{detail}")
    else:
        print("failed.")
        detail = f" (upstream status {result.upstream_status})" if result.upstream_status else ""
        print(f"  ✗ {result.message}{detail}")
    return result.success


def _sign(client: UcasClient, session: Session, identifier: str, label: str) -> bool:
    if normalize_course_sched_id(identifier) is None and normalize_uuid(identifier) is None:
        print("  Invalid ID: use a 7-digit course ID or a 32-char hex UUID.")
        return False

    print(f"Signing in for {label}... ", end="", flush=True)
    try:
        result = client.sign(session.username, session.password, identifier)
    except UcasError as exc:
        print("failed.")
        print(f"  {exc.message}")
        return False
    return _report_sign(result)


def _print_help() -> None:
    print("Enter a course number to sign in, or one of:")
    for line in HELP_LINES:
        print(line)


# --------------------------------------------------------------------------- #
# QR code
# --------------------------------------------------------------------------- #

_QR_STYLE = "\x1b[30;47m"  # black foreground on white background
_QR_RESET = "\x1b[0m"
_QR_HINT = "Scan with the UCAS mobile app. Press 'b' to go back."

# Keep the on-screen code fresh: the sign-in URL embeds a timestamp that the
# upstream validates, so a stale code is rejected. Matches the web version.
QR_REFRESH_SECONDS = 5.0


def _qr_matrix(payload: str) -> list[list[bool]]:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    code.add_data(payload)
    code.make(fit=True)
    return code.get_matrix()


def _qr_lines(payload: str) -> list[str]:
    """Return the QR code as lines of half-block characters."""
    matrix = _qr_matrix(payload)
    width = len(matrix[0]) if matrix else 0
    lines = []
    for row_index in range(0, len(matrix), 2):
        top = matrix[row_index]
        bottom = matrix[row_index + 1] if row_index + 1 < len(matrix) else [False] * width
        cells = []
        for column in range(width):
            upper, lower = top[column], bottom[column]
            if upper and lower:
                cells.append("█")
            elif upper:
                cells.append("▀")
            elif lower:
                cells.append("▄")
            else:
                cells.append(" ")
        lines.append(_QR_STYLE + "".join(cells) + _QR_RESET)
    return lines


def _read_key(timeout: float) -> str | None:
    """Read a single keypress, or return ``None`` when ``timeout`` elapses.

    Without a POSIX terminal (Windows, or output piped/redirected) there is no
    keypress to read, so the timeout is simply slept through.
    """
    if not (_HAS_TERMIOS and sys.stdin.isatty() and sys.stdout.isatty()):
        time.sleep(timeout)
        return None
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        return os.read(fd, 1).decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _wait_for_back(seconds: float) -> bool:
    """Wait up to ``seconds``, returning ``True`` as soon as ``b`` is pressed.

    Any other key is ignored: the QR code refreshes on its own 5-second
    schedule, there is no way to refresh it earlier.
    """
    deadline = time.monotonic() + seconds
    remaining = seconds
    while remaining > 0:
        key = _read_key(remaining)
        if key is None:
            return False
        if key.lower() == "b":
            return True
        remaining = deadline - time.monotonic()
    return False


def _print_qr(payload: str) -> None:
    """Render ``payload`` as a QR code using half-block characters.

    Colours are forced (black on white) so the code scans regardless of the
    terminal's light/dark theme.
    """
    print()
    for line in _qr_lines(payload):
        print("  " + line)
    print()


def _show_qr(client: UcasClient, course: Course) -> None:
    """Display a live sign-in QR code for ``course``.

    The QR encodes the upstream sign-in URL (without a user id, exactly like the
    web version). The URL carries a timestamp, so it must stay fresh: a new code
    is printed every :data:`QR_REFRESH_SECONDS` seconds until the user goes back.
    """
    _show_qr_refreshing(client, course, course.course_name or course.id)


def _show_qr_refreshing(client: UcasClient, course: Course, label: str) -> None:
    """Print a fresh QR code every few seconds; press 'b' to go back.

    The output simply scrolls -- the newest code is always the last one -- so it
    works in any terminal without cursor-positioning support. The hint line is
    repeated with every code so it scrolls along with it. Codes are refreshed on
    a fixed schedule only, never on a keypress.
    """
    interval = QR_REFRESH_SECONDS
    print(f"Sign-in QR for '{label}': a new code is printed every {interval:g}s.")
    while True:
        timestamp = client.sign_timestamp()
        payload = build_sign_url(course.id, timestamp)
        _print_qr(payload)
        print(f"  {payload}")
        print(f"  {_QR_HINT}")
        if _wait_for_back(interval):
            return


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


def _course_loop(client: UcasClient, session: Session) -> None:
    date = _today(client)
    courses = _load_courses(client, session, date)
    _print_help()

    while True:
        print()
        command = _ask("Select")
        if not command:
            continue
        lowered = command.lower()

        if lowered in ("q", "quit", "exit"):
            raise _Quit

        if lowered in ("?", "help", "h"):
            _print_help()
            continue

        if lowered in ("r", "reload", "refresh"):
            date = _today(client)
            courses = _load_courses(client, session, date)
            continue

        if lowered == "qr" or lowered.startswith("qr "):
            argument = command[2:].strip()
            if not argument:
                argument = _ask("Course number")
            if not argument.isdigit():
                print("  Usage: qr <number>")
                continue
            index = int(argument)
            if not 1 <= index <= len(courses):
                if courses:
                    print(f"  Enter a number between 1 and {len(courses)}.")
                else:
                    print("  No courses loaded; use 'r' to reload.")
                continue
            _show_qr(client, courses[index - 1])
            continue

        if command.isdigit():
            index = int(command)
            if not 1 <= index <= len(courses):
                if courses:
                    print(f"  Enter a number between 1 and {len(courses)}.")
                else:
                    print("  No courses loaded; use 'r' to reload.")
                continue
            course = courses[index - 1]
            label = course.course_name or course.id
            if _sign(client, session, course.id, f"'{label}'"):
                date = _today(client)
                courses = _load_courses(client, session, date)
            continue

        print(f"  Unrecognized input: {command!r}. Type ? for help.")


def main(argv: list[str] | None = None, *, client: UcasClient | None = None) -> int:
    """Program entry point."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in args):
        print(USAGE, end="")
        return 0
    if any(arg in ("-V", "--version") for arg in args):
        print(f"ucas-course-sign-in {__version__}")
        return 0

    print("UCAS Course Sign-in")
    print("===================")

    active_client = client or UcasClient()
    try:
        session = _login(active_client)
        _course_loop(active_client, session)
    except _Quit:
        print("\nBye.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        active_client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
