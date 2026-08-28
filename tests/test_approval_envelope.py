from __future__ import annotations

from datetime import UTC, datetime
from contextlib import contextmanager
import os
from types import SimpleNamespace
import uuid

import pytest

from mechanical_design_agent.approval_envelope import (
    ApprovalBoundaryError,
    build_approval_envelope,
    build_change_audit_event,
    classify_change_against_envelope,
    require_mutation_authorization,
    validate_approval_envelope_draft,
)
from mechanical_design_agent.repository import PostgresRepository
from mechanical_design_agent.service import MechanicalDesignService


SYNTHETIC_ENVELOPE_DRAFT = {
    "design_intent": {
        "function": "Hold a synthetic calibration plate at discrete angles",
        "operating_sequence": "lift, reposition, engage",
    },
    "architecture": {
        "mechanism": "printed tray with a detent brace",
        "components": ["base", "tray", "brace"],
    },
    "key_interfaces": [
        {"id": "tray-pivot", "contract": "printed revolute interface"},
        {"id": "brace-detent", "contract": "paired indexed support interface"},
    ],
    "user_constraints": [
        {"id": "part-count", "rule": "three printed rigid components"},
        {"id": "angle-range", "rule": "10 to 30 degrees"},
    ],
    "manufacturing_method": {"process": "FDM"},
    "material_constraints": [{"id": "rigid-parts", "rule": "thermoplastic"}],
    "validation_requirements": [
        {"id": "shape-validity", "rule": "all printed parts are valid solids"},
        {"id": "interference", "rule": "no unintended rigid overlap"},
    ],
}


def _active_envelope() -> dict:
    return build_approval_envelope(
        envelope_id="synthetic-envelope-001",
        approval_change_set_id="synthetic-change-001",
        job_id="synthetic-job-001",
        working_copy_id="synthetic-working-copy-001",
        draft=SYNTHETIC_ENVELOPE_DRAFT,
        approval_actor="synthetic-owner",
        approval_text="批准",
        approval_time=datetime(2026, 1, 2, tzinfo=UTC),
        approval_revision=3,
        envelope_revision=1,
    )


def _inside_impact(change_kind: str) -> dict:
    return {
        "change_kind": change_kind,
        "mechanism_changed": False,
        "architecture_changed": False,
        "key_interfaces_changed": [],
        "functional_change": "none",
        "constraint_impacts": [
            {"constraint_id": "part-count", "outcome": "within"},
            {"constraint_id": "angle-range", "outcome": "within"},
        ],
        "manufacturing_process_changed": False,
        "material_constraints_changed": False,
        "standard_part_categories_added": [],
        "validation_requirements_removed": [],
        "boundary_certainty": "inside",
    }


def test_unapproved_first_change_cannot_authorize_cad_mutation() -> None:
    with pytest.raises(ApprovalBoundaryError, match="active approved envelope"):
        require_mutation_authorization(
            {
                "id": "synthetic-change-first",
                "status": "proposed",
                "approval_envelope_id": None,
                "authorization_mode": "human_required",
            },
            active_envelope_id=None,
        )


def test_active_envelope_authorizes_exact_approved_change() -> None:
    require_mutation_authorization(
        {
            "id": "synthetic-change-approved",
            "status": "approved",
            "approval_envelope_id": "synthetic-envelope-001",
            "authorization_mode": "approval_envelope",
        },
        active_envelope_id="synthetic-envelope-001",
    )


def test_superseded_envelope_cannot_authorize_stale_change() -> None:
    with pytest.raises(ApprovalBoundaryError, match="active approved envelope"):
        require_mutation_authorization(
            {
                "id": "synthetic-change-stale",
                "status": "approved",
                "approval_envelope_id": "synthetic-envelope-old",
                "authorization_mode": "approval_envelope",
            },
            active_envelope_id="synthetic-envelope-new",
        )


@pytest.mark.parametrize(
    "change_kind",
    [
        "parameter_optimization",
        "feature_detail",
        "clearance_adjustment",
        "interference_repair",
        "geometry_validity_repair",
        "validation_repair",
        "implementation_refinement",
    ],
)
def test_routine_engineering_iterations_are_authorized_after_approval(
    change_kind: str,
) -> None:
    decision = classify_change_against_envelope(
        _active_envelope(),
        _inside_impact(change_kind),
    )

    assert decision["status"] == "within_envelope"
    assert decision["requires_human_approval"] is False
    assert decision["reasons"] == []


def test_parameter_percentage_is_not_a_materiality_rule() -> None:
    impact = _inside_impact("parameter_optimization")
    impact["reported_percentage_delta"] = 75

    decision = classify_change_against_envelope(_active_envelope(), impact)

    assert decision["status"] == "within_envelope"


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"mechanism_changed": True}, "mechanism_changed"),
        ({"architecture_changed": True}, "architecture_changed"),
        (
            {"key_interfaces_changed": ["tray-pivot"]},
            "key_interface_changed",
        ),
        (
            {"manufacturing_process_changed": True},
            "manufacturing_process_changed",
        ),
        (
            {"material_constraints_changed": True},
            "material_constraints_changed",
        ),
        (
            {"standard_part_categories_added": ["bearing"]},
            "standard_part_category_added",
        ),
        (
            {"validation_requirements_removed": ["interference"]},
            "validation_requirement_removed",
        ),
    ],
)
def test_material_design_intent_changes_require_new_approval(
    patch: dict, reason: str
) -> None:
    impact = _inside_impact("implementation_refinement")
    impact.update(patch)

    decision = classify_change_against_envelope(_active_envelope(), impact)

    assert decision["status"] == "requires_human_approval"
    assert decision["requires_human_approval"] is True
    assert reason in decision["reasons"]


def test_constraint_boundary_exceeded_requires_new_approval() -> None:
    impact = _inside_impact("parameter_optimization")
    impact["constraint_impacts"] = [
        {"constraint_id": "angle-range", "outcome": "exceeds"}
    ]

    decision = classify_change_against_envelope(_active_envelope(), impact)

    assert "approved_constraint_exceeded" in decision["reasons"]


@pytest.mark.parametrize("certainty", ["outside", "ambiguous"])
def test_unknown_or_outside_boundary_fails_closed(certainty: str) -> None:
    impact = _inside_impact("implementation_refinement")
    impact["boundary_certainty"] = certainty

    decision = classify_change_against_envelope(_active_envelope(), impact)

    assert decision["status"] == "requires_human_approval"
    assert "boundary_not_reliably_inside" in decision["reasons"]


def test_incomplete_impact_declaration_fails_closed() -> None:
    decision = classify_change_against_envelope(
        _active_envelope(),
        {"change_kind": "parameter_optimization"},
    )

    assert decision["status"] == "requires_human_approval"
    assert "incomplete_semantic_impact" in decision["reasons"]


def test_missing_approved_constraint_impact_fails_closed() -> None:
    impact = _inside_impact("parameter_optimization")
    impact["constraint_impacts"] = [
        {"constraint_id": "angle-range", "outcome": "within"}
    ]

    decision = classify_change_against_envelope(_active_envelope(), impact)

    assert decision["status"] == "requires_human_approval"
    assert "incomplete_semantic_impact" in decision["reasons"]


def test_malformed_semantic_types_fail_closed() -> None:
    impact = _inside_impact("parameter_optimization")
    impact["mechanism_changed"] = "false"

    decision = classify_change_against_envelope(_active_envelope(), impact)

    assert decision["status"] == "requires_human_approval"
    assert decision["reasons"] == ["incomplete_semantic_impact"]


def test_envelope_requires_all_governed_design_intent_sections() -> None:
    draft = dict(SYNTHETIC_ENVELOPE_DRAFT)
    draft.pop("key_interfaces")

    with pytest.raises(ValueError, match="key_interfaces"):
        validate_approval_envelope_draft(draft)


def test_envelope_records_approval_identity_and_revision() -> None:
    envelope = _active_envelope()

    assert envelope["approval_change_set_id"] == "synthetic-change-001"
    assert envelope["job_id"] == "synthetic-job-001"
    assert envelope["working_copy_id"] == "synthetic-working-copy-001"
    assert envelope["approved_by"] == "synthetic-owner"
    assert envelope["approved_at"] == "2026-01-02T00:00:00+00:00"
    assert envelope["approval_revision"] == 3
    assert envelope["envelope_revision"] == 1
    assert envelope["status"] == "active"


def test_autonomous_change_audit_event_is_complete() -> None:
    decision = classify_change_against_envelope(
        _active_envelope(),
        _inside_impact("interference_repair"),
    )
    event = build_change_audit_event(
        event_id="synthetic-event-001",
        change_set_id="synthetic-change-002",
        envelope_id="synthetic-envelope-001",
        event_type="autonomous_authorized",
        actor_id="synthetic-agent",
        decision=decision,
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert event == {
        "id": "synthetic-event-001",
        "change_set_id": "synthetic-change-002",
        "approval_envelope_id": "synthetic-envelope-001",
        "event_type": "autonomous_authorized",
        "actor_id": "synthetic-agent",
        "decision": decision,
        "created_at": "2026-01-03T00:00:00+00:00",
    }


def test_synthetic_unit_flow_has_no_filesystem_or_product_family_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unit approval classification must not access files")

    monkeypatch.setattr("pathlib.Path.open", forbidden)
    monkeypatch.setattr("pathlib.Path.read_text", forbidden)
    monkeypatch.setattr("pathlib.Path.write_text", forbidden)

    decision = classify_change_against_envelope(
        _active_envelope(),
        _inside_impact("validation_repair"),
    )

    assert decision["status"] == "within_envelope"
    assert "family_id" not in decision


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _SyntheticChangeConnection:
    def __init__(self, active_envelope: dict | None) -> None:
        self.active_envelope = active_envelope
        self.inserted_change: dict | None = None
        self.audit_events: list[dict] = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, parameters=()):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT w.*,m.product_id"):
            return _Rows(
                [
                    {
                        "id": "synthetic-working-copy-001",
                        "job_id": "synthetic-job-001",
                        "source_model_revision_id": None,
                        "product_id": None,
                        "family_id": None,
                        "design_group_id": "synthetic-group",
                    }
                ]
            )
        if normalized.startswith("SELECT * FROM design_retrieval_receipts"):
            return _Rows(
                [{"retrieval_status": "completed_no_match", "used_knowledge_ids": []}]
            )
        if normalized.startswith("SELECT * FROM design_approval_envelopes"):
            return _Rows([self.active_envelope] if self.active_envelope else [])
        if normalized.startswith("INSERT INTO design_change_sets"):
            self.inserted_change = {
                "id": "synthetic-change-recorded",
                "working_copy_id": parameters[0],
                "status": parameters[1],
                "approval_envelope_id": parameters[7],
                "authorization_mode": parameters[11],
                "requires_human_approval": parameters[12],
            }
            return _Rows([self.inserted_change])
        if normalized.startswith("INSERT INTO design_change_audit_events"):
            self.audit_events.append(
                {
                    "change_set_id": parameters[0],
                    "approval_envelope_id": parameters[1],
                    "event_type": parameters[2],
                    "actor_id": parameters[3],
                }
            )
            return _Rows()
        return _Rows()


def _record_with_connection(
    active_envelope: dict | None,
    *,
    draft: dict | None = None,
    semantic_impact: dict | None = None,
) -> tuple[dict, _SyntheticChangeConnection]:
    connection = _SyntheticChangeConnection(active_envelope)
    repository = PostgresRepository("postgresql://synthetic-unused")

    @contextmanager
    def synthetic_connection():
        yield connection

    repository.connection = synthetic_connection
    change = repository.record_change_set(
        "synthetic-working-copy-001",
        "design_proposal" if draft else "parameter_change",
        [{"target": "synthetic-feature", "operation": "adjust"}],
        [],
        "synthetic approval-envelope test",
        "synthetic-agent",
        approval_envelope_draft=draft,
        semantic_impact=semantic_impact,
    )
    return change, connection


def test_initial_design_intent_is_recorded_pending_human_approval() -> None:
    change, connection = _record_with_connection(
        None, draft=SYNTHETIC_ENVELOPE_DRAFT
    )

    assert change["status"] == "proposed"
    assert change["requires_human_approval"] is True
    assert connection.audit_events[0]["event_type"] == "human_approval_required"


def test_repository_authorizes_and_audits_in_envelope_iteration() -> None:
    change, connection = _record_with_connection(
        _active_envelope(), semantic_impact=_inside_impact("interference_repair")
    )

    assert change["status"] == "approved"
    assert change["authorization_mode"] == "approval_envelope"
    assert change["approval_envelope_id"] == "synthetic-envelope-001"
    assert connection.audit_events == [
        {
            "change_set_id": "synthetic-change-recorded",
            "approval_envelope_id": "synthetic-envelope-001",
            "event_type": "autonomous_authorized",
            "actor_id": "synthetic-agent",
        }
    ]


def test_repository_keeps_ambiguous_iteration_pending_and_audited() -> None:
    impact = _inside_impact("implementation_refinement")
    impact["boundary_certainty"] = "ambiguous"

    change, connection = _record_with_connection(
        _active_envelope(), semantic_impact=impact
    )

    assert change["status"] == "proposed"
    assert change["requires_human_approval"] is True
    assert connection.audit_events[0]["event_type"] == "boundary_fail_closed"


class _SyntheticReviewRepository:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def review_change_set(self, *args):
        self.calls.append(args)
        return {"id": args[0], "status": "approved" if args[1] == "approve" else "rejected"}

    def authorize_change_mutation(self, change_set_id, actor_id):
        self.calls.append((change_set_id, actor_id))
        return {"id": change_set_id, "status": "approved"}


def _synthetic_service(repository: _SyntheticReviewRepository) -> MechanicalDesignService:
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.repository = repository
    service.settings = SimpleNamespace(actor_id="synthetic-owner")
    service._require_database = lambda: None
    return service


def test_user_can_approve_without_copying_internal_uuid() -> None:
    repository = _SyntheticReviewRepository()
    result = _synthetic_service(repository).design_change_review(
        "synthetic-internal-change-id", "approve", "synthetic design intent", "批准"
    )

    assert result["status"] == "approved"
    assert repository.calls[0][:2] == (
        "synthetic-internal-change-id",
        "approve",
    )


def test_user_can_request_revised_proposal_without_copying_internal_uuid() -> None:
    repository = _SyntheticReviewRepository()
    result = _synthetic_service(repository).design_change_review(
        "synthetic-internal-change-id", "reject", "revise mechanism", "修改方案"
    )

    assert result["status"] == "rejected"


def test_pre_mutation_service_gate_uses_internal_change_identity() -> None:
    repository = _SyntheticReviewRepository()
    result = _synthetic_service(repository).design_change_mutation_authorize(
        "synthetic-internal-change-id"
    )

    assert result["status"] == "approved"
    assert repository.calls == [
        ("synthetic-internal-change-id", "synthetic-owner")
    ]


@pytest.mark.skipif(
    not os.environ.get("MECH_DESIGN_APPROVAL_ENVELOPE_LIVE_DATABASE_URL"),
    reason="isolated synthetic PostgreSQL approval-envelope test is not configured",
)
def test_synthetic_postgres_approval_envelope_flow() -> None:
    from mechanical_design_agent.migrations import postgres_migrations_directory

    database_url = os.environ["MECH_DESIGN_APPROVAL_ENVELOPE_LIVE_DATABASE_URL"]
    repository = PostgresRepository(database_url)
    with postgres_migrations_directory() as migrations:
        repository.apply_migrations(migrations)

    token = uuid.uuid4().hex
    organization_id = f"synthetic-org-{token}"
    design_group_id = f"synthetic-group-{token}"
    actor_id = f"synthetic-owner-{token}"
    job_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    working_copy_id = str(uuid.uuid4())
    with repository.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO organizations(id,name) VALUES (%s,%s)",
            (organization_id, "Synthetic approval test organization"),
        )
        connection.execute(
            "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
            (design_group_id, organization_id, "Synthetic approval test group"),
        )
        connection.execute(
            "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s)",
            (actor_id, organization_id, "Synthetic owner", "family_owner"),
        )
        connection.execute(
            "INSERT INTO design_jobs("
            "id,workspace_id,display_id,job_type,title,slug,status,phase,revision,"
            "organization_id,design_group_id,family_id,directory_name,idempotency_token,"
            "provisioning_state,created_by) "
            "VALUES (%s,%s,%s,'mechanical_design',%s,%s,'active','design',4,%s,%s,NULL,%s,%s,'ready',%s)",
            (
                job_id,
                workspace_id,
                f"SYN-{token[:8]}",
                "Synthetic approval envelope job",
                f"synthetic-approval-{token}",
                organization_id,
                design_group_id,
                f"synthetic-approval-{token}",
                f"synthetic-approval-{token}",
                actor_id,
            ),
        )
        connection.execute(
            "INSERT INTO design_working_copies("
            "id,organization_id,design_group_id,family_id,source_model_revision_id,"
            "source_sha256,source_kind,working_path,status,created_by,design_origin,job_id,"
            "source_snapshot_id,bound_job_revision,working_sha256,working_size_bytes,"
            "working_relative_path) "
            "VALUES (%s,%s,%s,NULL,NULL,%s,'new_design_seed',%s,'draft',%s,'new_design',%s,"
            "NULL,4,%s,1,%s)",
            (
                working_copy_id,
                organization_id,
                design_group_id,
                "a" * 64,
                "/synthetic/approval-envelope.FCStd",
                actor_id,
                job_id,
                "b" * 64,
                f"models/working/{working_copy_id}/working.FCStd",
            ),
        )
        connection.execute(
            "INSERT INTO design_retrieval_receipts("
            "working_copy_id,design_origin,source_model_revision_id,family_id,query,"
            "retrieval_scope,retrieved_knowledge_ids,used_knowledge_ids,retrieval_status,"
            "non_use_reason,created_by) "
            "VALUES (%s,'new_design',NULL,NULL,%s,'{}'::jsonb,'[]'::jsonb,'[]'::jsonb,"
            "'completed_no_match',%s,%s)",
            (
                working_copy_id,
                "synthetic approval envelope retrieval",
                "synthetic test has no approved knowledge matches",
                actor_id,
            ),
        )

    proposal = repository.record_change_set(
        working_copy_id,
        "design_proposal",
        [{"target": "synthetic-assembly", "operation": "create"}],
        [],
        "synthetic initial design intent",
        actor_id,
        approval_envelope_draft=SYNTHETIC_ENVELOPE_DRAFT,
    )
    assert proposal["status"] == "proposed"

    approved = repository.review_change_set(
        str(proposal["id"]),
        "approve",
        actor_id,
        "Synthetic design intent approved",
        "批准",
    )
    envelope = repository.get_active_approval_envelope(working_copy_id)
    assert envelope is not None
    assert approved["approval_envelope_id"] == envelope["id"]
    assert envelope["job_id"] == uuid.UUID(job_id)
    assert envelope["approval_revision"] == 4
    assert "family_id" not in envelope
    repository.authorize_change_mutation(str(proposal["id"]), actor_id)

    autonomous = repository.record_change_set(
        working_copy_id,
        "parameter_change",
        [{"target": "synthetic-clearance", "operation": "increase"}],
        [],
        "synthetic interference repair",
        actor_id,
        semantic_impact=_inside_impact("interference_repair"),
    )
    assert autonomous["status"] == "approved"
    assert autonomous["reviewed_by"] is None
    assert autonomous["authorization_mode"] == "approval_envelope"
    repository.authorize_change_mutation(str(autonomous["id"]), actor_id)

    events = repository.list_change_audit_events(str(autonomous["id"]))
    assert [event["event_type"] for event in events] == [
        "autonomous_authorized",
        "mutation_authorized",
    ]

    successor_draft = {
        **SYNTHETIC_ENVELOPE_DRAFT,
        "architecture": {
            "mechanism": "synthetic printed tray with a sliding brace",
            "components": ["base", "tray", "sliding-brace"],
        },
    }
    successor = repository.record_change_set(
        working_copy_id,
        "design_proposal",
        [{"target": "synthetic-assembly", "operation": "change-mechanism"}],
        [],
        "synthetic material mechanism change",
        actor_id,
        approval_envelope_draft=successor_draft,
    )
    assert successor["status"] == "proposed"
    successor_approved = repository.review_change_set(
        str(successor["id"]),
        "approve",
        actor_id,
        "Synthetic successor intent approved",
        "批准",
    )
    successor_envelope = repository.get_active_approval_envelope(working_copy_id)
    assert successor_envelope is not None
    assert successor_envelope["id"] == successor_approved["approval_envelope_id"]
    assert successor_envelope["envelope_revision"] == 2
    with repository.connection() as connection:
        old_envelope = connection.execute(
            "SELECT status,superseded_by FROM design_approval_envelopes WHERE id=%s",
            (envelope["id"],),
        ).fetchone()
    assert old_envelope["status"] == "superseded"
    assert old_envelope["superseded_by"] == successor_envelope["id"]
    with pytest.raises(ApprovalBoundaryError, match="active approved envelope"):
        repository.authorize_change_mutation(str(autonomous["id"]), actor_id)
