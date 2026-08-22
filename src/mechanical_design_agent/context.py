from __future__ import annotations

import hashlib
from typing import Any

from .design_lessons import (
    CONSTRAINT_KINDS,
    PUBLISHED_SOURCE,
    condition_expression_satisfied,
    match_design_lesson,
)
from .models import DesignContext


GENERIC_CHECKS = [
    {"id": "source-hash", "description": "Preserve source CAD hash and provenance."},
    {"id": "unit-consistency", "description": "Resolve units before dimension or calculation use."},
    {"id": "geometry-validity", "description": "Check null shapes, solids, topology, and non-finite dimensions."},
    {"id": "interface-and-interference", "description": "Validate intended interfaces and unintended interference."},
    {"id": "standard-part-provenance", "description": "Resolve catalog components before custom standard-part geometry."},
]
LESSON_SOURCE_REDACTION_REASON = (
    "source incident details require explicit authorization for the lesson source family"
)
DESIGN_LESSON_CANDIDATE_PAGE_SIZE = 50
DESIGN_LESSON_MAX_CANDIDATES = 500
DESIGN_LESSON_MAX_INCLUDED = 50
DESIGN_LESSON_MAX_EXCLUDED = 50


class DesignContextBuilder:
    def __init__(self, repository: Any, graph_projection: Any | None = None):
        self.repository = repository
        self.graph_projection = graph_projection

    def build(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        requested_family_id: str | None = None,
        model_revision_id: str | None = None,
        explicit_family_authorization: bool = False,
        confirmed_in_current_session: bool = False,
        user_requested_analogy: bool = False,
        design_features: dict[str, Any] | None = None,
        lesson_query: str = "",
    ) -> dict[str, Any]:
        design_group = self.repository.get_design_group(design_group_id)
        if str(design_group["organization_id"]) != organization_id:
            raise ValueError("design group does not belong to the requested organization")
        model = self.repository.get_model_analysis(model_revision_id) if model_revision_id else None
        if model and (
            model["organization_id"] != organization_id or model["design_group_id"] != design_group_id
        ):
            raise ValueError("model does not belong to the requested organization/design group")
        model_family = model.get("family_id") if model else None
        authorized_family: str | None = None
        basis = "no-specialized-family-authority"
        if model_family:
            if requested_family_id and requested_family_id != model_family:
                raise ValueError("requested family conflicts with the confirmed source model family")
            authorized_family = str(model_family)
            basis = "confirmed-source-model-family"
        elif requested_family_id and (explicit_family_authorization or confirmed_in_current_session):
            family = self.repository.get_family(requested_family_id)
            if family["organization_id"] != organization_id or family["design_group_id"] != design_group_id:
                raise ValueError("requested family does not belong to the requested organization/design group")
            authorized_family = requested_family_id
            basis = "explicit-current-session-family-authorization"

        assertions = self.repository.approved_assertions(
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=authorized_family,
            model_revision_id=model_revision_id,
            include_design_lessons=False,
        )
        hard: list[dict[str, Any]] = []
        preferences: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        specialized: list[dict[str, Any]] = []
        combined_checks = list(GENERIC_CHECKS)
        accepted_assertions: list[dict[str, Any]] = []
        for requirement in self._structured_user_requirements(design_features or {}):
            if not self._assertion_is_effective(requirement, design_features or {}):
                continue
            rendered = self._render_assertion(requirement)
            self._classify_assertion(
                requirement,
                rendered,
                hard=hard,
                preferences=preferences,
                facts=facts,
                combined_checks=combined_checks,
            )
            accepted_assertions.append(requirement)

        for item in assertions:
            if item.get("source_kind") == PUBLISHED_SOURCE:
                continue
            if not self._assertion_scope_is_authorized(
                item,
                authorized_family=authorized_family,
                model_revision_id=model_revision_id,
            ):
                continue
            rendered = self._render_assertion(item)
            if item["scope_kind"] in {"product", "family", "design_group"}:
                specialized.append(rendered)
            self._classify_assertion(
                item,
                rendered,
                hard=hard,
                preferences=preferences,
                facts=facts,
                combined_checks=combined_checks,
            )
            accepted_assertions.append(item)

        approved_lessons: list[dict[str, Any]] = []
        match_explanations: list[dict[str, Any]] = []
        excluded_lessons: list[dict[str, Any]] = []
        lesson_page_search = getattr(
            self.repository,
            "search_approved_design_lesson_page",
            None,
        )
        lesson_search = getattr(self.repository, "search_approved_design_lessons", None)

        def lesson_candidates():
            if lesson_page_search is None:
                if lesson_search is not None:
                    yield from lesson_search(
                        organization_id=organization_id,
                        query=lesson_query,
                        limit=DESIGN_LESSON_CANDIDATE_PAGE_SIZE,
                    )
                return
            cursor: str | None = None
            scanned = 0
            while scanned < DESIGN_LESSON_MAX_CANDIDATES:
                page_size = min(
                    DESIGN_LESSON_CANDIDATE_PAGE_SIZE,
                    DESIGN_LESSON_MAX_CANDIDATES - scanned,
                )
                page = lesson_page_search(
                    organization_id=organization_id,
                    query=lesson_query,
                    page_size=page_size,
                    cursor=cursor,
                )
                lessons = list(page.get("items", []))
                scanned += len(lessons)
                yield from lessons
                next_cursor = page.get("next_cursor")
                if not next_cursor or next_cursor == cursor or not lessons:
                    return
                cursor = str(next_cursor)

        for lesson in lesson_candidates():
            if len(approved_lessons) >= DESIGN_LESSON_MAX_INCLUDED:
                break
            # Search already enforces this in PostgreSQL, but retaining the status
            # guard prevents stale or alternate repository implementations leaking
            # revoked/superseded lessons into a context.
            if lesson.get("status", "approved") != "approved":
                continue
            source_family_authorized = bool(
                authorized_family
                and str(lesson.get("source_family_id") or "") == authorized_family
            )
            design_lesson_ref = self._opaque_lesson_ref(lesson)
            match = match_design_lesson(lesson, design_features or {}, lesson_query)
            explanation = self._render_lesson_match(
                lesson,
                match,
                source_family_authorized=source_family_authorized,
                design_lesson_ref=design_lesson_ref,
            )
            if not match["eligible"]:
                if len(excluded_lessons) < DESIGN_LESSON_MAX_EXCLUDED:
                    excluded_lessons.append({
                        **explanation,
                        "reason": match["exclusion_reasons"][0],
                        "reasons": match["exclusion_reasons"],
                    })
                continue
            match_explanations.append(explanation)
            approved_lessons.append(self._render_lesson(
                lesson,
                source_family_authorized=source_family_authorized,
                design_lesson_ref=design_lesson_ref,
            ))
            for assertion in lesson.get("assertions", []):
                if assertion.get("status", "approved") != "approved":
                    continue
                contextual_assertion = {
                    **assertion,
                    "_lesson_source_family_id": lesson.get("source_family_id"),
                    "_redact_source_details": not source_family_authorized,
                    "_design_lesson_ref": design_lesson_ref,
                }
                rendered = self._render_assertion(contextual_assertion)
                self._classify_assertion(
                    contextual_assertion,
                    rendered,
                    hard=hard,
                    preferences=preferences,
                    facts=facts,
                    combined_checks=combined_checks,
                )
                accepted_assertions.append(contextual_assertion)

        conflicts = self._knowledge_conflicts(
            accepted_assertions,
            design_features=design_features or {},
        )
        automatic_application_blocked = any(
            conflict["automatic_application_blocked"] for conflict in conflicts
        )
        if authorized_family:
            profile = self.repository.approved_family_profile(authorized_family)
            if profile:
                rendered_profile = {
                    "profile_id": str(profile["id"]),
                    "kind": "approved-family-profile",
                    "scope_kind": "family",
                    "family_id": profile["family_id"],
                    "profile": profile["profile"],
                    "evidence": profile["evidence"],
                    "distinct_model_count": profile["distinct_model_count"],
                    "note": "Family profile facts are not hard constraints unless represented by separately approved R3 assertions.",
                }
                facts.append(rendered_profile)
                specialized.append(rendered_profile)

        similar = []
        if authorized_family and model_revision_id:
            similar = self.repository.similar_models(authorized_family, model_revision_id)
        elif user_requested_analogy:
            # Cross-family analogy candidates require a separate explicit tool in a later phase.
            # Returning none is safer than treating similarity as authorization.
            similar = []

        graph_relationships = []
        if self.graph_projection is not None and (authorized_family or model_revision_id):
            try:
                graph_relationships = self.graph_projection.scoped_relationships(
                    family_id=authorized_family,
                    model_revision_id=model_revision_id,
                )
            except Exception as exc:
                graph_relationships = [
                    {
                        "status": "unavailable",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "authoritative_fallback": "postgresql",
                    }
                ]

        excluded_rows = self.repository.excluded_specialized_count(
            organization_id, design_group_id, authorized_family
        )
        excluded_counts: dict[str, int] = {}
        for item in excluded_rows:
            scope_kind = str(item.get("scope_kind", "specialized"))
            excluded_counts[scope_kind] = excluded_counts.get(scope_kind, 0) + int(
                item.get("count", 0)
            )
        excluded = [
            {
                "family_id": None,
                "scope_kind": scope_kind,
                "count": count,
                "reason": "specialized knowledge scope is not authorized; family identity was omitted",
            }
            for scope_kind, count in sorted(excluded_counts.items())
        ]
        if authorized_family is None:
            excluded.append(
                {
                    "family_id": None,
                    "scope_kind": "all-specialized",
                    "reason": "no family was explicitly authorized; specialized terminology and rules were intentionally omitted",
                }
            )
        context = DesignContext(
            schema_version="DesignContext/v2",
            authorization_basis=basis,
            hard_constraints=hard,
            preferences=preferences,
            approved_facts=facts,
            specialized_knowledge=specialized,
            approved_design_lessons=approved_lessons,
            combined_engineering_checks=combined_checks,
            knowledge_conflicts=conflicts,
            lesson_match_explanations=match_explanations,
            excluded_design_lessons=excluded_lessons,
            automatic_application_blocked=automatic_application_blocked,
            graph_relationships=graph_relationships,
            similar_models=similar,
            generic_engineering_checks=list(GENERIC_CHECKS),
            open_questions=(
                []
                if authorized_family
                else [
                    {
                        "kind": "design-requirements",
                        "prompt_intent": "Ask for function, load, space, interfaces, constraints, and family before using specialized knowledge.",
                    }
                ]
            ),
            excluded_specialized_knowledge=excluded,
        )
        return context.to_dict()

    @staticmethod
    def _render_assertion(item: dict[str, Any]) -> dict[str, Any]:
        redact_source_details = bool(item.get("_redact_source_details"))
        applicability = item["applicability"]
        if redact_source_details:
            applicability = {
                key: applicability[key]
                for key in ("constraint_kind",)
                if key in applicability
            }
            if item.get("_design_lesson_ref"):
                applicability["design_lesson_ref"] = item["_design_lesson_ref"]
        rendered = {
            "assertion_id": str(item["id"]),
            "subject_ref": item["subject_ref"],
            "predicate": item["predicate"],
            "object_value": item["object_value"],
            "unit": item.get("unit"),
            "scope_kind": item["scope_kind"],
            "family_id": item.get("family_id"),
            "risk_level": item["risk_level"],
            "source_kind": item.get("source_kind"),
            "evidence": [] if redact_source_details else item["evidence"],
            "applicability": applicability,
            "non_applicable_conditions": (
                [] if redact_source_details else item["non_applicable_conditions"]
            ),
            "contradicts": [str(value) for value in item.get("contradicts", [])],
            **(
                {"assertion_key": item["assertion_key"]}
                if item.get("assertion_key") is not None
                else {}
            ),
        }
        if redact_source_details:
            rendered["redactions"] = [
                {
                    "fields": ["applicability", "evidence", "non_applicable_conditions"],
                    "reason": LESSON_SOURCE_REDACTION_REASON,
                }
            ]
        if item.get("_user_requirement"):
            rendered["knowledge_kind"] = "user_requirement"
        return rendered

    @staticmethod
    def _classify_assertion(
        item: dict[str, Any],
        rendered: dict[str, Any],
        *,
        hard: list[dict[str, Any]],
        preferences: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        combined_checks: list[dict[str, Any]],
    ) -> None:
        constraint_kind = str(item.get("applicability", {}).get("constraint_kind", ""))
        if constraint_kind == "hard_constraint":
            hard.append(rendered)
        elif constraint_kind == "preference":
            preferences.append(rendered)
        else:
            facts.append(rendered)
            if constraint_kind in {"check", "warning"}:
                combined_checks.append(rendered)

    @classmethod
    def _render_lesson(
        cls,
        lesson: dict[str, Any],
        *,
        source_family_authorized: bool,
        design_lesson_ref: str,
    ) -> dict[str, Any]:
        rendered = {
            "design_lesson_ref": design_lesson_ref,
            "revision": lesson.get("revision"),
            "source_details_redacted": not source_family_authorized,
            "redactions": (
                []
                if source_family_authorized
                else [
                    {
                        "fields": [
                            "source_family_id",
                            "design_lesson_id",
                            "lesson_id",
                            "title",
                            "problem",
                            "root_causes",
                            "corrections",
                            "prevention",
                            "applicability",
                            "non_applicable_conditions",
                            "assertion_evidence",
                        ],
                        "reason": LESSON_SOURCE_REDACTION_REASON,
                    }
                ]
            ),
            "assertions": [
                cls._render_assertion({
                    **assertion,
                    "_redact_source_details": not source_family_authorized,
                    "_design_lesson_ref": design_lesson_ref,
                })
                for assertion in lesson.get("assertions", [])
                if assertion.get("status", "approved") == "approved"
            ],
        }
        if source_family_authorized:
            rendered.update({
                "design_lesson_id": str(lesson["id"]),
                "lesson_id": str(lesson.get("lesson_key") or lesson["id"]),
                "source_family_id": lesson.get("source_family_id"),
                "title": lesson["title"],
                "problem": lesson["problem"],
                "root_causes": lesson["root_causes"],
                "corrections": lesson["corrections"],
                "prevention": lesson["prevention"],
                "applicability": lesson["applicability"],
                "non_applicable_conditions": lesson.get("non_applicable_conditions", []),
            })
        return rendered

    @staticmethod
    def _render_lesson_match(
        lesson: dict[str, Any],
        match: dict[str, Any],
        *,
        source_family_authorized: bool,
        design_lesson_ref: str,
    ) -> dict[str, Any]:
        reasons = []
        if match["exact_query"]:
            reasons.append("exact approved lesson search term")
        if match["matched_features"]["failure_modes"]:
            reasons.append("matching failure mode")
        if match["matched_dimension_count"] >= 2:
            reasons.append("at least two structured applicability dimensions matched")
        rendered = {
            "design_lesson_ref": design_lesson_ref,
            "eligible": match["eligible"],
            "matched_features": match["matched_features"],
            "matched_dimension_count": match["matched_dimension_count"],
            "unmet_conditions": (
                match["unmet_conditions"] if source_family_authorized else []
            ),
            "unmet_condition_count": len(match["unmet_conditions"]),
            "matched_non_applicable_conditions": (
                match["matched_non_applicable_conditions"]
                if source_family_authorized
                else []
            ),
            "matched_non_applicable_condition_count": len(
                match["matched_non_applicable_conditions"]
            ),
            "exact_query": match["exact_query"],
            "reasons": reasons,
            "source_details_redacted": not source_family_authorized,
        }
        if source_family_authorized:
            rendered.update({
                "design_lesson_id": str(lesson["id"]),
                "lesson_id": str(lesson.get("lesson_key") or lesson["id"]),
                "title": lesson["title"],
            })
        else:
            rendered["redactions"] = [{
                "fields": [
                    "title",
                    "design_lesson_id",
                    "lesson_id",
                    "source incident narrative",
                    "unmet source applicability conditions",
                    "matched source non-applicable conditions",
                ],
                "reason": LESSON_SOURCE_REDACTION_REASON,
            }]
        return rendered

    @staticmethod
    def _opaque_lesson_ref(lesson: dict[str, Any]) -> str:
        digest = hashlib.sha256(str(lesson["id"]).encode("utf-8")).hexdigest()
        return f"design-lesson-{digest}"

    @classmethod
    def _knowledge_conflicts(
        cls,
        assertions: list[dict[str, Any]],
        *,
        design_features: dict[str, Any],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        effective_assertions = [
            assertion
            for assertion in assertions
            if cls._assertion_is_effective(assertion, design_features)
        ]
        for index, left in enumerate(effective_assertions):
            for right in effective_assertions[index + 1:]:
                left_id = str(left["id"])
                right_id = str(right["id"])
                pair = tuple(sorted((left_id, right_id)))
                left_kind = cls._constraint_kind(left)
                right_kind = cls._constraint_kind(right)
                user_requirement_involved = bool(
                    left.get("_user_requirement") or right.get("_user_requirement")
                )
                hard_involved = "hard_constraint" in {left_kind, right_kind}
                blocks_automatic_application = hard_involved or user_requirement_involved
                if (
                    right_id in {str(value) for value in left.get("contradicts", [])}
                    or left_id in {str(value) for value in right.get("contradicts", [])}
                ):
                    key = ("explicit_contradiction", *pair)
                    if key not in seen:
                        seen.add(key)
                        conflicts.append(cls._render_conflict(
                            "explicit_contradiction",
                            left,
                            right,
                            blocks_automatic_application,
                        ))
                same_key = (
                    left.get("subject_ref") == right.get("subject_ref")
                    and left.get("predicate") == right.get("predicate")
                )
                different_value = (
                    left.get("object_value") != right.get("object_value")
                    or left.get("unit") != right.get("unit")
                )
                same_effective_scope = (
                    cls._effective_scope_identity(left)
                    == cls._effective_scope_identity(right)
                )
                if (
                    same_key
                    and different_value
                    and (same_effective_scope or user_requirement_involved)
                    and cls._applicability_overlaps(left, right)
                    and not (left.get("_user_requirement") and right.get("_user_requirement"))
                ):
                    conflict_kind = (
                        "user_requirement_conflict"
                        if user_requirement_involved
                        else (
                            "hard_constraint_value_mismatch"
                            if left_kind == right_kind == "hard_constraint"
                            else "value_mismatch"
                        )
                    )
                    key = (conflict_kind, *pair)
                    if key not in seen:
                        seen.add(key)
                        conflicts.append(cls._render_conflict(
                            conflict_kind,
                            left,
                            right,
                            blocks_automatic_application,
                        ))
        return conflicts

    @staticmethod
    def _effective_scope_identity(assertion: dict[str, Any]) -> tuple[str, str | None]:
        scope_kind = str(assertion.get("scope_kind", ""))
        target_field = {
            "organization_general": "organization_id",
            "design_group": "design_group_id",
            "family": "family_id",
            "product": "product_id",
            "model": "model_revision_id",
            "current_design": "model_revision_id",
        }.get(scope_kind)
        target = assertion.get(target_field) if target_field else None
        return scope_kind, str(target) if target is not None else None

    @staticmethod
    def _structured_user_requirements(
        design_features: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_requirements = design_features.get("explicit_requirements", [])
        if not isinstance(raw_requirements, list):
            return []
        requirements: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_requirements):
            if not isinstance(raw, dict):
                # Free-form language is intentionally not interpreted as engineering semantics.
                continue
            if not all(key in raw for key in ("subject_ref", "predicate", "object_value")):
                continue
            subject_ref = raw.get("subject_ref")
            predicate = raw.get("predicate")
            unit = raw.get("unit")
            applicability = raw.get("applicability", {})
            non_applicable = raw.get("non_applicable_conditions", [])
            constraint_kind = raw.get("constraint_kind", "hard_constraint")
            if (
                not isinstance(subject_ref, str)
                or not subject_ref.strip()
                or not isinstance(predicate, str)
                or not predicate.strip()
                or unit is not None and not isinstance(unit, str)
                or not isinstance(applicability, dict)
                or not isinstance(non_applicable, list)
                or not all(isinstance(item, str) for item in non_applicable)
                or constraint_kind not in CONSTRAINT_KINDS
            ):
                continue
            requirements.append({
                "id": f"user-requirement-{index + 1}",
                "subject_ref": subject_ref,
                "predicate": predicate,
                "object_value": raw["object_value"],
                "unit": unit,
                "scope_kind": "current_design",
                "family_id": None,
                "risk_level": "explicit_requirement",
                "source_kind": "user_requirement",
                "evidence": [
                    {
                        "source": "design_features.explicit_requirements",
                        "index": index,
                    }
                ],
                "applicability": {
                    **applicability,
                    "constraint_kind": constraint_kind,
                },
                "non_applicable_conditions": list(non_applicable),
                "contradicts": [],
                "_user_requirement": True,
            })
        return requirements

    @staticmethod
    def _assertion_scope_is_authorized(
        assertion: dict[str, Any],
        *,
        authorized_family: str | None,
        model_revision_id: str | None,
    ) -> bool:
        scope_kind = str(assertion.get("scope_kind", ""))
        if scope_kind == "organization_general":
            return True
        if scope_kind in {"family", "design_group"}:
            return bool(
                authorized_family
                and str(assertion.get("family_id") or "") == authorized_family
            )
        if scope_kind == "model":
            return bool(
                model_revision_id
                and str(assertion.get("model_revision_id") or "") == model_revision_id
            )
        if scope_kind == "product":
            # Product assertions are selected by the authoritative repository through
            # the current model. Without a model there is no product authorization.
            return bool(model_revision_id)
        return False

    @staticmethod
    def _declared_conditions(design_features: dict[str, Any]) -> set[str]:
        return {
            str(condition)
            for field in ("satisfied_conditions", "declared_conditions")
            for condition in design_features.get(field, [])
            if isinstance(condition, str)
        }

    @classmethod
    def _assertion_is_effective(
        cls,
        assertion: dict[str, Any],
        design_features: dict[str, Any],
    ) -> bool:
        applicability = assertion.get("applicability", {})
        declared_conditions = cls._declared_conditions(design_features)
        required = {
            str(condition)
            for condition in applicability.get("required_conditions", [])
        }
        non_applicable = {
            str(condition)
            for condition in assertion.get("non_applicable_conditions", [])
        }
        if not required.issubset(declared_conditions):
            return False
        if not condition_expression_satisfied(
            applicability.get("required_condition_expression"), declared_conditions
        ):
            return False
        if non_applicable & declared_conditions:
            return False
        for dimension in (
            "component_classes",
            "interface_types",
            "design_stages",
            "failure_modes",
        ):
            rule_values = set(applicability.get(dimension, []))
            current_values = set(design_features.get(dimension, []))
            if rule_values and current_values and rule_values.isdisjoint(current_values):
                return False
        return True

    @staticmethod
    def _applicability_overlaps(
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> bool:
        left_applicability = left.get("applicability", {})
        right_applicability = right.get("applicability", {})
        for dimension in (
            "component_classes",
            "interface_types",
            "design_stages",
            "failure_modes",
        ):
            left_values = set(left_applicability.get(dimension, []))
            right_values = set(right_applicability.get(dimension, []))
            if left_values and right_values and left_values.isdisjoint(right_values):
                return False
        left_required = set(left_applicability.get("required_conditions", []))
        right_required = set(right_applicability.get("required_conditions", []))
        left_excluded = set(left.get("non_applicable_conditions", []))
        right_excluded = set(right.get("non_applicable_conditions", []))
        return not (
            left_required & right_excluded
            or right_required & left_excluded
        )

    @staticmethod
    def _constraint_kind(assertion: dict[str, Any]) -> str:
        return str(assertion.get("applicability", {}).get("constraint_kind", ""))

    @classmethod
    def _render_conflict(
        cls,
        kind: str,
        left: dict[str, Any],
        right: dict[str, Any],
        blocks_automatic_application: bool,
    ) -> dict[str, Any]:
        left_rendered = cls._render_assertion(left)
        right_rendered = cls._render_assertion(right)
        user_requirement = (
            left_rendered
            if left.get("_user_requirement")
            else right_rendered if right.get("_user_requirement") else None
        )
        approved_assertion = (
            right_rendered
            if left.get("_user_requirement")
            else left_rendered if right.get("_user_requirement") else None
        )
        conflict = {
            "kind": kind,
            "severity": "hard_constraint" if blocks_automatic_application else "advisory",
            "automatic_application_blocked": blocks_automatic_application,
            "assertions": [left_rendered, right_rendered],
            "includes_user_requirement": user_requirement is not None,
            "evidence": {
                "assertion_ids": [str(left["id"]), str(right["id"])],
                "subject_ref": (
                    left.get("subject_ref")
                    if left.get("subject_ref") == right.get("subject_ref")
                    else None
                ),
                "predicate": (
                    left.get("predicate")
                    if left.get("predicate") == right.get("predicate")
                    else None
                ),
                "values": [
                    {"object_value": left.get("object_value"), "unit": left.get("unit")},
                    {"object_value": right.get("object_value"), "unit": right.get("unit")},
                ],
            },
        }
        if user_requirement is not None:
            conflict["user_requirement"] = user_requirement
            conflict["approved_assertion"] = approved_assertion
            conflict["priority"] = "user_requirement"
        return conflict
