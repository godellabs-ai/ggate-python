"""Best-effort collector-side redaction before content leaves the process.

Off by default: the Console is the detection engine, so masking here would hide exactly the
secrets it exists to catch. Deployments that would rather lose those detections than send the
content set ``GGATE_REDACT=1``."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Tuple


SECRET_PATTERNS: Iterable[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


@dataclass(frozen=True)
class RedactionSummary:
    content_sha256: str | None = None
    redaction_count: int = 0

    def to_json(self) -> dict:
        data = {"redaction_count": self.redaction_count}
        if self.content_sha256:
            data["content_sha256"] = self.content_sha256
        return data


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact_text(text: str) -> Tuple[str, int]:
    count = 0
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted, changed = pattern.subn("[REDACTED]", redacted)
        count += changed
    return redacted, count
