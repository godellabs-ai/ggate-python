"""Configuration and identity resolution.

The SDK is a per-user collector: identity (org, seat user, workstation) travels with
each event and the agent forwards events without re-attributing them. Defaults come
from, in order: explicit ``init(...)`` arguments, ``GGATE_*`` environment variables,
the per-user ``~/.ggate/config.yaml`` the agent maintains, then built-in fallbacks —
so an app on a machine with an installed agent needs zero configuration.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


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


def _ggate_home() -> Path:
    return Path(os.getenv("GGATE_HOME", "~/.ggate")).expanduser()


def _config_path() -> Path:
    return Path(os.getenv("GGATE_CONFIG") or _ggate_home() / "config.yaml").expanduser()


def _read_config_scalar(name: str) -> Optional[str]:
    """One top-level scalar from the agent's per-user config.yaml (line-oriented, no YAML dep)."""
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


def _agent_json_path() -> Path:
    """The agent discovery file: `GGATE_AGENT_JSON`, else the config.yaml
    `agent_endpoint_file` (shared-agent VMs publish it outside any one home), else
    `~/.ggate/agent.json` — matching the Rust `resolved_agent_endpoint_file`."""
    explicit = os.getenv("GGATE_AGENT_JSON") or _read_config_scalar("agent_endpoint_file")
    if explicit:
        return Path(explicit).expanduser()
    return _ggate_home() / "agent.json"


def _default_timeout() -> float:
    """Scan budget in seconds. `GGATE_TIMEOUT_MS`, else the agent config's
    `model_timeout_ms` (the same gating budget the Rust hook collector uses), else 4s.
    This is a worst-case ceiling for a slow/cold agent, not a per-call latency: a warm
    agent answers in milliseconds and a dead one refuses the connection instantly."""
    raw = os.getenv("GGATE_TIMEOUT_MS") or _read_config_scalar("model_timeout_ms")
    try:
        return max(1, int(raw)) / 1000 if raw else 4.0
    except ValueError:
        return 4.0


def _default_user() -> Optional[str]:
    """Seat identity, matching the Rust collectors' `seat_user`: explicit GGATE_USER /
    GGATE_USER_EMAIL, else the per-user config's `user_email`, else `<os-user>@<hostname>`."""
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
    agent_json: Path = field(default_factory=_agent_json_path)
    tls_dir: Path = field(default_factory=lambda: _ggate_home() / "tls")
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
    # Console mode: scan via the Console HTTP API (x-api-key) instead of a local agent.
    console_url: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_CONSOLE_URL"))
    api_key: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_API_KEY"))
    labels: Dict[str, str] = field(default_factory=dict)
    # Agent mode redacts before anything leaves the process (the local engine has already seen
    # the raw text). Console mode must send raw content — the Console IS the detection engine.
    # An explicit GGATE_REDACT / redact= always wins; the console-mode default is resolved in
    # __post_init__ because it depends on console_url/api_key.
    redact: Optional[bool] = field(
        default_factory=lambda: None if os.getenv("GGATE_REDACT") is None else _bool_env("GGATE_REDACT", True)
    )
    capture_file_text: bool = field(
        default_factory=lambda: _bool_env("GGATE_CAPTURE_FILE_TEXT", False)
    )
    max_file_bytes: int = field(default_factory=lambda: _int_env("GGATE_MAX_FILE_BYTES", 65536))
    # After a sync-scan transport failure, fail open instantly for this long instead of
    # re-dialing a struggling agent on every call.
    cooldown: float = field(default_factory=lambda: max(0, _int_env("GGATE_COOLDOWN_SECS", 30)))
    # Bound on the atexit/default flush of the background queue.
    flush_timeout: float = field(
        default_factory=lambda: max(0, _int_env("GGATE_FLUSH_TIMEOUT_MS", 3000)) / 1000
    )

    def __post_init__(self):
        if self.collector_id is None:
            object.__setattr__(self, "collector_id", f"{self.workstation_id}:ggate-python-sdk")
        if self.redact is None:
            object.__setattr__(self, "redact", not (self.console_url and self.api_key))

    @classmethod
    def from_values(cls, **kwargs) -> "Config":
        values = {k: v for k, v in kwargs.items() if v is not None}
        cfg = cls(**values)
        if cfg.mode.lower() not in {"sync", "async"}:
            raise ValueError("mode must be 'sync' or 'async'")
        object.__setattr__(cfg, "mode", cfg.mode.lower())
        return cfg
