"""Exceptions raised by the UCAS client.

Everything this package raises on purpose derives from :class:`UcasError`,
which carries a machine-readable ``code`` and the ``stage`` (``request`` /
``login`` / ``schedule`` / ``sign``) it happened in.

Crash contract
--------------
The hierarchy encodes how each front end may react:

* The six *operational* errors (bad input, rejected credentials, network and
  upstream trouble) derive from :class:`UcasOperationalError`. They are part of
  normal operation: the automatic server catches them, logs them at
  ``CRITICAL`` -- which the notify handler pushes -- and keeps running.
* :class:`UcasNotImplementedError` deliberately derives from
  :class:`UcasError` **only**. It marks a code path whose behaviour is not
  pinned down yet, so it is never caught and crashes the process on purpose:
  an unattended server must not limp along on an unimplemented path.

So ``except UcasOperationalError`` is always safe, while
``except UcasError`` also swallows the deliberate crashes -- avoid it.
"""

from __future__ import annotations


class UcasError(Exception):
    """Base class for every deliberate error raised by this package."""

    def __init__(
        self,
        message: str,
        code: str = "UNEXPECTED_ERROR",
        stage: str = "request",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage

    def describe(self) -> str:
        """Message plus code and stage, for log lines and notifications."""
        return f"{self.message} (code={self.code}, stage={self.stage})"


class UcasOperationalError(UcasError):
    """Base class for errors the front ends survive.

    Catching this (and only this) means: log it, notify the user, carry on.
    """


class UcasTimeError(UcasOperationalError):
    """The date/time value handed in is unusable (``BAD_DATE``)."""


class UcasNetworkError(UcasOperationalError):
    """The HTTP request itself failed: timeout or connection error."""


class UcasJsonError(UcasOperationalError):
    """The upstream answer was not the expected JSON object."""


class UcasServerError(UcasOperationalError):
    """The upstream answered with an HTTP error status or an error ``STATUS``."""


class UcasAuthError(UcasOperationalError):
    """Credentials are missing, malformed or rejected by the upstream.

    A malformed pair is caught locally (``BAD_CREDENTIALS``); a pair the
    upstream refuses is reported as ``AUTH_FAILED``.
    """


class UcasUnrecognizableCourse(UcasOperationalError):
    """The identifier is neither a course ID nor a timetable UUID (``BAD_IDENTIFIER``)."""


class UcasNotImplementedError(UcasError):
    """A code path that is deliberately not implemented yet.

    Unlike every other :class:`UcasError` this one is *meant* to crash the
    caller: it signals an upstream response shape nobody has written handling
    for. Never catch it -- let it propagate, crash the process and show up in
    the logs, so the missing path gets noticed and written.
    """
