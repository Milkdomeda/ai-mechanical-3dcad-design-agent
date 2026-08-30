from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from .lightweight_design import LightweightDesignService


ContextLoader = Callable[[str, dict[str, object]], Mapping[str, object]]

_CONTEXT_COLLECTIONS = (
    "hard_constraints",
    "preferences",
    "approved_facts",
    "specialized_knowledge",
    "approved_design_lessons",
    "similar_models",
)
_IDENTITY_FIELDS = (
    "assertion_id",
    "design_lesson_ref",
    "model_revision_id",
    "knowledge_id",
    "id",
)


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{label} must be a JSON object")
    return copied


def _knowledge_ids(context: Mapping[str, object]) -> list[str]:
    found: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for field in _IDENTITY_FIELDS:
                candidate = value.get(field)
                if (
                    isinstance(candidate, str)
                    and candidate.strip()
                    and candidate not in found
                ):
                    found.append(candidate)
                    break
            assertions = value.get("assertions")
            if isinstance(assertions, list):
                for assertion in assertions:
                    visit(assertion)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for collection in _CONTEXT_COLLECTIONS:
        visit(context.get(collection, []))
    return found


class LightweightKnowledgeService:
    """Best-effort knowledge retrieval without introducing a lifecycle gate."""

    def __init__(
        self,
        sessions: LightweightDesignService,
        context_loader: ContextLoader,
    ) -> None:
        self.sessions = sessions
        self.context_loader = context_loader

    def retrieve(
        self,
        *,
        design_id: str,
        query: str,
        features: Mapping[str, object],
        used_ids: Sequence[str] = (),
        required: bool = False,
    ) -> dict[str, object]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonblank string")
        feature_copy = _json_object(features, "features")
        selected: list[str] = []
        for value in used_ids:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("used_ids must contain nonblank strings")
            if value not in selected:
                selected.append(value)
        if not isinstance(required, bool):
            raise ValueError("required must be a boolean")

        try:
            loaded = self.context_loader(query.strip(), feature_copy)
            context = _json_object(loaded, "knowledge context")
        except Exception as exc:
            warning = f"knowledge backend unavailable ({type(exc).__name__})"
            self.sessions.record_knowledge(
                design_id=design_id,
                status="unavailable",
                used_ids=[],
                warning=warning,
            )
            return {
                "schema_version": "LightweightKnowledgeResult/v1",
                "status": "unavailable",
                "blocking": required,
                "available_ids": [],
                "used_ids": [],
                "context": None,
                "warning": warning,
            }

        available = _knowledge_ids(context)
        unknown = [value for value in selected if value not in available]
        if unknown:
            raise ValueError(
                "used knowledge IDs are not present in the current context: "
                + ", ".join(unknown)
            )

        status = "completed_matches" if available else "completed_no_match"
        self.sessions.record_knowledge(
            design_id=design_id,
            status=status,
            used_ids=selected,
            warning=None,
        )
        return {
            "schema_version": "LightweightKnowledgeResult/v1",
            "status": status,
            "blocking": bool(required and not available),
            "available_ids": available,
            "used_ids": selected,
            "context": context,
            "warning": None,
        }


__all__ = ["LightweightKnowledgeService"]
