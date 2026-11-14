"""Configuration and identity resolution.

Console URL, credentials and identity can come from the local collector config. Explicit
credential providers, named environment variables and mounted secret files support rotation.
The SDK connects directly to the Console; it does not call a local Detection Engine.
"""

from __future__ import annotations

import os
import json
import re
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, NoReturn, Optional

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
            if raw_line[:1].isspace() or ":" not in raw_line:
                continue
            key, value = raw_line.split(":", 1)
            if key != name:
                continue
            value = value.strip()
            if value.startswith('"'):
                parsed, end = json.JSONDecoder().raw_decode(value)
                if not isinstance(parsed, str) or (value[end:].strip() and not value[end:].lstrip().startswith("#")):
                    return None
                return parsed.strip() or None
            if value.startswith("'"):
                match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
                return match[1].replace("''", "'").strip() or None if match else None
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            return None if value in {"", "null", "Null", "NULL", "~"} else value
    except (OSError, ValueError):
        return None
    return None


def _default_timeout() -> float:
    """Scan budget in seconds: ``GGATE_TIMEOUT_MS``, else :data:`DEFAULT_TIMEOUT`."""
    raw = os.getenv("GGATE_TIMEOUT_MS")
    try:
        return max(1, int(raw)) / 1000 if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def _required(name: str, env_var: str, example: str) -> NoReturn:
    """Refuse to build a Config without an identity the SDK cannot infer.

    Unlike a missing Console — which degrades to fail-open scans, so the host application keeps
    working — an unnamed agent is a permanent labelling error in the event store: every event the
    process ever writes lands under an anonymous agent, which no later configuration can repair.
    So it is caught at construction instead.
    """
    raise ValueError(
        f"ggate: {name} is required. Pass {name}={example!r} to init(), or set {env_var}."
    )


def _non_blank(value: Optional[str]) -> Optional[str]:
    """The value with surrounding whitespace stripped, or None — so blanks never count."""
    text = (value or "").strip()
    return text or None


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
    #: What this agent is, as an operator would name it — ``"JIRA Project Assistant"``, not the
    #: framework it is built on. Required (``init(agent_name=...)`` or ``GGATE_AGENT_NAME``): it
    #: travels as ``identity.agent_name`` and is what the Console's Session column shows, so two
    #: assistants built on the same framework stay distinguishable.
    agent_name: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_AGENT_NAME"))
    #: The team or owner accountable for this agent — ``"Platform Engineering"``. Required
    #: (``init(team=...)`` or ``GGATE_TEAM``).
    #:
    #: A deployed agent has no person at a keyboard, so :attr:`user` falls back to the build
    #: machine's ``<os-user>@<hostname>`` — whoever ran the deploy, not whoever owns the workload.
    #: The seat identity is still reported unchanged (the Console scopes event access by it); this
    #: is the name shown beside it.
    team: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_TEAM"))
    mode: str = field(default_factory=lambda: os.getenv("GGATE_MODE", "sync"))
    timeout: float = field(default_factory=_default_timeout)
    # Where scans go. Both are required; without them every scan fails open with a message
    # saying so (see `Client`), because a missing setting must never break the host app.
    console_url: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_CONSOLE_URL") or _read_config_scalar("api_url"))
    cloud_instance_metadata: bool = field(default_factory=lambda: _bool_env("GGATE_CLOUD_INSTANCE_METADATA", _read_config_scalar("cloud_instance_metadata_enabled") == "true"))
    workload_identity: Dict[str, str] = field(default_factory=dict)
    api_key: Optional[str] = None
    api_key_env: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_API_KEY_ENV") or _read_config_scalar("api_key_env"))
    api_key_file: Optional[str] = field(default_factory=lambda: os.getenv("GGATE_API_KEY_FILE"))
    api_key_provider: Optional[Callable[[], Optional[str]]] = None
    # PEM CA certificate used to verify a Console signed by a private CA. Standard public CA trust
    # remains the default; TLS verification is never disabled by this SDK.
    console_ca_cert: Optional[str] = field(
        default_factory=lambda: os.getenv("GGATE_CONSOLE_CA_CERT")
    )
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
        if self.console_ca_cert:
            object.__setattr__(self, "console_ca_cert", str(Path(self.console_ca_cert).expanduser()))
        object.__setattr__(
            self,
            "agent_name",
            _non_blank(self.agent_name)
            or _required("agent_name", "GGATE_AGENT_NAME", "JIRA Project Assistant"),
        )
        object.__setattr__(
            self,
            "team",
            _non_blank(self.team) or _required("team", "GGATE_TEAM", "Platform Engineering"),
        )

    def resolve_api_key(self) -> Optional[str]:
        if self.api_key_provider is not None:
            return self.api_key_provider()
        if self.api_key_env:
            return _non_blank(os.getenv(self.api_key_env))
        if self.api_key_file:
            try:
                return _non_blank(Path(self.api_key_file).read_text().strip())
            except OSError:
                return None
        return self.api_key or _read_config_scalar("api_key") or os.getenv("GGATE_API_KEY")

    @property
    def configured(self) -> bool:
        """Whether a Console to scan against has been supplied."""
        return bool(self.console_url and self.resolve_api_key())

    @classmethod
    def from_values(cls, **kwargs) -> "Config":
        values = {k: v for k, v in kwargs.items() if v is not None}
        cfg = cls(**values)
        if cfg.mode.lower() not in {"sync", "async"}:
            raise ValueError("mode must be 'sync' or 'async'")
        object.__setattr__(cfg, "mode", cfg.mode.lower())
        return cfg
