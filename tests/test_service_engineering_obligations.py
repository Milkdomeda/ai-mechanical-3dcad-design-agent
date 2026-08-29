from __future__ import annotations

from mechanical_design_agent.engineering_obligations import engineering_scope_sha256
from mechanical_design_agent.service import MechanicalDesignService


def scope() -> dict:
    return {
        "schema_version": "EngineeringScope/v1",
        "deliverable_kind": "single_part",
        "component_count": 1,
        "motion_present": False,
        "assembly_interfaces": [],
        "component_plan": [
            {
                "component_id": "plate",
                "category": "machined_plate",
                "sourcing_class": "custom",
                "included_in_delivery": True,
            }
        ],
    }


class Repository:
    def latest_product_family_match_decision(self, **_kwargs: object) -> dict:
        return {"status": "unbound_no_match"}

    def product_family_inventory(self, **_kwargs: object) -> list[dict]:
        return [{"family_id": "unrelated"}]

    def latest_retrieval_receipt(self, _working_copy_id: str) -> dict:
        return {"retrieval_status": "completed_no_match"}

    def get_active_approval_envelope(self, _working_copy_id: str) -> dict:
        return {"design_intent": {"engineering_scope": scope()}}

    def latest_design_job_obligation_decisions(
        self, **kwargs: object
    ) -> dict[str, dict]:
        assert kwargs["scope_sha256"] == engineering_scope_sha256(scope())
        common = {
            "resolution_level": "screening",
            "rationale": "Explicitly screened for this simple part.",
            "evidence_refs": [],
            "engineering_scope": scope(),
            "scope_sha256": engineering_scope_sha256(scope()),
        }
        return {
            "standard_parts_assessment": {
                **common,
                "obligation_kind": "standard_parts_assessment",
                "outcome": "not_applicable",
            },
            "assembly_assessment": {
                **common,
                "obligation_kind": "assembly_assessment",
                "outcome": "not_applicable",
            },
        }


def test_job_read_model_closes_simple_part_without_a_fixed_pipeline() -> None:
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.repository = Repository()
    result = service._job_engineering_obligations(
        {
            "job_id": "job-id",
            "active_working_copy_id": "working-id",
            "organization_id": "org",
            "design_group_id": "group",
            "family_id": None,
        }
    )

    assert result["open_obligations"] == []
    assert result["blocked_actions"] == {}
    assert result["allowed_actions"] == ["design_change_mutation_authorize"]
