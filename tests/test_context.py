from __future__ import annotations

import json
import unittest

from mechanical_design_agent.context import DesignContextBuilder
from mechanical_design_agent.models import canonical_json


class FakeRepository:
    def get_design_group(self, design_group_id: str) -> dict:
        return {"id": design_group_id, "organization_id": "org"}

    def get_model_analysis(self, model_revision_id: str) -> dict:
        return {
            "id": model_revision_id,
            "organization_id": "org",
            "design_group_id": "group-a",
            "family_id": "family-a",
        }

    def get_family(self, family_id: str) -> dict:
        return {"id": family_id, "organization_id": "org", "design_group_id": "group-a"}

    def approved_assertions(self, **kwargs) -> list[dict]:
        generic = {
            "id": "generic",
            "subject_ref": "organization",
            "predicate": "uses-units",
            "object_value": "mm",
            "unit": None,
            "scope_kind": "organization_general",
            "family_id": None,
            "risk_level": "R1",
            "evidence": [{"source": "approved"}],
            "applicability": {},
            "non_applicable_conditions": [],
        }
        if kwargs["family_id"] is None:
            return [generic]
        specialized = dict(generic)
        specialized.update(
            {
                "id": "specialized",
                "subject_ref": "component:1",
                "predicate": "canonical-name",
                "object_value": "private-family-name",
                "scope_kind": "family",
                "family_id": "family-a",
            }
        )
        return [generic, specialized]

    def approved_family_profile(self, family_id: str):
        return {
            "id": "profile",
            "family_id": family_id,
            "profile": {"observation": "reviewed"},
            "evidence": [{"models": 3}],
            "distinct_model_count": 3,
        }

    def similar_models(self, family_id: str, model_revision_id: str):
        return []

    def excluded_specialized_count(self, organization_id: str, design_group_id: str, family_id: str | None):
        return []


class FakeGraphProjection:
    def __init__(self):
        self.calls = []

    def scoped_relationships(self, **kwargs):
        self.calls.append(kwargs)
        return [{"source_id": "model", "relationship": "MEMBER_OF", "target_id": "family-a"}]


class ConstraintKindRepository(FakeRepository):
    def approved_assertions(self, **kwargs) -> list[dict]:
        common = {
            "subject_ref": "component:actuator",
            "object_value": True,
            "unit": None,
            "scope_kind": "organization_general",
            "family_id": None,
            "risk_level": "R3",
            "evidence": [{"source": "approved-design-lesson"}],
            "non_applicable_conditions": [],
        }
        return [
            {
                **common,
                "id": "hard",
                "predicate": "requires-clearance",
                "applicability": {"constraint_kind": "hard_constraint"},
            },
            {
                **common,
                "id": "preference",
                "predicate": "prefers-access",
                "applicability": {"constraint_kind": "preference"},
            },
            {
                **common,
                "id": "check",
                "predicate": "inspect-mount",
                "applicability": {"constraint_kind": "check"},
            },
            {
                **common,
                "id": "warning",
                "predicate": "warn-interference",
                "applicability": {"constraint_kind": "warning"},
            },
        ]


def matching_features() -> dict:
    return {
        "component_classes": ["shaft-support"],
        "interface_types": ["coaxial-interface"],
        "design_stages": ["assembly-layout"],
        "failure_modes": [],
        "satisfied_conditions": ["catalog-support-present"],
        "explicit_requirements": [],
    }


def lesson_assertion(
    assertion_id: str,
    *,
    predicate: str = "requires-support-check",
    object_value=True,
    constraint_kind: str = "check",
    contradicts: list[str] | None = None,
) -> dict:
    return {
        "id": assertion_id,
        "assertion_key": assertion_id,
        "subject_ref": "interface:shaft-support",
        "predicate": predicate,
        "object_value": object_value,
        "unit": None,
        "scope_kind": "organization_general",
        "family_id": None,
        "risk_level": "R3",
        "status": "approved",
        "source_kind": "approved_design_lesson",
        "evidence": [{"source": "approved-design-lesson"}],
        "applicability": {
            "constraint_kind": constraint_kind,
            "lesson_id": "DL-GENERAL",
        },
        "non_applicable_conditions": [],
        "contradicts": list(contradicts or []),
    }


def ordinary_hard_constraint(
    assertion_id: str,
    *,
    object_value,
    design_stages: list[str] | None = None,
    scope_kind: str = "organization_general",
    family_id: str | None = None,
) -> dict:
    assertion = lesson_assertion(
        assertion_id,
        predicate="sets-support-mode",
        object_value=object_value,
        constraint_kind="hard_constraint",
    )
    assertion.update({
        "scope_kind": scope_kind,
        "family_id": family_id,
        "source_kind": "manual_review",
        "applicability": {
            "constraint_kind": "hard_constraint",
            **({"design_stages": design_stages} if design_stages is not None else {}),
        },
    })
    return assertion


def general_lesson(*, status: str = "approved", assertions: list[dict] | None = None) -> dict:
    return {
        "id": "lesson-general",
        "lesson_key": "DL-GENERAL",
        "revision": 1,
        "status": status,
        "title": "Check support alignment before layout release",
        "problem": {"summary": "A support was misaligned", "failure_modes": ["misalignment"]},
        "root_causes": ["Interface alignment was not checked"],
        "corrections": ["Add an alignment check"],
        "prevention": {"check": "Inspect support alignment"},
        "applicability": {
            "component_classes": ["shaft-support"],
            "interface_types": ["coaxial-interface"],
            "design_stages": ["assembly-layout"],
            "required_conditions": ["catalog-support-present"],
        },
        "non_applicable_conditions": [],
        "search_terms": ["support alignment"],
        "assertions": assertions or [lesson_assertion("lesson-check")],
    }


def private_source_lesson() -> dict:
    private_term = "SOURCE-FAMILY-SECRET-X9"
    assertion = lesson_assertion("safe-general-check")
    assertion["evidence"] = [{"path": f"screenshots/{private_term}.png"}]
    assertion["applicability"] = {
        **assertion["applicability"],
        "source_family_note": private_term,
    }
    lesson = general_lesson(assertions=[assertion])
    lesson.update({
        "source_family_id": "family-a",
        "title": f"{private_term} incident",
        "problem": {"summary": private_term, "failure_modes": ["misalignment"]},
        "root_causes": [private_term],
        "corrections": [private_term],
        "prevention": {"private_instruction": private_term},
        "applicability": {
            **lesson["applicability"],
            "source_family_note": private_term,
        },
    })
    return lesson


class LessonRepository(FakeRepository):
    def __init__(self, lessons: list[dict] | None = None, ordinary: list[dict] | None = None):
        self.lessons = lessons if lessons is not None else [general_lesson()]
        self.ordinary = ordinary

    def approved_assertions(self, **kwargs) -> list[dict]:
        base = super().approved_assertions(**kwargs) if self.ordinary is None else list(self.ordinary)
        # The repository's ordinary retrieval currently also sees published lesson assertions.
        # Context must suppress this copy and add only assertions from eligible lessons.
        return [*base, lesson_assertion("ordinary-duplicate")]

    def search_approved_design_lessons(self, **kwargs) -> list[dict]:
        return list(self.lessons)

    def excluded_specialized_count(self, organization_id: str, design_group_id: str, family_id: str | None):
        return [{"family_id": "private-family-name", "scope_kind": "family", "count": 2}]


class DesignContextTests(unittest.TestCase):
    def test_r3_assertions_are_classified_by_constraint_kind(self) -> None:
        context = DesignContextBuilder(ConstraintKindRepository()).build(
            organization_id="org", design_group_id="group-a"
        )

        self.assertEqual([item["assertion_id"] for item in context["hard_constraints"]], ["hard"])
        self.assertEqual([item["assertion_id"] for item in context["preferences"]], ["preference"])
        self.assertEqual(
            [item["assertion_id"] for item in context["approved_facts"]],
            ["check", "warning"],
        )

    def test_authorized_family_returns_family_knowledge_and_general_lesson(self) -> None:
        context = DesignContextBuilder(LessonRepository()).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
            design_features=matching_features(),
        )

        self.assertTrue(context["specialized_knowledge"])
        self.assertEqual(len(context["approved_design_lessons"]), 1)
        self.assertEqual(context["schema_version"], "DesignContext/v2")

    def test_no_family_authority_still_returns_general_lesson_without_family_terms(self) -> None:
        context = DesignContextBuilder(LessonRepository()).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=matching_features(),
        )

        self.assertEqual(len(context["approved_design_lessons"]), 1)
        self.assertEqual(context["specialized_knowledge"], [])
        self.assertNotIn("private-family-name", json.dumps(context))

    def test_source_incident_details_are_redacted_without_matching_family_authority(self) -> None:
        private_term = "SOURCE-FAMILY-SECRET-X9"
        for family_kwargs in (
            {},
            {"requested_family_id": "family-b", "explicit_family_authorization": True},
        ):
            with self.subTest(family_kwargs=family_kwargs):
                context = DesignContextBuilder(
                    LessonRepository(lessons=[private_source_lesson()])
                ).build(
                    organization_id="org",
                    design_group_id="group-a",
                    design_features=matching_features(),
                    **family_kwargs,
                )

                serialized = json.dumps(context)
                self.assertNotIn(private_term, serialized)
                self.assertIn("safe-general-check", serialized)
                lesson = context["approved_design_lessons"][0]
                self.assertTrue(lesson["source_details_redacted"])
                self.assertTrue(lesson["redactions"])
                self.assertEqual(lesson["assertions"][0]["evidence"], [])

    def test_source_incident_details_are_visible_only_to_matching_family_authority(self) -> None:
        context = DesignContextBuilder(
            LessonRepository(lessons=[private_source_lesson()])
        ).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
            design_features=matching_features(),
        )

        self.assertIn("SOURCE-FAMILY-SECRET-X9", json.dumps(context))
        self.assertFalse(context["approved_design_lessons"][0]["source_details_redacted"])

    def test_unmatched_source_family_condition_is_redacted_from_exclusion_evidence(self) -> None:
        private_term = "SOURCE-FAMILY-SECRET-X9"
        lesson = private_source_lesson()
        lesson["applicability"]["required_conditions"].append(private_term)

        context = DesignContextBuilder(LessonRepository([lesson])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=matching_features(),
        )

        self.assertNotIn(private_term, json.dumps(context))
        excluded = context["excluded_design_lessons"][0]
        self.assertEqual(excluded["unmet_conditions"], [])
        self.assertEqual(excluded["unmet_condition_count"], 1)

    def test_unauthorized_lesson_identity_uses_one_opaque_ref_across_all_outputs(self) -> None:
        private_lesson_id = "PRIVATE-SOURCE-LESSON-ID-X9"
        approved = ordinary_hard_constraint("approved-hard", object_value="approved")
        lesson_hard = lesson_assertion(
            "lesson-hard",
            predicate="sets-support-mode",
            object_value="lesson",
            constraint_kind="hard_constraint",
        )
        lesson_hard["applicability"]["lesson_id"] = private_lesson_id
        lesson = general_lesson(assertions=[lesson_hard])
        lesson.update({
            "id": private_lesson_id,
            "lesson_key": private_lesson_id,
            "source_family_id": "family-a",
        })

        context = DesignContextBuilder(LessonRepository([lesson], [approved])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=matching_features(),
        )

        serialized = canonical_json(context)
        self.assertNotIn(private_lesson_id, serialized)
        lesson_ref = context["approved_design_lessons"][0]["design_lesson_ref"]
        self.assertEqual(context["lesson_match_explanations"][0]["design_lesson_ref"], lesson_ref)
        lesson_constraint = next(
            item for item in context["hard_constraints"]
            if item.get("source_kind") == "approved_design_lesson"
        )
        self.assertEqual(lesson_constraint["applicability"]["design_lesson_ref"], lesson_ref)
        conflict_lesson = next(
            item for item in context["knowledge_conflicts"][0]["assertions"]
            if item.get("source_kind") == "approved_design_lesson"
        )
        self.assertEqual(conflict_lesson["applicability"]["design_lesson_ref"], lesson_ref)

        authorized = DesignContextBuilder(LessonRepository([lesson], [approved])).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
            design_features=matching_features(),
        )
        self.assertIn(private_lesson_id, canonical_json(authorized))
        self.assertEqual(authorized["approved_design_lessons"][0]["lesson_id"], private_lesson_id)

    def test_matched_private_non_applicable_condition_is_redacted_but_still_excludes(self) -> None:
        private_lesson_id = "PRIVATE-SOURCE-LESSON-ID-X9"
        private_condition = "PRIVATE-SOURCE-CONDITION-X9"
        lesson = general_lesson(assertions=[lesson_assertion(
            "private-condition-hard",
            constraint_kind="hard_constraint",
        )])
        lesson.update({
            "id": private_lesson_id,
            "lesson_key": private_lesson_id,
            "source_family_id": "family-a",
            "non_applicable_conditions": [private_condition],
        })
        features = matching_features()
        features["satisfied_conditions"].append(private_condition)

        context = DesignContextBuilder(LessonRepository([lesson])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=features,
        )

        serialized = canonical_json(context)
        self.assertNotIn(private_lesson_id, serialized)
        self.assertNotIn(private_condition, serialized)
        excluded = context["excluded_design_lessons"][0]
        self.assertEqual(excluded["matched_non_applicable_conditions"], [])
        self.assertEqual(excluded["matched_non_applicable_condition_count"], 1)
        self.assertTrue(excluded["design_lesson_ref"])
        self.assertEqual(context["approved_design_lessons"], [])
        self.assertFalse(
            any(item["assertion_id"] == "private-condition-hard" for item in context["hard_constraints"])
        )

        authorized = DesignContextBuilder(LessonRepository([lesson])).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
            design_features=features,
        )
        authorized_excluded = authorized["excluded_design_lessons"][0]
        self.assertEqual(authorized_excluded["lesson_id"], private_lesson_id)
        self.assertEqual(
            authorized_excluded["matched_non_applicable_conditions"],
            [private_condition],
        )

    def test_unmet_conditions_exclude_lesson_with_structured_reason(self) -> None:
        features = matching_features()
        features["satisfied_conditions"] = []

        context = DesignContextBuilder(LessonRepository()).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=features,
        )

        self.assertEqual(context["approved_design_lessons"], [])
        excluded = context["excluded_design_lessons"][0]
        self.assertEqual(excluded["unmet_conditions"], [])
        self.assertEqual(excluded["unmet_condition_count"], 1)
        self.assertEqual(excluded["reason"], "unmet required conditions")

    def test_non_applicable_condition_excludes_lesson_and_its_hard_constraint(self) -> None:
        lesson = general_lesson(assertions=[lesson_assertion(
            "sealed-unit-hard",
            predicate="requires-open-service-access",
            constraint_kind="hard_constraint",
        )])
        lesson["non_applicable_conditions"] = ["sealed-unit"]
        features = matching_features()
        features["satisfied_conditions"].append("sealed-unit")

        context = DesignContextBuilder(LessonRepository([lesson])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=features,
        )

        self.assertEqual(context["approved_design_lessons"], [])
        excluded = context["excluded_design_lessons"][0]
        self.assertEqual(excluded["matched_non_applicable_conditions"], [])
        self.assertEqual(excluded["matched_non_applicable_condition_count"], 1)
        self.assertFalse(
            any(item["assertion_id"] == "sealed-unit-hard" for item in context["hard_constraints"])
        )

    def test_one_weak_matching_dimension_is_insufficient(self) -> None:
        context = DesignContextBuilder(LessonRepository()).build(
            organization_id="org",
            design_group_id="group-a",
            design_features={
                "component_classes": ["shaft-support"],
                "interface_types": [],
                "design_stages": [],
                "failure_modes": [],
                "satisfied_conditions": ["catalog-support-present"],
            },
        )

        self.assertEqual(context["approved_design_lessons"], [])
        self.assertEqual(
            context["excluded_design_lessons"][0]["reason"],
            "insufficient structured applicability match",
        )

    def test_exact_lesson_query_can_include_lesson(self) -> None:
        context = DesignContextBuilder(LessonRepository()).build(
            organization_id="org",
            design_group_id="group-a",
            design_features={"satisfied_conditions": ["catalog-support-present"]},
            lesson_query="  SUPPORT ALIGNMENT  ",
        )

        self.assertEqual(len(context["approved_design_lessons"]), 1)
        self.assertTrue(context["lesson_match_explanations"][0]["exact_query"])

    def test_revoked_lessons_are_absent(self) -> None:
        context = DesignContextBuilder(
            LessonRepository(lessons=[general_lesson(status="revoked")])
        ).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=matching_features(),
        )

        self.assertEqual(context["approved_design_lessons"], [])
        self.assertEqual(context["lesson_match_explanations"], [])
        self.assertEqual(context["excluded_design_lessons"], [])

    def test_lesson_check_is_not_hard_and_is_added_to_combined_checks_once(self) -> None:
        context = DesignContextBuilder(LessonRepository()).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=matching_features(),
        )

        self.assertFalse(
            any(item["applicability"].get("constraint_kind") == "check" for item in context["hard_constraints"])
        )
        lesson_checks = [
            item for item in context["combined_engineering_checks"]
            if item.get("assertion_id") == "lesson-check"
        ]
        self.assertEqual(len(lesson_checks), 1)
        self.assertNotIn("ordinary-duplicate", json.dumps(context))

    def test_explicit_contradiction_is_reported_and_blocks_hard_application(self) -> None:
        ordinary = lesson_assertion(
            "ordinary-hard",
            predicate="sets-bearing-mode",
            object_value="fixed",
            constraint_kind="hard_constraint",
        )
        ordinary["source_kind"] = "manual_review"
        lesson = general_lesson(assertions=[lesson_assertion(
            "lesson-hard",
            predicate="sets-bearing-arrangement",
            object_value="floating",
            constraint_kind="hard_constraint",
            contradicts=["ordinary-hard"],
        )])

        context = DesignContextBuilder(LessonRepository([lesson], [ordinary])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=matching_features(),
        )

        self.assertEqual(context["knowledge_conflicts"][0]["kind"], "explicit_contradiction")
        self.assertTrue(context["automatic_application_blocked"])

    def test_same_key_different_hard_values_are_reported_and_blocked(self) -> None:
        ordinary = lesson_assertion(
            "ordinary-hard",
            predicate="minimum-support-clearance",
            object_value={"mm": 2},
            constraint_kind="hard_constraint",
        )
        ordinary["source_kind"] = "manual_review"
        lesson = general_lesson(assertions=[lesson_assertion(
            "lesson-hard",
            predicate="minimum-support-clearance",
            object_value={"mm": 5},
            constraint_kind="hard_constraint",
        )])

        context = DesignContextBuilder(LessonRepository([lesson], [ordinary])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=matching_features(),
        )

        self.assertEqual(
            context["knowledge_conflicts"][0]["kind"],
            "hard_constraint_value_mismatch",
        )
        self.assertTrue(context["automatic_application_blocked"])

    def test_all_same_scope_value_mismatches_are_reported_as_advisory(self) -> None:
        assertions = [
            lesson_assertion(
                assertion_id,
                predicate="sets-support-mode",
                object_value=object_value,
                constraint_kind="check",
            )
            for assertion_id, object_value in (
                ("check-fixed", "fixed"),
                ("check-floating", "floating"),
                ("check-guided", "guided"),
            )
        ]
        for assertion in assertions:
            assertion["source_kind"] = "manual_review"

        context = DesignContextBuilder(LessonRepository([], assertions)).build(
            organization_id="org",
            design_group_id="group-a",
            design_features={},
        )

        self.assertEqual(len(context["knowledge_conflicts"]), 3)
        self.assertEqual(
            {
                frozenset(conflict["evidence"]["assertion_ids"])
                for conflict in context["knowledge_conflicts"]
            },
            {
                frozenset(("check-fixed", "check-floating")),
                frozenset(("check-fixed", "check-guided")),
                frozenset(("check-floating", "check-guided")),
            },
        )
        self.assertTrue(
            all(conflict["kind"] == "value_mismatch" for conflict in context["knowledge_conflicts"])
        )
        self.assertTrue(
            all(
                conflict["severity"] == "advisory"
                and not conflict["automatic_application_blocked"]
                for conflict in context["knowledge_conflicts"]
            )
        )
        self.assertFalse(context["automatic_application_blocked"])

    def test_same_scope_value_mismatch_involving_hard_constraint_blocks(self) -> None:
        hard = ordinary_hard_constraint("hard-rule", object_value="fixed")
        check = lesson_assertion(
            "check-rule",
            predicate="sets-support-mode",
            object_value="floating",
            constraint_kind="check",
        )
        check["source_kind"] = "manual_review"

        context = DesignContextBuilder(LessonRepository([], [hard, check])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features={},
        )

        self.assertEqual(len(context["knowledge_conflicts"]), 1)
        self.assertEqual(context["knowledge_conflicts"][0]["kind"], "value_mismatch")
        self.assertTrue(context["knowledge_conflicts"][0]["automatic_application_blocked"])
        self.assertTrue(context["automatic_application_blocked"])

    def test_same_scope_kind_with_different_targets_does_not_conflict(self) -> None:
        family_a = ordinary_hard_constraint(
            "family-a-rule",
            object_value="fixed",
            scope_kind="family",
            family_id="family-a",
        )
        family_b = ordinary_hard_constraint(
            "family-b-rule",
            object_value="floating",
            scope_kind="family",
            family_id="family-b",
        )

        conflicts = DesignContextBuilder._knowledge_conflicts(
            [family_a, family_b],
            design_features={},
        )

        self.assertEqual(conflicts, [])

    def test_mutually_exclusive_design_stages_do_not_create_same_key_conflict(self) -> None:
        concept = ordinary_hard_constraint(
            "concept-rule",
            object_value="concept-value",
            design_stages=["concept-stage"],
        )
        release = ordinary_hard_constraint(
            "release-rule",
            object_value="release-value",
            design_stages=["release-stage"],
        )

        context = DesignContextBuilder(LessonRepository([], [concept, release])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features={"design_stages": ["concept-stage", "release-stage"]},
        )

        self.assertEqual(context["knowledge_conflicts"], [])
        self.assertFalse(context["automatic_application_blocked"])

    def test_hard_rules_not_effective_for_current_stage_do_not_conflict(self) -> None:
        release_a = ordinary_hard_constraint(
            "release-a",
            object_value="value-a",
            design_stages=["release-stage"],
        )
        release_b = ordinary_hard_constraint(
            "release-b",
            object_value="value-b",
            design_stages=["release-stage"],
        )

        context = DesignContextBuilder(LessonRepository([], [release_a, release_b])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features={"design_stages": ["concept-stage"]},
        )

        self.assertEqual(context["knowledge_conflicts"], [])

    def test_unrelated_family_assertion_is_omitted_and_cannot_conflict(self) -> None:
        general = ordinary_hard_constraint("general-rule", object_value="general")
        unrelated = ordinary_hard_constraint(
            "unrelated-rule",
            object_value="UNRELATED-FAMILY-SECRET",
            scope_kind="family",
            family_id="family-b",
        )

        context = DesignContextBuilder(LessonRepository([], [general, unrelated])).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
            design_features={},
        )

        self.assertNotIn("UNRELATED-FAMILY-SECRET", json.dumps(context))
        self.assertEqual(context["knowledge_conflicts"], [])

    def test_organization_general_and_current_family_hard_rules_do_not_conflict(self) -> None:
        general = ordinary_hard_constraint("general-rule", object_value="general")
        current_family = ordinary_hard_constraint(
            "current-family-rule",
            object_value="family-specific",
            scope_kind="family",
            family_id="family-a",
        )

        context = DesignContextBuilder(LessonRepository([], [general, current_family])).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
            design_features={},
        )

        self.assertEqual(context["knowledge_conflicts"], [])
        self.assertFalse(context["automatic_application_blocked"])

    def test_structured_user_requirement_conflicts_with_approved_hard_constraint(self) -> None:
        approved = ordinary_hard_constraint("approved-hard", object_value=2)
        approved["unit"] = "mm"
        features = {
            "explicit_requirements": [
                {
                    "subject_ref": "interface:shaft-support",
                    "predicate": "sets-support-mode",
                    "object_value": 5,
                    "unit": "mm",
                    "constraint_kind": "hard_constraint",
                    "applicability": {"design_stages": ["assembly-layout"]},
                }
            ],
            "design_stages": ["assembly-layout"],
        }

        context = DesignContextBuilder(LessonRepository([], [approved])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=features,
        )

        conflict = context["knowledge_conflicts"][0]
        self.assertTrue(conflict["includes_user_requirement"])
        self.assertEqual(conflict["user_requirement"]["object_value"], 5)
        self.assertEqual(conflict["approved_assertion"]["assertion_id"], "approved-hard")
        self.assertTrue(context["automatic_application_blocked"])
        self.assertEqual(
            context["hard_constraints"][0].get("knowledge_kind"),
            "user_requirement",
        )

    def test_explicit_user_requirement_conflict_blocks_even_without_hard_constraint(self) -> None:
        approved = lesson_assertion(
            "approved-check",
            predicate="sets-support-mode",
            object_value="fixed",
            constraint_kind="check",
        )
        approved["source_kind"] = "manual_review"
        features = {
            "explicit_requirements": [
                {
                    "subject_ref": "interface:shaft-support",
                    "predicate": "sets-support-mode",
                    "object_value": "floating",
                    "constraint_kind": "check",
                }
            ],
        }

        context = DesignContextBuilder(LessonRepository([], [approved])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=features,
        )

        self.assertEqual(len(context["knowledge_conflicts"]), 1)
        conflict = context["knowledge_conflicts"][0]
        self.assertEqual(conflict["kind"], "user_requirement_conflict")
        self.assertTrue(conflict["includes_user_requirement"])
        self.assertTrue(conflict["automatic_application_blocked"])
        self.assertTrue(context["automatic_application_blocked"])

    def test_plain_text_explicit_requirement_is_not_semantically_guessed(self) -> None:
        approved = ordinary_hard_constraint("approved-hard", object_value=2)

        context = DesignContextBuilder(LessonRepository([], [approved])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features={"explicit_requirements": ["make the support much larger"]},
        )

        self.assertEqual(context["knowledge_conflicts"], [])
        self.assertFalse(
            any(item.get("knowledge_kind") == "user_requirement" for item in context["hard_constraints"])
        )

    def test_user_requirement_outside_current_stage_does_not_conflict(self) -> None:
        approved = ordinary_hard_constraint("approved-hard", object_value=2)
        features = {
            "explicit_requirements": [
                {
                    "subject_ref": "interface:shaft-support",
                    "predicate": "sets-support-mode",
                    "object_value": 5,
                    "constraint_kind": "hard_constraint",
                    "applicability": {"design_stages": ["release-stage"]},
                }
            ],
            "design_stages": ["concept-stage"],
        }

        context = DesignContextBuilder(LessonRepository([], [approved])).build(
            organization_id="org",
            design_group_id="group-a",
            design_features=features,
        )

        self.assertEqual(context["knowledge_conflicts"], [])

    def test_no_family_authority_returns_no_specialized_knowledge(self) -> None:
        context = DesignContextBuilder(FakeRepository()).build(
            organization_id="org", design_group_id="group-a"
        )
        self.assertEqual(context["specialized_knowledge"], [])
        self.assertEqual(context["authorization_basis"], "no-specialized-family-authority")
        self.assertTrue(context["open_questions"])

    def test_graph_is_not_queried_before_scope_authorization(self) -> None:
        graph = FakeGraphProjection()
        context = DesignContextBuilder(FakeRepository(), graph).build(
            organization_id="org", design_group_id="group-a"
        )
        self.assertEqual(graph.calls, [])
        self.assertEqual(context["graph_relationships"], [])

    def test_explicit_family_authority_enables_only_that_family(self) -> None:
        context = DesignContextBuilder(FakeRepository()).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
        )
        self.assertEqual(context["authorization_basis"], "explicit-current-session-family-authorization")
        self.assertEqual(len(context["specialized_knowledge"]), 2)

    def test_graph_is_queried_only_with_authorized_family(self) -> None:
        graph = FakeGraphProjection()
        context = DesignContextBuilder(FakeRepository(), graph).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
        )
        self.assertEqual(graph.calls, [{"family_id": "family-a", "model_revision_id": None}])
        self.assertEqual(context["graph_relationships"][0]["target_id"], "family-a")

    def test_cross_scope_family_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DesignContextBuilder(FakeRepository()).build(
                organization_id="another-org",
                design_group_id="group-a",
                requested_family_id="family-a",
                explicit_family_authorization=True,
            )


if __name__ == "__main__":
    unittest.main()
