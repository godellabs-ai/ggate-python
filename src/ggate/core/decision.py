"""Decision model returned by the Console.

Mirrors the Rust ``Decision``: a verdict, a human-readable message, and — for warn/block — a
``detection`` headline naming which protection fired
(``{"source": ..., "detail": ..., "other_sources": N}``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Decision:
    verdict: str = "pass"
    message: str = "allowed"
    reason_codes: List[str] = field(default_factory=list)
    matched_detectors: List[Dict[str, Any]] = field(default_factory=list)
    policy_source: Dict[str, Any] = field(default_factory=lambda: {"kind": "none"})
    detection: Optional[Dict[str, Any]] = None
    fail_open: bool = False

    @property
    def blocked(self) -> bool:
        return self.verdict in {"block", "hard_block"}

    @property
    def hard_blocked(self) -> bool:
        return self.verdict == "hard_block"

    @property
    def allowed(self) -> bool:
        return self.verdict in {"pass", "warn", "system"}

    @property
    def warned(self) -> bool:
        return self.verdict == "warn"

    @classmethod
    def pass_(cls) -> "Decision":
        return cls()

    @classmethod
    def fail_open_decision(cls, message: str) -> "Decision":
        return cls(
            verdict="pass",
            message=message,
            reason_codes=["engine_unreachable"],
            fail_open=True,
        )

    @classmethod
    def from_response(cls, value: Dict[str, Any]) -> "Decision":
        """Parse a scan response body. The Console answers with the decision itself; an
        explicit `{"kind": "error", ...}` — or any shape this SDK does not recognize — fails
        open rather than being reported as a verdict nobody reached."""
        kind = value.get("kind")
        if kind == "error":
            return cls.fail_open_decision(value.get("message") or "console returned an error")
        if kind not in (None, "verdict"):
            return cls.fail_open_decision(f"unexpected scan response kind {kind!r}")
        return cls(
            verdict=str(value.get("verdict", "pass")),
            message=str(value.get("message", "allowed")),
            reason_codes=list(value.get("reason_codes") or []),
            matched_detectors=list(value.get("matched_detectors") or []),
            policy_source=value.get("policy_source") or {"kind": "none"},
            detection=value.get("detection"),
            fail_open=bool(value.get("fail_open", False)),
        )

    def to_json(self) -> Dict[str, Any]:
        data = {
            "verdict": self.verdict,
            "reason_codes": self.reason_codes,
            "message": self.message,
            "matched_detectors": self.matched_detectors,
            "policy_source": self.policy_source,
            "fail_open": self.fail_open,
        }
        if self.detection is not None:
            data["detection"] = self.detection
        return data
