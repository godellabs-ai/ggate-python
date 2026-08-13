"""The SDK's transport: scan via the Console's HTTP API.

Scans go to the Console's ``POST /api/v1/scan`` endpoint, which runs the full
pipeline — normalize, OCR/extraction of attachments, deterministic rules, the
security classifier, DLP, threat intel, document intelligence, and policy —
records the event, and returns the decision.

Auth is the same scale path the agents use: the IAM API key is exchanged ONCE at
``/api/v1/agent/token`` for a short-lived JWT, and scans carry ``Authorization:
Bearer`` — verified signature-only on the Console, so the per-scan cost is a
stateless check instead of an Argon2 hash (~1s). The token is re-exchanged
shortly before expiry, or after a 401.

Collector registration is implicit — the Console reconciles its fleet registry
from the events — so ``collector_ready`` is a no-op.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict

from ..exceptions import GgateTransportError
from .config import Config

# Re-exchange this long before the access token expires.
_TOKEN_SLACK_SECS = 60.0


class ConsoleTransport:
    def __init__(self, config: Config):
        self.config = config
        self._base_url = (config.console_url or "").rstrip("/")
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        op = request.get("op")
        if op == "collector_ready":
            return {"kind": "ok"}
        if op != "scan_text":
            raise GgateTransportError(f"op {op!r} is not supported by the console transport")

        body = json.dumps(
            {"event": request.get("event"), "redaction": request.get("redaction")},
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        try:
            return self._scan(body, self._token())
        except GgateTransportError as exc:
            if "HTTP 401" not in str(exc):
                raise
            # Token expired server-side (or was revoked): exchange fresh and retry once.
            with self._lock:
                self._access_token = None
            return self._scan(body, self._token())

    async def request_async(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self.request, request)

    def _scan(self, body: bytes, token: str) -> Dict[str, Any]:
        http_request = urllib.request.Request(
            f"{self._base_url}/api/v1/scan",
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.config.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", errors="replace") if exc.fp else ""
            raise GgateTransportError(f"console scan failed: HTTP {exc.code} {detail}") from exc
        except (OSError, ValueError) as exc:
            raise GgateTransportError(f"console unreachable ({self._base_url}): {exc}") from exc

    def _token(self) -> str:
        """Current access token, exchanging the API key when missing or near expiry."""
        with self._lock:
            if self._access_token and time.monotonic() < self._token_expires_at - _TOKEN_SLACK_SECS:
                return self._access_token
            http_request = urllib.request.Request(
                f"{self._base_url}/api/v1/agent/token",
                method="POST",
                headers={"x-api-key": self.config.api_key or ""},
            )
            try:
                with urllib.request.urlopen(http_request, timeout=self.config.timeout) as response:
                    data = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                raise GgateTransportError(
                    f"console rejected the API key (token exchange HTTP {exc.code})"
                ) from exc
            except (OSError, ValueError) as exc:
                raise GgateTransportError(f"console unreachable ({self._base_url}): {exc}") from exc
            token = data.get("access_token")
            if not token:
                raise GgateTransportError("console token exchange returned no access_token")
            self._access_token = token
            self._token_expires_at = time.monotonic() + float(data.get("expires_in") or 3600)
            return token
