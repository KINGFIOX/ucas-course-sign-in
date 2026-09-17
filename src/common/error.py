"""Exceptions raised by the UCAS client.

Everything this package raises on purpose derives from :class:`UcasError`, which
carries a machine-readable ``code`` and the ``stage`` (``request`` / ``login`` /
``schedule`` / ``sign``) it happened in. Catch :class:`UcasError` to handle all
of them, or a subclass to handle one specific kind.
"""

from __future__ import annotations


class UcasError(Exception):
    """Base class for every deliberate error raised by this package."""

    def __init__(self, message: str, code: str = "UNEXPECTED_ERROR", stage: str = "request") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage

    def __str__(self) -> str:  # pragma: no cover - convenience for logging
        return self.message


class UcasTimeError(UcasError):
    """The date/time value handed in is unusable (``BAD_DATE``)."""


class UcasNotImplementedError(UcasError):
    """not implemented error"""


class UcasNetworkError(UcasError):
    """The HTTP request itself failed: timeout or connection error."""


class UcasJsonError(UcasError):
    """The upstream answer was not the expected JSON object."""


class UcasServerError(UcasError):
    """The upstream answered with an HTTP error status or an error ``STATUS``."""


class UcasAuthError(UcasError):
    """Credentials are missing, malformed or rejected by the upstream.

    A malformed pair is caught locally (``BAD_CREDENTIALS``); a pair the
    upstream refuses is reported as ``AUTH_FAILED``.
    """


class UcasUnrecognizableCourse(UcasError):
    """The identifier is neither a course ID nor a timetable UUID (``BAD_IDENTIFIER``)."""
