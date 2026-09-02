"""The SDK's transport: scan via the Console's HTTP API.

Scans go to the Console's ``POST /api/v1/scan`` endpoint, which runs the full
pipeline — normalize, OCR/extraction of attachments, deterministic rules, the
security classifier, DLP, threat intel, document intelligence, and policy —
records the event, and returns the decision.

Auth is the same scale path the Detection Engine uses: the IAM API key is exchanged at
``/api/v1/detection-engine/token`` for a short-lived JWT plus a long-lived refresh token.
Scans carry ``Authorization: Bearer``; the refresh endpoint rotates the pair shortly before
expiry, avoiding a fresh Argon2 API-key verification each hour.

Collector registration is implicit — the Console reconciles its fleet registry
from the events — so ``collector_ready`` is a no-op.
"""

from __future__ import annotations

import asyncio
import json
import ssl
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
        self._refresh_token: str | None = None
        self._token_expires_at = 0.0
        self._ssl_context = (
            ssl.create_default_context(cafile=config.console_ca_cert)
            if config.console_ca_cert
            else None
        )

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
        token = self._token()
        try:
            return self._scan(body, token)
        except GgateTransportError as exc:
            message = str(exc)
            if any(f"HTTP {status}" in message for status in (502, 503, 504)):
                # Re-scanning the same event id is safe; tolerate one transient proxy restart.
                return self._scan(body, token)
            if "HTTP 401" not in message:
                raise
            # Token expired server-side (or was revoked): refresh the pair and retry once.
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
            with self._urlopen(http_request) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", errors="replace") if exc.fp else ""
            raise GgateTransportError(f"console scan failed: HTTP {exc.code} {detail}") from exc
        except (OSError, ValueError) as exc:
            raise GgateTransportError(f"console unreachable ({self._base_url}): {exc}") from exc

    def _token(self) -> str:
        """Current access token, refreshing the token pair when missing or near expiry."""
        with self._lock:
            if self._access_token and time.monotonic() < self._token_expires_at - _TOKEN_SLACK_SECS:
                return self._access_token
            if self._refresh_token:
                refreshed = self._refresh_access_token()
                if refreshed:
                    return refreshed
            http_request = urllib.request.Request(
                f"{self._base_url}/api/v1/detection-engine/token",
                method="POST",
                headers={"x-api-key": self.config.api_key or ""},
            )
            try:
                with self._urlopen(http_request) as response:
                    data = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                raise GgateTransportError(
                    f"console rejected the API key (token exchange HTTP {exc.code})"
                ) from exc
            except (OSError, ValueError) as exc:
                raise GgateTransportError(f"console unreachable ({self._base_url}): {exc}") from exc
            return self._accept_token_pair(data, "exchange")

    def _refresh_access_token(self) -> str | None:
        body = json.dumps({"refresh_token": self._refresh_token}, separators=(",", ":")).encode(
            "utf-8"
        )
        http_request = urllib.request.Request(
            f"{self._base_url}/api/v1/detection-engine/token/refresh",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with self._urlopen(http_request) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                self._refresh_token = None
                return None
            detail = exc.read(300).decode("utf-8", errors="replace") if exc.fp else ""
            raise GgateTransportError(
                f"console token refresh failed: HTTP {exc.code} {detail}"
            ) from exc
        except (OSError, ValueError) as exc:
            raise GgateTransportError(f"console unreachable ({self._base_url}): {exc}") from exc
        return self._accept_token_pair(data, "refresh")

    def _accept_token_pair(self, data: Dict[str, Any], operation: str) -> str:
        token = data.get("access_token")
        if not token:
            raise GgateTransportError(f"console token {operation} returned no access_token")
        self._access_token = str(token)
        if data.get("refresh_token"):
            self._refresh_token = str(data["refresh_token"])
        self._token_expires_at = time.monotonic() + float(data.get("expires_in") or 3600)
        return self._access_token

    def _urlopen(self, request):
        kwargs = {"timeout": self.config.timeout}
        if self._ssl_context is not None:
            kwargs["context"] = self._ssl_context
        return urllib.request.urlopen(request, **kwargs)
