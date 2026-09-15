"""Phone notifications over a Feishu / Lark group custom bot.

The automatic sign-in runner has to tell you about a successful sign-in, a
failed one, and an error while fetching the day's schedule. That is the whole
job of this module: turn a small :class:`Message` into a text message on the
configured Feishu / Lark custom-bot webhook.

The webhook (``https://open.feishu.cn/open-apis/bot/v2/hook/...``) already
carries the bot token. When the bot is configured with the "signature"
security mode, set ``UCAS_FEISHU_SECRET``; ``timestamp`` and ``sign`` are then
added to the JSON body. Messages are sent as Feishu ``text``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

DEFAULT_TIMEOUT = 10.0

#: Feishu text messages are limited to ~30 KB.
FEISHU_MAX_BYTES = 30000


class NotifyError(Exception):
    """Raised when the message cannot be delivered."""


class NotifyConfigError(Exception):
    """Raised when the notifier is not configured correctly."""


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Message:
    """A push message."""

    title: str
    body: str = ""


# --------------------------------------------------------------------------- #
# Notifier
# --------------------------------------------------------------------------- #


class Notifier:
    """Interface the runner talks to; :class:`FeishuNotifier` implements it."""

    name = "notifier"

    def send(self, message: Message) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        pass


def _truncate_utf8(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def feishu_signature(timestamp: int, secret: str) -> str:
    """Return the Feishu custom-bot signature for ``timestamp`` and ``secret``.

    Feishu's scheme differs from most services: it HMAC-SHA256s with the
    ``"{timestamp}\\n{secret}"`` string as the *key* and an empty *message*, then
    base64-encodes the digest. ``timestamp`` and ``sign`` are sent inside the
    JSON body, not the URL.
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuNotifier(Notifier):
    """Post a text message to a Feishu / Lark group custom-bot webhook.

    ``webhook`` is the full bot URL (``https://open.feishu.cn/open-apis/bot/v2/hook/...``).
    The URL already carries the token; when the bot is configured with the
    "signature" security setting, pass the secret in ``secret``. An
    ``http_client`` can be injected for testing.
    """

    name = "feishu"

    def __init__(
        self,
        webhook: str,
        secret: str = "",
        http_client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.webhook = webhook.strip()
        self.secret = secret.strip()
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def build_payload(self, message: Message, timestamp: int | None = None) -> dict[str, Any]:
        """Build the JSON body, adding ``timestamp``/``sign`` when a secret is set.

        ``timestamp`` can be injected for deterministic tests.
        """
        text = _truncate_utf8(f"{message.title}\n{message.body}".strip(), FEISHU_MAX_BYTES)
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if self.secret:
            ts = int(timestamp if timestamp is not None else time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = feishu_signature(ts, self.secret)
        return payload

    def send(self, message: Message) -> None:
        try:
            response = self.client.post(
                self.webhook,
                json=self.build_payload(message),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise NotifyError(f"feishu: {exc}") from exc
        if response.status_code >= 400:
            raise NotifyError(f"feishu: upstream returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise NotifyError("feishu: upstream returned non-JSON data") from exc
        code = data.get("code")
        if code is None:
            code = data.get("StatusCode")
        if str(code) != "0":
            raise NotifyError(
                f"feishu: {data.get('msg') or data.get('StatusMessage') or f'code {code}'}"
            )

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_notifier(
    env: Mapping[str, str] | None = None,
    http_client: httpx.Client | None = None,
) -> FeishuNotifier:
    """Build the Feishu notifier from environment variables.

    ``UCAS_FEISHU_WEBHOOK`` is required; ``UCAS_FEISHU_SECRET`` is optional and
    only needed when the bot uses the "signature" security mode.
    """
    env = env if env is not None else os.environ
    webhook = (env.get("UCAS_FEISHU_WEBHOOK") or "").strip()
    if not webhook:
        raise NotifyConfigError("feishu: UCAS_FEISHU_WEBHOOK is required")
    return FeishuNotifier(
        webhook=webhook,
        secret=(env.get("UCAS_FEISHU_SECRET") or "").strip(),
        http_client=http_client,
    )
