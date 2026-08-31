from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping

from .approval_semantics import APPROVE, REJECT, classify_approval
from .models import canonical_json, require_safe_id
from .secure_fs import (
    atomic_publish_new,
    atomic_replace,
    ensure_managed_directory,
    exclusive_file_lock,
    read_managed_file,
    validate_managed_path,
)


_SCHEMA = "ProductFamilyOnboarding/v1"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc
    return copied


class ProductFamilyKnowledgeService:
    """Independent Product Family analysis, review, and publication."""

    def __init__(self, workspace: Path, repository: object) -> None:
        self.workspace = validate_managed_path(
            Path(workspace), allow_missing_leaf=False
        ).path
        self.repository = repository

    @property
    def root(self) -> Path:
        return ensure_managed_directory(
            self.workspace / "knowledge" / "product-families" / "onboarding",
            parents=True,
            exist_ok=True,
        ).path

    def start(self, request: Mapping[str, object]) -> dict[str, object]:
        copied = _json_object(request, "request")
        onboarding_id = require_safe_id(
            str(copied.get("onboarding_id", "")), "onboarding_id"
        )
        family_id = require_safe_id(str(copied.get("family_id", "")), "family_id")
        family_name = copied.get("family_name")
        if not isinstance(family_name, str) or not family_name.strip():
            raise ValueError("family_name is required")
        path = self.root / f"{onboarding_id}.json"
        with exclusive_file_lock(self.root / ".onboarding.lock"):
            if path.exists():
                existing = self._read(path)
                if existing["request"] != copied:
                    raise ValueError("onboarding_id belongs to a different request")
                return {**existing, "resumed": True}
            now = _timestamp()
            state = {
                "schema_version": _SCHEMA,
                "onboarding_id": onboarding_id,
                "family_id": family_id,
                "status": "started",
                "request": copied,
                "analysis": None,
                "review": None,
                "publication": None,
                "created_at": now,
                "updated_at": now,
            }
            atomic_publish_new(path, canonical_json(state).encode("utf-8"))
            return {**state, "resumed": False}

    def analyze(
        self, onboarding_id: str, analysis: Mapping[str, object]
    ) -> dict[str, object]:
        path = self._path(onboarding_id)
        copied = _json_object(analysis, "analysis")
        assertions = copied.get("assertions")
        if not isinstance(assertions, list):
            raise ValueError("analysis.assertions must be a list")
        with exclusive_file_lock(self.root / ".onboarding.lock"):
            state = self._read(path)
            if state["status"] == "published":
                raise ValueError("published Product Family Knowledge is immutable")
            state["analysis"] = copied
            state["review"] = None
            state["status"] = "analyzed"
            state["updated_at"] = _timestamp()
            atomic_replace(path, canonical_json(state).encode("utf-8"))
            return state

    def review(
        self,
        onboarding_id: str,
        decision_text: str,
        review: Mapping[str, object],
    ) -> dict[str, object]:
        decision = classify_approval(decision_text)
        path = self._path(onboarding_id)
        if decision not in {APPROVE, REJECT}:
            return {
                "schema_version": "ProductFamilyReviewResult/v1",
                "onboarding_id": onboarding_id,
                "decision_state": decision,
                "status": "not_reviewed",
                "next_action": "clarify_review_decision",
            }
        copied = _json_object(review, "review")
        with exclusive_file_lock(self.root / ".onboarding.lock"):
            state = self._read(path)
            if state["analysis"] is None:
                raise ValueError("Product Family Knowledge must be analyzed first")
            state["review"] = {
                "state": decision,
                "text": decision_text.strip(),
                "notes": copied,
                "reviewed_at": _timestamp(),
            }
            state["status"] = "approved" if decision == APPROVE else "rejected"
            state["updated_at"] = _timestamp()
            atomic_replace(path, canonical_json(state).encode("utf-8"))
            return state

    def publish(self, onboarding_id: str) -> dict[str, object]:
        path = self._path(onboarding_id)
        with exclusive_file_lock(self.root / ".onboarding.lock"):
            state = self._read(path)
            if state["status"] == "published":
                return {**state["publication"], "resumed": True}
            if state["status"] != "approved":
                raise ValueError("Product Family Knowledge requires approval before publication")
            publish = getattr(self.repository, "publish_product_family", None)
            if not callable(publish):
                raise ValueError("knowledge repository cannot publish Product Families")
            result = publish(
                family_id=state["family_id"],
                family_name=state["request"]["family_name"],
                aliases=state["request"].get("aliases", []),
                knowledge=state["analysis"],
                decision_text=state["review"]["text"],
            )
            state["publication"] = _json_object(result, "publication result")
            state["status"] = "published"
            state["updated_at"] = _timestamp()
            atomic_replace(path, canonical_json(state).encode("utf-8"))
            return state["publication"]

    def status(self, onboarding_id: str) -> dict[str, object]:
        return self._read(self._path(onboarding_id))

    def _path(self, onboarding_id: str) -> Path:
        normalized = require_safe_id(onboarding_id, "onboarding_id")
        path = self.root / f"{normalized}.json"
        if not path.is_file():
            raise ValueError(f"unknown Product Family onboarding: {normalized}")
        return path

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            state = json.loads(read_managed_file(path).content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Product Family onboarding state is invalid") from exc
        if not isinstance(state, dict) or state.get("schema_version") != _SCHEMA:
            raise ValueError("Product Family onboarding state is incompatible")
        return state


__all__ = ["ProductFamilyKnowledgeService"]
