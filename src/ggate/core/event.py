"""RuntimeEvent construction, matching the Rust `gate/v1` contract.

The shapes here mirror ``shared/ggate-core/src/event.rs`` exactly — that crate is the
source of truth. An SDK event carries ``identity.agent_source = "agent-framework"``
and ``collector.collector_type = "sdk"``; the specific AI framework travels in
``collector.labels.framework`` and ``source.client``, which is what the Console uses
to render `agent-framework:<framework>` connectors.

The event states only what the SDK actually knows. Anything the receiver can work out
for itself is left off the wire: ``schema_version`` (a constant), and the payload sizes
``length`` / ``output_len``, which the Console derives from the text in the same
payload (``RuntimeEvent::fill_derived``). Sending them again would be a second source of
truth for the same fact, and a chance for the two to disagree.

What stays is what only this process knows and the Console reads: the event id (also the
retry idempotency key), the client-side timestamp, identity and seat user, session and
correlation ids the caller supplied, and the collector metadata the fleet registry is
reconciled from — SDK name and version, platform, framework.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import platform
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .config import Config
from .redactor import RedactionSummary, redact_text, sha256_hex

SDK_VERSION = "0.2.0"

# Normalized to the Rust `std::env::consts` vocabulary the rest of the fleet reports
# (platform_os is a Console dimension, so casing must match the hook collectors).
_PLATFORM_OS = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(
    platform.system(), (platform.system() or os.name).lower()
)
_PLATFORM_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}.get(
    platform.machine().lower(), platform.machine().lower() or None
)

_FILE_ACTIONS = {"read", "write", "edit", "delete"}
_TOOL_RESULT_KINDS = {"shell", "file_read", "web", "mcp", "other"}
_TOKEN_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")

_context: ContextVar[Dict[str, Any]] = ContextVar("ggate_context", default={})
_DEFAULT_SESSION_ID = f"py-sdk-{uuid.uuid4()}"


@dataclass(frozen=True)
class Attachment:
    id: Optional[str] = None
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    source: str = "upload"
    capture: str = "metadata_only"
    text: Optional[str] = None
    content_base64: Optional[str] = None
    truncated: bool = False
    redaction_count: Optional[int] = None

    def to_json(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "source": self.source,
            "capture": self.capture,
        }
        for key in ("id", "filename", "mime_type", "size", "sha256", "text", "content_base64"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.truncated:
            data["truncated"] = True
        if self.redaction_count is not None:
            data["redaction_count"] = self.redaction_count
        return data


def attachment_from_path(path: str | os.PathLike[str], *, source: str = "upload", config: Config) -> Attachment:
    file_path = Path(path)
    stat = file_path.stat()
    text = None
    content_base64 = None
    redaction_count = None
    capture = "metadata_only"
    mime_type = mimetypes.guess_type(file_path.name)[0]

    captured = bytearray()
    hasher = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            if len(captured) <= config.max_file_bytes:
                needed = config.max_file_bytes + 1 - len(captured)
                captured.extend(chunk[:needed])
    data = bytes(captured)
    truncated = len(data) > config.max_file_bytes
    data = data[: config.max_file_bytes]
    digest = hasher.hexdigest()
    if config.capture_file_text and (mime_type or "").startswith("text/"):
        raw = data.decode("utf-8", errors="replace")
        text, redaction_count = redact_text(raw) if config.redact else (raw, 0)
        capture = "text"
    elif config.capture_file_text:
        content_base64 = base64.b64encode(data).decode("ascii")
        capture = "content"

    return Attachment(
        filename=file_path.name,
        mime_type=mime_type,
        size=stat.st_size,
        sha256=digest,
        source=source,
        capture=capture,
        text=text,
        content_base64=content_base64,
        truncated=truncated,
        redaction_count=redaction_count,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_dict(data: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None and v != {} and v != []}


@contextmanager
def event_context(**metadata) -> Iterator[None]:
    """Bind metadata (framework, session_id, model, ...) to every event in this scope."""
    current = dict(_context.get())
    current.update({k: v for k, v in metadata.items() if v is not None})
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


def _redact_value(value: Any, depth: int = 0) -> Tuple[Any, int]:
    """Redact every string leaf of a JSON-ish value (tool inputs routinely carry secrets)."""
    if isinstance(value, str):
        return redact_text(value)
    if depth >= 8:
        return value, 0
    if isinstance(value, Mapping):
        total = 0
        out = {}
        for key, item in value.items():
            out[key], changed = _redact_value(item, depth + 1)
            total += changed
        return out, total
    if isinstance(value, (list, tuple)):
        total = 0
        items = []
        for item in value:
            redacted, changed = _redact_value(item, depth + 1)
            items.append(redacted)
            total += changed
        return items, total
    return value, 0


def _token_usage(value: Any) -> Optional[Dict[str, int]]:
    """Coerce a usage mapping to the wire `TokenUsage` shape, dropping unknown keys."""
    if not isinstance(value, Mapping):
        return None
    usage = {}
    for key in _TOKEN_USAGE_KEYS:
        raw = value.get(key)
        if isinstance(raw, (int, float)) and raw >= 0:
            usage[key] = int(raw)
    return usage or None


class RuntimeEventBuilder:
    def __init__(self, config: Config):
        self.config = config

    def prompt(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        **metadata,
    ) -> Tuple[Dict[str, Any], RedactionSummary]:
        payload = {
            "surface": "prompt",
            "text": text,
            "attachments": self._attachments(attachments),
        }
        return self._event("pre", payload, primary_text=text, **metadata)

    def response(
        self,
        text: str,
        *,
        attachments: Optional[Iterable[Attachment | Mapping[str, Any]]] = None,
        stop_reason: Optional[str] = None,
        model: Optional[str] = None,
        usage: Optional[Mapping[str, Any]] = None,
        api_calls: Optional[int] = None,
        tool_calls: Optional[int] = None,
        duration_ms: Optional[int] = None,
        **metadata,
    ) -> Tuple[Dict[str, Any], RedactionSummary]:
        payload = {
            "surface": "response",
            "text": text,
            "model": model,
            "stop_reason": stop_reason,
            "usage": _token_usage(usage),
            "api_calls": api_calls,
            "tool_calls": tool_calls,
            "duration_ms": duration_ms,
            "attachments": self._attachments(attachments),
        }
        return self._event("post", payload, primary_text=text, model=model, **metadata)

    def file_event(self, path: str, *, action: str = "read", content_len: Optional[int] = None, **metadata):
        if action not in _FILE_ACTIONS:
            raise ValueError(f"action must be one of {sorted(_FILE_ACTIONS)}, got {action!r}")
        payload = {
            "surface": "file_read" if action == "read" else "file_write",
            "action": action,
            "path": path,
            "content_len": content_len,
        }
        return self._event("post", payload, primary_text=path, **metadata)

    def tool_call(self, tool: str, *, input_summary=None, server: Optional[str] = None, service=None, **metadata):
        text = str(input_summary) if input_summary is not None else tool
        payload = {
            "surface": "mcp_tool",
            # Filled with the framework name in _event when the caller names no server:
            # a framework tool has no MCP server, and the framework reads best in the Console.
            "server": server,
            "tool": tool,
            "input_summary": input_summary,
            "service": service,
        }
        return self._event("pre", payload, primary_text=text, **metadata)

    def tool_result(self, tool: str, output: str, *, kind: str = "other", server: Optional[str] = None, **metadata):
        payload = {
            "surface": "tool_result",
            "kind": kind if kind in _TOOL_RESULT_KINDS else "other",
            "tool": tool,
            "server": server,
            "output": output,
            "ok": True,
        }
        return self._event("post", payload, primary_text=output, **metadata)

    def _event(self, phase: str, payload: Dict[str, Any], *, primary_text: str, **metadata):
        merged = dict(_context.get())
        merged.update({k: v for k, v in metadata.items() if v is not None})
        framework = str(merged.get("framework", "generic"))
        model = merged.get("model")

        if payload.get("surface") == "mcp_tool" and not payload.get("server"):
            payload["server"] = framework

        content_sha = sha256_hex(primary_text) if self.config.redact else None
        redaction_count = 0
        if self.config.redact:
            payload, redaction_count = self._redact_payload(payload)

        event = {
            "id": str(uuid.uuid4()),
            "timestamp": _now(),
            "phase": phase,
            "identity": _clean_dict(
                {
                    "org_id": self.config.org_id,
                    "user": self.config.user,
                    "host": self.config.host,
                    "workstation_id": self.config.workstation_id,
                    "agent_source": "agent-framework",
                    "agent_version": SDK_VERSION,
                    "install_source": "manual",
                }
            ),
            "session": _clean_dict(
                {
                    "session_id": merged.get("session_id") or _DEFAULT_SESSION_ID,
                    # Only a correlation the caller actually established. A fresh per-event UUID
                    # would correlate one event with itself and fill the Console's correlation
                    # column with noise.
                    "correlation_id": merged.get("correlation_id"),
                    "parent_event_id": merged.get("parent_event_id"),
                    "repo": merged.get("repo"),
                    "permission_mode": merged.get("permission_mode"),
                }
            ),
            "payload": _clean_dict(payload),
            "collector": _clean_dict(
                {
                    "collector_id": self.config.collector_id,
                    "collector_type": "sdk",
                    "name": "ggate-python-sdk",
                    "version": SDK_VERSION,
                    "mode": "enforce" if self.config.mode == "sync" else "audit",
                    "platform": _clean_dict({"os": _PLATFORM_OS, "arch": _PLATFORM_ARCH}),
                    "labels": {
                        **self.config.labels,
                        "language": "python",
                        "framework": framework,
                    },
                }
            ),
            "source": _clean_dict(
                {
                    "surface": "api",
                    "provider": merged.get("provider"),
                    "client": framework,
                    "conversation_id": merged.get("conversation_id"),
                    "model": model,
                    "request_id": merged.get("request_id"),
                    "detail": merged.get("source_detail"),
                }
            ),
        }
        return event, RedactionSummary(content_sha, redaction_count)

    def _attachments(self, attachments):
        if not attachments:
            return []
        output: List[Dict[str, Any]] = []
        for attachment in attachments:
            if isinstance(attachment, Attachment):
                output.append(attachment.to_json())
            else:
                output.append(_clean_dict(dict(attachment)))
        return output

    def _redact_payload(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        count = 0
        clean = dict(payload)
        for key in ("text", "output", "command"):
            value = clean.get(key)
            if isinstance(value, str):
                clean[key], changed = redact_text(value)
                count += changed
        if clean.get("input_summary") is not None:
            clean["input_summary"], changed = _redact_value(clean["input_summary"])
            count += changed
        attachments = []
        for attachment in clean.get("attachments") or []:
            item = dict(attachment)
            if isinstance(item.get("text"), str):
                item["text"], changed = redact_text(item["text"])
                item["redaction_count"] = int(item.get("redaction_count") or 0) + changed
                count += changed
            attachments.append(item)
        clean["attachments"] = attachments
        return clean, count
