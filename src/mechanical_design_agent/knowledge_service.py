from __future__ import annotations

from typing import Any, Mapping

from .approval_semantics import APPROVE, classify_approval
from .product_family_knowledge import ProductFamilyKnowledgeService


class KnowledgeService:
    """Application service for durable engineering knowledge administration."""

    def __init__(self, repository: object, projection: object, workspace: object) -> None:
        self.repository = repository
        self.projection = projection
        self.families = ProductFamilyKnowledgeService(workspace, repository)

    def product_family_onboarding_start(self, **request: object) -> dict[str, object]:
        return self.families.start(request)

    def product_family_onboarding_analyze(
        self, *, onboarding_id: str, analysis: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        return self.families.analyze(onboarding_id, analysis or {"assertions": []})

    def product_family_onboarding_review(
        self,
        *,
        onboarding_id: str,
        decision_text: str,
        review: Mapping[str, object],
    ) -> dict[str, object]:
        return self.families.review(onboarding_id, decision_text, review)

    def product_family_onboarding_publish(
        self, *, onboarding_id: str
    ) -> dict[str, object]:
        return self.families.publish(onboarding_id)

    def product_family_onboarding_status(
        self, *, onboarding_id: str
    ) -> dict[str, object]:
        return self.families.status(onboarding_id)

    def design_context_build(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        requested_family_id: object,
        design_features: Mapping[str, object],
        lesson_query: str,
    ) -> dict[str, object]:
        del organization_id, design_group_id, design_features
        result = self.repository.search(
            query=lesson_query,
            product_family_id=(
                str(requested_family_id) if requested_family_id else None
            ),
        )
        return {
            "schema_version": "DesignContext/v2",
            "hard_constraints": [],
            "preferences": [],
            "approved_facts": [],
            "specialized_knowledge": result["families"],
            "approved_design_lessons": result["lessons"],
            "similar_models": [],
        }

    def knowledge_search(
        self, *, query: str, filters: Mapping[str, object]
    ) -> dict[str, object]:
        return self.repository.search(
            query=query,
            product_family_id=(
                str(filters["product_family_id"])
                if filters.get("product_family_id")
                else None
            ),
            limit=int(filters.get("limit", 20)),
        )

    def design_lesson_search(
        self, *, query: str, features: Mapping[str, object], limit: int
    ) -> dict[str, object]:
        return self.repository.search(
            query=query,
            product_family_id=(
                str(features["product_family_id"])
                if features.get("product_family_id")
                else None
            ),
            limit=limit,
        )

    def design_lesson_get(self, *, lesson_id: str) -> dict[str, object]:
        return self.repository.get_design_lesson(lesson_id)

    def design_lesson_supersede(
        self,
        *,
        lesson_id: str,
        replacement_lesson_id: str,
        decision_text: str,
    ) -> dict[str, object]:
        decision = classify_approval(decision_text)
        if decision != APPROVE:
            return {
                "decision_state": decision,
                "status": "not_changed",
            }
        return self.repository.set_design_lesson_status(
            lesson_id=lesson_id,
            status="superseded",
            replacement_lesson_id=replacement_lesson_id,
        )

    def design_lesson_revoke(
        self, *, lesson_id: str, decision_text: str
    ) -> dict[str, object]:
        decision = classify_approval(decision_text)
        if decision != APPROVE:
            return {
                "decision_state": decision,
                "status": "not_changed",
            }
        return self.repository.set_design_lesson_status(
            lesson_id=lesson_id, status="revoked"
        )

    def publish_design_lesson_review(self, **kwargs: object) -> dict[str, object]:
        return self.repository.publish_design_lesson_review(**kwargs)

    def projection_sync(self, *, limit: int = 100) -> dict[str, object]:
        return self.projection.sync(self.repository, limit=limit)

    def projection_rebuild(self, *, decision_text: str) -> dict[str, object]:
        if classify_approval(decision_text) != APPROVE:
            return {"status": "not_rebuilt", "decision_state": classify_approval(decision_text)}
        return self.projection.rebuild(self.repository)


__all__ = ["KnowledgeService"]
