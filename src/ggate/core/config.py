"""Configuration and identity resolution.

The SDK scans against a **Console** and nothing else: ``GGATE_CONSOLE_URL`` +
``GGATE_API_KEY`` are the only required settings. Everything else has a default, resolved in
this order: explicit ``init(...)`` arguments, ``GGATE_*`` environment variables, the device
config an installed agent maintains at ``~/.ggate/config.yaml``, then built-in fallbacks.

That device config is read for **identity only** (org, seat user, workstation id) and only when
the file happens to exist — the SDK never talks to an agent. Sharing those values keeps SDK
events attributed to the same org/seat/device as the coding-agent and browser events from the
same machine, so the Console correlates them instead of inventing a second identity.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

#: Scan budget in seconds when nothing is configured. A ceiling, not a per-call cost: a text
#: scan answers in tens of milliseconds. Raise it (``GGATE_TIMEOUT_MS``) for prompts carrying
#: attachments, where the Console also extracts and OCRs the file before deciding.
DEFAULT_TIMEOUT = 4.0


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _config_path() -> Path:
    return Path(
        os.getenv("GGATE_CONFIG")
        or Path(os.getenv("GGATE_HOME", "~/.ggate")).expanduser() / "config.yaml"
    ).expanduser()


def _read_config_scalar(name: str) -> Optional[str]:
    """One top-level scalar from the device config.yaml (line-oriented, no YAML dep).

    Absent file, unreadable file, or missing key all resolve to ``None``: this is an optional
    source of identity defaults, never a requirement.
    """
    try:
        for raw_line in _config_path().read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip() != name:
                continue
            return value.strip().strip("'\"") or None
    except OSError:
        return None
    return None


def _default_timeout() -> float:
    """Scan budget in seconds: ``GGATE_TIMEOUT_MS``, else :data:`DEFAULT_TIMEOUT`."""
    raw = os.getenv("GGATE_TIMEOUT_MS")
    try:
        return max(1, int(raw)) / 1000 if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def _default_user() -> Optional[str]:
    """Seat identity, matching the Rust collectors' `seat_user`: explicit GGATE_USER /
    GGATE_USER_EMAIL, else the device config's `user_email`, else `<os-user>@<hostname>`."""
    explicit = os.getenv("GGATE_USER") or os.getenv("GGATE_USER_EMAIL")
    if explicit:
        return explicit
    email = _read_config_scalar("user_email")
    if email:
        return email
    os_user = os.getenv("USER") or os.getenv("USERNAME")
    if not os_user:
        try:
            import getpass

            os_user = getpass.getuser()
        except Exception:  # noqa: BLE001 - no resolvable login; fall through to host-only
            os_user = None
    host = socket.gethostname()
    if os_user and host:
        return f"{os_user}@{host}"
    return os_user or host or None


@dataclass(frozen=True)
class Config:
    mode: str = field(default_factory=lambda: os.getenv("GGATE_MODE", "sync"))
    timeout: float = field(default_factory=_default_timeout)
    # Where scans go. Both are required; without them every scan fails open with a message
    # saying so (see `Client`), because a missing setting must never break the host app.
    console_url: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_CONSOLE_URL"))
    api_key: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_API_KEY"))
    queue_max: int = field(default_factory=lambda: _int_env("GGATE_QUEUE_MAX", 1024))
    enabled: bool = field(default_factory=lambda: not _bool_env("GGATE_DISABLED", False))
    org_id: str = field(
        default_factory=lambda: os.getenv("GGATE_ORG_ID") or _read_config_scalar("org_id") or "local"
    )
    user: Optional[str] = field(default_factory=_default_user)
    host: str = field(default_factory=socket.gethostname)
    workstation_id: str = field(
        default_factory=lambda: os.getenv("GGATE_WORKSTATION_ID")
        or _read_config_scalar("workstation_id")
        or str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()))
    )
    collector_id: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_COLLECTOR_ID"))
    labels: Dict[str, str] = field(default_factory=dict)
    # Off by default: the Console IS the detection engine, so masking client-side would hide
    # exactly the secrets it exists to catch. `GGATE_REDACT=1` / `redact=True` turns it on for
    # deployments that would rather lose those detections than let the content leave the process.
    redact: bool = field(default_factory=lambda: _bool_env("GGATE_REDACT", False))
    capture_file_text: bool = field(
        default_factory=lambda: _bool_env("GGATE_CAPTURE_FILE_TEXT", False)
    )
    max_file_bytes: int = field(default_factory=lambda: _int_env("GGATE_MAX_FILE_BYTES", 65536))
    # After a sync-scan failure, fail open instantly for this long instead of re-calling a
    # struggling Console on every request.
    cooldown: float = field(default_factory=lambda: max(0, _int_env("GGATE_COOLDOWN_SECS", 30)))
    # Bound on the atexit/default flush of the background queue.
    flush_timeout: float = field(
        default_factory=lambda: max(0, _int_env("GGATE_FLUSH_TIMEOUT_MS", 3000)) / 1000
    )

    def __post_init__(self):
        if self.collector_id is None:
            object.__setattr__(self, "collector_id", f"{self.workstation_id}:ggate-python-sdk")

    @property
    def configured(self) -> bool:
        """Whether a Console to scan against has been supplied."""
        return bool(self.console_url and self.api_key)

    @classmethod
    def from_values(cls, **kwargs) -> "Config":
        values = {k: v for k, v in kwargs.items() if v is not None}
        cfg = cls(**values)
        if cfg.mode.lower() not in {"sync", "async"}:
            raise ValueError("mode must be 'sync' or 'async'")
        object.__setattr__(cfg, "mode", cfg.mode.lower())
        return cfg
