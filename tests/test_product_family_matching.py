from __future__ import annotations

from types import SimpleNamespace

from mechanical_design_agent.family_matching import match_product_family
from mechanical_design_agent.service import MechanicalDesignService
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


FAMILIES = [
    {
        "family_id": "synthetic-linear-motion",
        "canonical_name": "Linear Motion Modules",
        "aliases": ["linear module", "guided actuator"],
        "status": "ready",
        "discovery_descriptors": [
            "linear guide",
            "ball screw",
            "carriage travel",
        ],
        "products": [
            {
                "canonical_name": "LM-700",
                "aliases": ["LM700"],
                "status": "confirmed",
            }
        ],
    },
    {
        "family_id": "synthetic-vacuum-hardware",
        "canonical_name": "Vacuum Hardware",
        "aliases": ["vacuum vessel"],
        "status": "ready",
        "discovery_descriptors": ["vacuum flange", "sealed chamber"],
        "products": [],
    },
]


def test_no_candidate_keeps_design_unbound() -> None:
    result = match_product_family(
        query="adjustable laptop support with six printed angle positions",
        families=FAMILIES,
    )

    assert result["status"] == "unbound_no_match"
    assert result["binding_family_id"] is None
    assert result["candidates"] == []
    assert result["specialized_knowledge_authorized"] is False
    assert result["next_action"] == "continue_unbound"


def test_exact_approved_alias_is_authoritative() -> None:
    result = match_product_family(
        query="Create a longer linear module for a packaging machine",
        families=FAMILIES,
    )

    assert result["status"] == "authoritative_match"
    assert result["binding_family_id"] == "synthetic-linear-motion"
    assert result["candidates"][0]["match_kind"] == "approved_alias"
    assert result["specialized_knowledge_authorized"] is True
    assert result["next_action"] == "bind_family"


def test_exact_confirmed_product_identifier_is_authoritative() -> None:
    result = match_product_family(
        query="Modify LM700 to add 200 mm of travel",
        families=FAMILIES,
    )

    assert result["status"] == "authoritative_match"
    assert result["binding_family_id"] == "synthetic-linear-motion"
    assert result["candidates"][0]["match_kind"] == "approved_product_identifier"


def test_descriptor_similarity_requires_confirmation() -> None:
    result = match_product_family(
        query="Design a carriage driven by a ball screw on a linear guide",
        families=FAMILIES,
    )

    assert result["status"] == "confirmation_required"
    assert result["binding_family_id"] is None
    assert result["candidates"][0]["family_id"] == "synthetic-linear-motion"
    assert result["candidates"][0]["match_kind"] == "semantic_candidate"
    assert result["specialized_knowledge_authorized"] is False
    assert result["next_action"] == "ask_user"


def test_existing_job_binding_wins_without_reading_other_family() -> None:
    result = match_product_family(
        query="Continue the current design",
        families=FAMILIES,
        bound_family_id="synthetic-linear-motion",
    )

    assert result["status"] == "authoritative_match"
    assert result["binding_family_id"] == "synthetic-linear-motion"
    assert result["candidates"] == [
        {
            "family_id": "synthetic-linear-motion",
            "match_kind": "existing_job_binding",
            "evidence": ["active Design Job is already bound to this family"],
        }
    ]


def test_conflicting_authoritative_binding_does_not_reassign() -> None:
    result = match_product_family(
        query="Continue the current design",
        families=FAMILIES,
        bound_family_id="synthetic-linear-motion",
        source_family_id="synthetic-vacuum-hardware",
    )

    assert result["status"] == "conflict"
    assert result["binding_family_id"] == "synthetic-linear-motion"
    assert result["specialized_knowledge_authorized"] is False
    assert result["next_action"] == "ask_user"


def test_request_text_conflicting_with_existing_job_binding_is_not_ignored() -> None:
    result = match_product_family(
        query="Continue this as vacuum vessel hardware",
        families=FAMILIES,
        bound_family_id="synthetic-linear-motion",
    )

    assert result["status"] == "conflict"
    assert result["binding_family_id"] == "synthetic-linear-motion"
    assert {item["family_id"] for item in result["candidates"]} == {
        "synthetic-linear-motion",
        "synthetic-vacuum-hardware",
    }
    assert result["specialized_knowledge_authorized"] is False


def test_unknown_explicit_family_is_not_silently_created() -> None:
    result = match_product_family(
        query="Use my new family",
        families=FAMILIES,
        explicit_family_id="synthetic-missing",
    )

    assert result["status"] == "conflict"
    assert result["binding_family_id"] is None
    assert result["candidates"] == []
    assert result["next_action"] == "ask_user"


class _InventoryRepository:
    def __init__(self) -> None:
        self.audit_kwargs = None

    def product_family_inventory(self, **kwargs):
        assert kwargs == {
            "organization_id": "org-synthetic",
            "design_group_id": "group-synthetic",
        }
        return [dict(FAMILIES[0])]

    def record_product_family_match(self, **kwargs):
        self.audit_kwargs = kwargs
        return {
            "id": "10000000-0000-4000-8000-000000000001",
            "query_sha256": "a" * 64,
            "created_at": "2026-08-28T00:00:00Z",
        }


def test_service_inventory_is_database_authoritative_without_workspace_config(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-synthetic", dry_run=False)
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.repository = _InventoryRepository()
    service.bootstrap_config = {
        "organization_id": "org-synthetic",
        "design_group_id": "group-synthetic",
        "family_id": None,
    }
    service.settings = SimpleNamespace(
        workspace=workspace,
        actor_id="actor-synthetic",
        family_config_path=None,
    )
    service._require_database = lambda: None

    result = service.product_family_inventory()

    assert result["source"] == "postgresql"
    assert result["families"][0]["family_id"] == "synthetic-linear-motion"
    assert result["families"][0]["database_registered"] is True
    assert result["families"][0]["workspace_configured"] is False
    assert result["families"][0]["selected_for_session"] is False


def test_service_reads_neutral_workspace_identity_without_family_config(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(
        workspace=workspace,
        actor_id="actor-synthetic",
        dry_run=False,
        organization_id="org-synthetic",
        design_group_id="group-synthetic",
    )
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.settings = SimpleNamespace(workspace=workspace, family_config_path=None)

    assert service._read_bootstrap_file() == {
        "organization_id": "org-synthetic",
        "design_group_id": "group-synthetic",
        "family_id": None,
    }


def test_structured_features_only_create_a_semantic_candidate() -> None:
    result = match_product_family(
        query="Design a guided mechanism",
        design_features={"drive": ["ball screw"], "travel_mm": 700},
        families=FAMILIES,
    )

    assert result["status"] == "confirmation_required"
    assert result["candidates"][0]["family_id"] == "synthetic-linear-motion"
    assert result["specialized_knowledge_authorized"] is False


def test_service_match_audits_unbound_semantic_candidate(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-synthetic", dry_run=False)
    repository = _InventoryRepository()
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.repository = repository
    service.bootstrap_config = {
        "organization_id": "org-synthetic",
        "design_group_id": "group-synthetic",
        "family_id": None,
    }
    service.settings = SimpleNamespace(
        workspace=workspace,
        actor_id="actor-synthetic",
        family_config_path=None,
    )
    service._require_database = lambda: None

    result = service.product_family_match(
        query="Design a ball screw carriage",
        design_features={"component_classes": ["carriage"]},
    )

    assert result["status"] == "confirmation_required"
    assert result["binding_family_id"] is None
    assert result["specialized_knowledge_authorized"] is False
    assert result["audit"]["decision_id"].endswith("0001")
    assert repository.audit_kwargs["job_id"] is None
    assert repository.audit_kwargs["working_copy_id"] is None
