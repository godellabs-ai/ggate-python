"""Decision model returned by the Console.

Mirrors the Rust ``Decision``: a verdict, a human-readable message, and — for warn/block — a
``detection`` headline naming which protection fired
(``{"source": ..., "detail": ..., "other_sources": N}``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class TopicClassification:
    label: str
    confidence: float
    runner_up: Optional[str] = None
    runner_up_confidence: Optional[float] = None


@dataclass(frozen=True)
class DocumentTaxonomy:
    topic: TopicClassification
    overall_confidence: float
    content_types: List[str] = field(default_factory=list)
    business_domains: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SensitivityClassification:
    severity: str
    sensitive_data_classes: List[str] = field(default_factory=list)
    business_confidentiality_classes: List[str] = field(default_factory=list)
    privacy_classes: List[str] = field(default_factory=list)
    rights_and_restrictions: List[str] = field(default_factory=list)
    required_handling: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DocumentIntelligence:
    """Non-enforcement topic and sensitivity output returned beside a scan decision."""

    taxonomy: DocumentTaxonomy
    sensitivity: SensitivityClassification
    matched_text: Optional[str] = None

    @classmethod
    def from_json(cls, value: Dict[str, Any]) -> "DocumentIntelligence":
        taxonomy = value.get("taxonomy") or {}
        topic = taxonomy.get("topic") or {}
        sensitivity = value.get("sensitivity") or {}
        return cls(
            taxonomy=DocumentTaxonomy(
                topic=TopicClassification(
                    label=str(topic.get("label") or ""),
                    confidence=float(topic.get("confidence") or 0.0),
                    runner_up=topic.get("runner_up"),
                    runner_up_confidence=(
                        float(topic["runner_up_confidence"])
                        if topic.get("runner_up_confidence") is not None
                        else None
                    ),
                ),
                overall_confidence=float(taxonomy.get("overall_confidence") or 0.0),
                content_types=list(taxonomy.get("content_types") or []),
                business_domains=list(taxonomy.get("business_domains") or []),
            ),
            sensitivity=SensitivityClassification(
                severity=str(sensitivity.get("severity") or "info"),
                sensitive_data_classes=list(sensitivity.get("sensitive_data_classes") or []),
                business_confidentiality_classes=list(
                    sensitivity.get("business_confidentiality_classes") or []
                ),
                privacy_classes=list(sensitivity.get("privacy_classes") or []),
                rights_and_restrictions=list(sensitivity.get("rights_and_restrictions") or []),
                required_handling=list(sensitivity.get("required_handling") or []),
            ),
            matched_text=value.get("matched_text"),
        )

    def to_json(self) -> Dict[str, Any]:
        topic = {
            "label": self.taxonomy.topic.label,
            "confidence": self.taxonomy.topic.confidence,
        }
        if self.taxonomy.topic.runner_up is not None:
            topic["runner_up"] = self.taxonomy.topic.runner_up
        if self.taxonomy.topic.runner_up_confidence is not None:
            topic["runner_up_confidence"] = self.taxonomy.topic.runner_up_confidence
        value: Dict[str, Any] = {
            "taxonomy": {
                "topic": topic,
                "content_types": self.taxonomy.content_types,
                "business_domains": self.taxonomy.business_domains,
                "overall_confidence": self.taxonomy.overall_confidence,
            },
            "sensitivity": {
                "severity": self.sensitivity.severity,
                "sensitive_data_classes": self.sensitivity.sensitive_data_classes,
                "business_confidentiality_classes": self.sensitivity.business_confidentiality_classes,
                "privacy_classes": self.sensitivity.privacy_classes,
                "rights_and_restrictions": self.sensitivity.rights_and_restrictions,
                "required_handling": self.sensitivity.required_handling,
            },
        }
        if self.matched_text is not None:
            value["matched_text"] = self.matched_text
        return value


@dataclass(frozen=True)
class Decision:
    verdict: str = "pass"
    message: str = "allowed"
    reason_codes: List[str] = field(default_factory=list)
    matched_detectors: List[Dict[str, Any]] = field(default_factory=list)
    policy_source: Optional[Dict[str, Any]] = None
    detection: Optional[Dict[str, Any]] = None
    document_intelligence: Optional[DocumentIntelligence] = None
    fail_open: bool = False

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    @property
    def hard_blocked(self) -> bool:
        """Deprecated compatibility alias; all current blocks are final/non-circumventable."""
        return self.blocked

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
        raw_verdict = str(value.get("verdict", "pass"))
        # Older Consoles emitted `hard_block`; preserve enforcement while exposing the current
        # unified `block` contract to callers.
        verdict = "block" if raw_verdict == "hard_block" else raw_verdict
        if verdict not in {"system", "pass", "warn", "block"}:
            return cls.fail_open_decision(f"unexpected scan verdict {raw_verdict!r}")
        intelligence = value.get("document_intelligence")
        return cls(
            verdict=verdict,
            message=str(value.get("message", "allowed")),
            reason_codes=list(value.get("reason_codes") or []),
            matched_detectors=list(value.get("matched_detectors") or []),
            policy_source=value.get("policy_source"),
            detection=value.get("detection"),
            document_intelligence=(
                DocumentIntelligence.from_json(intelligence)
                if isinstance(intelligence, dict)
                else None
            ),
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
        if self.document_intelligence is not None:
            data["document_intelligence"] = self.document_intelligence.to_json()
        return data
