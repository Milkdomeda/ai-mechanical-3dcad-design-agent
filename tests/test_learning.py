from __future__ import annotations

import unittest

from mechanical_design_agent.learning import (
    family_statistics,
    generate_question_targets,
    parse_assertion_proposals,
)


def manifest(label: str = "Part001") -> dict:
    return {
        "source": {"sha256": "a" * 64},
        "document": {"name": "fixture"},
        "shape_definitions": [
            {
                "source_id": "Part001",
                "source_name": "Part001",
                "source_label": label,
                "node_kind": "multi-solid-shape",
                "primary_parent_source_id": None,
                "topology": {"solid_count": 2},
                "bbox_mm": {"size": [10, 20, 30]},
                "surface_type_counts": {"Plane": 6},
            }
        ],
        "solid_fragments": [{}, {}],
        "repeated_shape_groups": [],
        "relation_candidates": [],
    }


class LearningTests(unittest.TestCase):
    def test_questions_are_bounded_generic_and_include_prompt_intent(self) -> None:
        targets = generate_question_targets(manifest(), family_confirmed=False, limit=5)
        self.assertLessEqual(len(targets), 5)
        self.assertEqual(targets[0]["question_kind"], "product-family-identity")
        self.assertTrue(all(item.get("prompt_intent") for item in targets))
        serialized = repr(targets).lower()
        for forbidden in ("线性执行", "蜗轮", "worm gear", "reducer"):
            self.assertNotIn(forbidden, serialized)

    def test_statistical_generalization_requires_three_models(self) -> None:
        self.assertFalse(family_statistics([manifest(), manifest("Part002")])["generalization_allowed"])
        self.assertTrue(
            family_statistics([manifest(), manifest("Part002"), manifest("Part003")])[
                "generalization_allowed"
            ]
        )

    def test_question_limit_cannot_exceed_five(self) -> None:
        with self.assertRaises(ValueError):
            generate_question_targets(manifest(), family_confirmed=False, limit=6)

    def test_low_risk_knowledge_cannot_be_promoted_to_broad_scope(self) -> None:
        with self.assertRaises(ValueError):
            parse_assertion_proposals(
                [
                    {
                        "subject_ref": "component:1",
                        "predicate": "canonical-name",
                        "object_value": "engineer term",
                        "scope_kind": "family",
                        "risk_level": "R1",
                        "evidence": [{"answer_event_id": "00000000-0000-0000-0000-000000000001"}],
                    }
                ]
            )

    def test_r3_may_be_staged_for_owner_review_at_organization_scope(self) -> None:
        proposals = parse_assertion_proposals(
            [
                {
                    "subject_ref": "organization",
                    "predicate": "hard-constraint",
                    "object_value": {"rule": "review required"},
                    "scope_kind": "organization_general",
                    "risk_level": "R3",
                    "evidence": [{"answer_event_id": "00000000-0000-0000-0000-000000000001"}],
                }
            ]
        )
        self.assertEqual(proposals[0].scope_kind, "organization_general")


if __name__ == "__main__":
    unittest.main()
