from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


ASSERTION_STATES = {
    "observed",
    "inferred_candidate",
    "engineer_confirmed",
    "approved",
    "superseded",
    "rejected",
}
RISK_LEVELS = {"R0", "R1", "R2", "R3"}
SCOPE_KINDS = {"model", "product", "family", "design_group", "organization_general"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def require_safe_id(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value or ""):
        raise ValueError(f"{label} contains unsafe characters")
    return value


def finite_vector(values: list[Any], dimensions: int, label: str) -> list[float]:
    if len(values) != dimensions:
        raise ValueError(f"{label} must contain exactly {dimensions} values")
    result = [float(item) for item in values]
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} must contain only finite values")
    return result


@dataclass(frozen=True)
class ScanEntry:
    relative_path: str
    absolute_path: str
    family_folder: str
    sha256: str
    size_bytes: int
    modified_at_ns: int
    suffix: str


@dataclass(frozen=True)
class ScanChange:
    kind: str
    entry: ScanEntry | None
    previous_path: str | None = None
    reason: str = ""


@dataclass
class AssertionProposal:
    subject_ref: str
    predicate: str
    object_value: Any
    scope_kind: str
    risk_level: str
    status: str = "inferred_candidate"
    unit: str = ""
    confidence: float = 0.5
    evidence: list[dict[str, Any]] = field(default_factory=list)
    applicability: dict[str, Any] = field(default_factory=dict)
    non_applicable_conditions: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)
    supersedes: str = ""
    source_kind: str = "codex_interpretation"

    def validate(self) -> "AssertionProposal":
        if not self.subject_ref.strip() or not self.predicate.strip():
            raise ValueError("assertion subject_ref and predicate are required")
        if self.scope_kind not in SCOPE_KINDS:
            raise ValueError(f"unsupported scope_kind: {self.scope_kind}")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"unsupported risk_level: {self.risk_level}")
        if self.status not in ASSERTION_STATES:
            raise ValueError(f"unsupported assertion status: {self.status}")
        if self.status in {"approved", "superseded", "rejected"}:
            raise ValueError("new proposals cannot start in a terminal or approved state")
        allowed_scopes = {
            "R0": {"model"},
            "R1": {"model", "product"},
            "R2": {"model", "product", "family"},
            "R3": SCOPE_KINDS,
        }[self.risk_level]
        if self.scope_kind not in allowed_scopes:
            raise ValueError(
                f"{self.risk_level} knowledge cannot be proposed at {self.scope_kind} scope; "
                "broader promotion requires the corresponding higher-risk review"
            )
        self.confidence = float(self.confidence)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(self.evidence, list) or not self.evidence:
            raise ValueError("assertion evidence must contain at least one item")
        try:
            self.contradicts = [str(uuid.UUID(str(value))) for value in self.contradicts]
            self.supersedes = str(uuid.UUID(self.supersedes)) if self.supersedes else ""
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("contradicts and supersedes must contain valid assertion UUIDs") from exc
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DesignContext:
    schema_version: str
    authorization_basis: str
    hard_constraints: list[dict[str, Any]]
    preferences: list[dict[str, Any]]
    approved_facts: list[dict[str, Any]]
    specialized_knowledge: list[dict[str, Any]]
    approved_design_lessons: list[dict[str, Any]]
    combined_engineering_checks: list[dict[str, Any]]
    knowledge_conflicts: list[dict[str, Any]]
    lesson_match_explanations: list[dict[str, Any]]
    excluded_design_lessons: list[dict[str, Any]]
    automatic_application_blocked: bool
    graph_relationships: list[dict[str, Any]]
    similar_models: list[dict[str, Any]]
    generic_engineering_checks: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    excluded_specialized_knowledge: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
