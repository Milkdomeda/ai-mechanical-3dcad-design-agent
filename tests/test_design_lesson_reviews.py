from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
import pytest

from mechanical_design_agent.artifacts import ArtifactStore
from mechanical_design_agent.config import Settings
from mechanical_design_agent.design_lessons import DesignLessonStagingStore
from mechanical_design_agent.hashing import file_sha256
from mechanical_design_agent.lesson_reviews import DesignLessonReviewStore
from mechanical_design_agent.jobs import JobFailure
from mechanical_design_agent.repository import PostgresRepository
from mechanical_design_agent.secure_fs import relative_managed_path
from mechanical_design_agent.server import create_mcp
from mechanical_design_agent.service import MechanicalDesignService


LIVE_DATABASE_URL = os.environ.get("MECH_DESIGN_DATABASE_URL", "").strip()
LIVE_NEO4J_CONFIGURED = all(
    os.environ.get(name, "").strip()
    for name in (
        "MECH_DESIGN_NEO4J_URI",
        "MECH_DESIGN_NEO4J_USER",
        "MECH_DESIGN_NEO4J_PASSWORD",
    )
)


class _Rows:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]

    def fetchone(self):
        return dict(self.rows[0]) if self.rows else None

    def fetchall(self):
        return [dict(row) for row in self.rows]


class _TrackedConnection:
    def __init__(self) -> None:
        self.actors = {
            "engineer-001": {
                "id": "engineer-001",
                "organization_id": "org-001",
                "role": "family_owner",
            },
            "outsider-001": {
                "id": "outsider-001",
                "organization_id": "org-other",
                "role": "family_owner",
            },
            "engineer-role-001": {
                "id": "engineer-role-001",
                "organization_id": "org-001",
                "role": "engineer",
            },
        }
        self.design_groups: dict[str, dict] = {
            "group-001": {"id": "group-001", "organization_id": "org-001"}
        }
        self.reviews: dict[str, dict] = {}
        self.working_copies: dict[str, dict] = {
            "working-001": {
                "id": "working-001",
                "job_id": "job-001",
                "organization_id": "org-001",
                "design_group_id": "group-001",
                "status": "approved_for_delivery",
                "approved_final_sha256": "6" * 64,
                "approved_final_artifact_path": "/artifacts/approved-final.FCStd",
            }
        }
        self.lesson_summaries: dict[str, dict] = {
            "working-001": {
                "id": "summary-001",
            "working_copy_id": "working-001",
            "job_id": "job-001",
                "summary_status": "completed",
                "publication_status": "blocked",
                "publication_blocker": "working copy is not approved_for_delivery",
            }
        }
        self.change_sets: list[dict] = []
        self.validation_reports: list[dict] = []
        self.standard_parts: list[dict] = []
        self.queries: list[tuple[str, tuple]] = []
        self.transaction_events: list[str] = []
        self.outbox_events: list[dict] = []
        self.fail_on_review_insert = False

    @contextmanager
    def transaction(self):
        reviews_before = copy.deepcopy(self.reviews)
        summaries_before = copy.deepcopy(self.lesson_summaries)
        outbox_before = copy.deepcopy(self.outbox_events)
        self.transaction_events.append("begin")
        try:
            yield
        except Exception:
            self.reviews = reviews_before
            self.lesson_summaries = summaries_before
            self.outbox_events = outbox_before
            self.transaction_events.append("rollback")
            raise
        else:
            self.transaction_events.append("commit")

    def execute(self, query, parameters=()):
        sql = " ".join(query.split())
        parameters = tuple(parameters)
        self.queries.append((sql, parameters))

        if sql.startswith(
            "SELECT id::text AS event_id,event_type,aggregate_type,aggregate_id FROM outbox_events"
        ):
            lesson_id, review_id = map(str, parameters)
            expected = {
                ("design_lesson.approved", "design_lesson", lesson_id),
                (
                    "design_lesson_review.approved",
                    "design_lesson_review",
                    review_id,
                ),
            }
            return _Rows(
                {
                    "event_id": str(event["id"]),
                    "event_type": event["event_type"],
                    "aggregate_type": event["aggregate_type"],
                    "aggregate_id": event["aggregate_id"],
                }
                for event in self.outbox_events
                if event.get("processed_at") is not None
                and (
                    event["event_type"],
                    event["aggregate_type"],
                    event["aggregate_id"],
                )
                in expected
            )

        if sql.startswith("SELECT r.*,r.id::text AS review_id"):
            rows = []
            for review in self.reviews.values():
                versions = [
                    int(event.get("aggregate_version", 0))
                    for event in self.outbox_events
                    if event.get("aggregate_type") == "design_lesson_review"
                    and event.get("aggregate_id") == str(review["id"])
                ]
                rows.append(
                    {
                        **review,
                        "review_id": str(review["id"]),
                        "occurred_at": review.get("retrieval_verified_at")
                        or review.get("reviewed_at")
                        or review.get("created_at"),
                        "aggregate_version": max(versions, default=0),
                    }
                )
            return _Rows(rows)

        if sql.startswith("SELECT * FROM actors WHERE id=%s FOR UPDATE"):
            actor = self.actors.get(str(parameters[0]))
            return _Rows([actor] if actor else [])
        if sql == "SELECT * FROM actors WHERE id=%s":
            actor = self.actors.get(str(parameters[0]))
            return _Rows([actor] if actor else [])
        if sql.startswith("SELECT * FROM design_working_copies WHERE id=%s FOR UPDATE"):
            working_copy = self.working_copies.get(str(parameters[0]))
            return _Rows([working_copy] if working_copy else [])
        if sql.startswith("SELECT job_id FROM design_working_copies WHERE id=%s"):
            working_copy = self.working_copies.get(str(parameters[0]))
            return _Rows(
                [{"job_id": working_copy["job_id"]}] if working_copy else []
            )
        if sql.startswith("SELECT * FROM design_groups WHERE id=%s FOR UPDATE"):
            design_group = self.design_groups.get(str(parameters[0]))
            return _Rows([design_group] if design_group else [])
        if sql == "SELECT * FROM design_groups WHERE id=%s":
            design_group = self.design_groups.get(str(parameters[0]))
            return _Rows([design_group] if design_group else [])
        if sql == "SELECT * FROM design_working_copies WHERE id=%s":
            working_copy = self.working_copies.get(str(parameters[0]))
            return _Rows([working_copy] if working_copy else [])
        if sql.startswith("SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE"):
            review = self.reviews.get(str(parameters[0]))
            return _Rows([review] if review else [])
        if sql.startswith(
            "SELECT r.*,w.job_id FROM design_lesson_reviews r JOIN design_working_copies w"
        ):
            review = self.reviews.get(str(parameters[0]))
            if review is None:
                return _Rows()
            working = self.working_copies[str(review["working_copy_id"])]
            return _Rows([{**review, "job_id": working["job_id"]}])
        if sql == "SELECT * FROM design_lesson_reviews WHERE id=%s":
            review = self.reviews.get(str(parameters[0]))
            return _Rows([review] if review else [])
        if sql.startswith("INSERT INTO design_lesson_reviews"):
            if self.fail_on_review_insert:
                raise RuntimeError("injected review insert failure")
            (
                review_id,
                organization_id,
                design_group_id,
                working_copy_id,
                lesson_id,
                package_sha256,
                review_card_sha256,
                final_model_sha256,
                review_path,
                package_path,
                actor_id,
                supersedes_review_id,
                approved_final_artifact_path,
            ) = parameters
            row = {
                "id": review_id,
                "organization_id": organization_id,
                "design_group_id": design_group_id,
                "working_copy_id": working_copy_id,
                "lesson_id": lesson_id,
                "package_sha256": package_sha256,
                "review_card_sha256": review_card_sha256,
                "final_model_sha256": final_model_sha256,
                "approved_final_artifact_path": approved_final_artifact_path,
                "status": "awaiting-engineer-review",
                "review_path": review_path,
                "package_path": package_path,
                "created_by": actor_id,
                "supersedes_review_id": supersedes_review_id,
            }
            self.reviews[str(review_id)] = row
            return _Rows([row])
        if sql.startswith("UPDATE design_lesson_reviews SET status='superseded'"):
            review = self.reviews.get(str(parameters[0]))
            if review is None or review["status"] != "awaiting-engineer-review":
                return _Rows()
            review["status"] = "superseded"
            return _Rows([review])
        if sql.startswith("UPDATE design_lesson_reviews SET status='rejected'"):
            reviewer_id, reviewer_text, review_id = parameters
            review = self.reviews.get(str(review_id))
            if review is None or review["status"] != "awaiting-engineer-review":
                return _Rows()
            review.update(
                status="rejected", reviewed_by=reviewer_id, reviewer_text=reviewer_text
            )
            return _Rows([review])
        if sql.startswith("UPDATE design_lesson_reviews SET status='invalid'"):
            reviewer_id, reviewer_text, review_id = parameters
            review = self.reviews.get(str(review_id))
            if review is None or review["status"] != "awaiting-engineer-review":
                return _Rows()
            review.update(
                status="invalid",
                reviewed_by=reviewer_id,
                reviewer_text=reviewer_text,
            )
            return _Rows([review])
        if sql.startswith("UPDATE design_lesson_reviews SET retrieval_probe=%s::jsonb,status='stored-and-retrievable'"):
            probe, review_id = parameters
            review = self.reviews.get(str(review_id))
            if review is None or review["status"] != "approved-retrieval-pending":
                return _Rows()
            review.update(status="stored-and-retrievable", retrieval_probe=probe)
            return _Rows([review])
        if sql.startswith("UPDATE design_lesson_reviews SET retrieval_probe=%s::jsonb"):
            probe, review_id = parameters
            review = self.reviews.get(str(review_id))
            if review is None or review["status"] != "approved-retrieval-pending":
                return _Rows()
            review["retrieval_probe"] = probe
            return _Rows([review])
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return _Rows([{}])
        if "COALESCE(max(aggregate_version),0)+1" in sql:
            return _Rows([{"aggregate_version": 1}])
        if sql.startswith("INSERT INTO outbox_events"):
            self.outbox_events.append(
                {
                    "aggregate_type": parameters[0],
                    "aggregate_id": parameters[1],
                    "event_type": parameters[2],
                    "payload": parameters[3],
                }
            )
            return _Rows()
        if sql.startswith("SELECT * FROM design_working_copies WHERE id=%s AND organization_id=%s"):
            working = self.working_copies.get(str(parameters[0]))
            if (
                working is None
                or working["status"] != "approved_for_delivery"
                or working.get("organization_id") != str(parameters[1])
                or working.get("design_group_id") != str(parameters[2])
            ):
                return _Rows()
            return _Rows([working])
        if sql.startswith("SELECT * FROM design_change_sets WHERE working_copy_id=%s AND status='applied'"):
            rows = [
                row
                for row in self.change_sets
                if str(row["working_copy_id"]) == str(parameters[0])
                and row["status"] == "applied"
            ]
            return _Rows(sorted(rows, key=lambda row: (row["created_at"], row["id"])))
        if sql.startswith("SELECT * FROM validation_reports WHERE working_copy_id=%s"):
            rows = [
                row
                for row in self.validation_reports
                if str(row["working_copy_id"]) == str(parameters[0])
            ]
            return _Rows(sorted(rows, key=lambda row: (row["created_at"], row["id"])))
        if sql.startswith("SELECT count(*) AS count FROM design_change_sets"):
            return _Rows([{"count": 0}])
        if sql.startswith("SELECT * FROM design_lesson_summaries WHERE working_copy_id=%s"):
            summary = self.lesson_summaries.get(str(parameters[0]))
            return _Rows([summary] if summary else [])
        if sql.startswith("UPDATE design_lesson_summaries SET publication_status='ready'"):
            summary_id = str(parameters[0])
            summary = next(
                (
                    item
                    for item in self.lesson_summaries.values()
                    if str(item["id"]) == summary_id
                ),
                None,
            )
            if summary is None:
                return _Rows()
            summary.update(publication_status="ready", publication_blocker=None)
            return _Rows([summary])
        if sql.startswith("SELECT DISTINCT ON (validation_kind)"):
            current_sha256 = "a" * 64
            return _Rows([
                {
                    "validation_kind": "geometry_model",
                    "status": "passed",
                    "working_sha256": current_sha256,
                },
                {
                    "validation_kind": "assembly_completeness",
                    "status": "passed",
                    "working_sha256": current_sha256,
                },
            ])
        if sql.startswith("UPDATE design_working_copies SET status='approved_for_delivery',approved_final_sha256=%s"):
            current_sha256, artifact_path, working_copy_id = parameters
            working = self.working_copies.get(str(working_copy_id))
            if working is None:
                return _Rows()
            working.update(
                status="approved_for_delivery",
                approved_final_sha256=str(current_sha256),
                approved_final_artifact_path=str(artifact_path),
            )
            return _Rows([working])
        if sql.startswith("SELECT * FROM standard_part_records WHERE metadata->>'working_copy_id'=%s"):
            rows = [
                row
                for row in self.standard_parts
                if row.get("metadata", {}).get("working_copy_id") == str(parameters[0])
                and row.get("metadata", {}).get("model_sha256") == str(parameters[1])
            ]
            return _Rows(
                sorted(rows, key=lambda row: (row["provider_id"], row["part_number"], row["id"]))
            )
        raise AssertionError(f"unexpected SQL: {sql}")


@contextmanager
def _repository_with(connection: _TrackedConnection):
    repository = PostgresRepository("postgresql://unused")

    @contextmanager
    def fake_connection():
        yield connection

    repository.connection = fake_connection
    yield repository


def _review(review_id: str, *, status: str = "awaiting-engineer-review") -> dict:
    return {
        "id": review_id,
        "organization_id": "org-001",
        "design_group_id": "group-001",
        "working_copy_id": "working-001",
        "job_id": "job-001",
        "lesson_id": "DL-001",
        "package_sha256": "1" * 64,
        "review_card_sha256": "2" * 64,
        "final_model_sha256": "3" * 64,
        "approved_final_artifact_path": "/artifacts/approved-final.FCStd",
        "status": status,
        "review_path": f"/reviews/{review_id}/review.md",
        "package_path": f"/staging/{review_id}/package.json",
        "created_by": "engineer-001",
        "supersedes_review_id": None,
    }


def _create(repository: PostgresRepository, **overrides):
    values = {
        "review_id": "DLR-new-001",
        "organization_id": "org-001",
        "design_group_id": "group-001",
        "working_copy_id": "working-001",
        "lesson_id": "DL-001",
        "package_sha256": "4" * 64,
        "review_card_sha256": "5" * 64,
        "final_model_sha256": "6" * 64,
        "approved_final_artifact_path": "/artifacts/approved-final.FCStd",
        "review_path": "/reviews/DLR-new-001/review.md",
        "package_path": "/staging/DLR-new-001/package.json",
        "actor_id": "engineer-001",
        "supersedes_review_id": None,
    }
    values.update(overrides)
    return repository.create_design_lesson_review(**values)


def _review_package() -> dict:
    return {
        "schema_version": "DesignLessonPackage/v1",
        "lesson_id": "DL-REVIEW-001",
        "title": "Verify actuator mounting clearance",
        "codex_session_id": "session-001",
        "source": {
            "organization_id": "org-001",
            "design_group_id": "group-001",
            "family_id": "family-001",
            "working_copy_id": "00000000-0000-0000-0000-000000000011",
            "change_set_ids": ["00000000-0000-0000-0000-000000000012"],
            "before_model_sha256": "1" * 64,
            "after_model_sha256": "2" * 64,
        },
        "problem": {
            "summary": "Mounting clearance was missed",
            "discovery_stage": "assembly-validation",
            "severity": "major",
            "symptoms": ["interference"],
            "affected_components": ["actuator"],
            "affected_interfaces": ["mount"],
            "failure_modes": ["interference"],
        },
        "root_causes": ["Clearance check was omitted"],
        "corrections": ["Add the clearance check"],
        "prevention": {
            "required_checks": ["Validate before release"],
            "design_review_questions": ["Is mounting clearance verified?"],
            "workflow_gate": "validation-before-release",
            "detection_method": "clearance validation",
        },
        "applicability": {
            "component_classes": ["actuator"],
            "interface_types": ["mount"],
            "design_stages": ["detail"],
            "required_conditions": ["moving-assembly"],
        },
        "non_applicable_conditions": [],
        "search_terms": ["actuator clearance", "mount clearance"],
        "atomic_assertions": [
            {
                "assertion_key": "actuator-clearance",
                "subject_ref": "component:actuator",
                "predicate": "requires-clearance",
                "object_value": {"minimum_mm": 2},
                "constraint_kind": "hard_constraint",
                "evidence_refs": ["validation-evidence"],
            },
            {
                "assertion_key": "mount-alignment",
                "subject_ref": "interface:mount",
                "predicate": "requires-alignment",
                "object_value": {"maximum_mm": 0.1},
                "constraint_kind": "check",
                "evidence_refs": ["validation-evidence"],
            },
            {
                "assertion_key": "mount-inspection",
                "subject_ref": "interface:mount",
                "predicate": "requires-inspection",
                "object_value": {"stage": "assembly"},
                "constraint_kind": "check",
                "evidence_refs": ["validation-evidence"],
            },
        ],
        "evidence_manifest": [
            {
                "evidence_id": "validation-evidence",
                "path": "evidence.json",
                "role": "geometry_validation",
                "media_type": "application/json",
                "sha256": "3" * 64,
                "working_copy_id": "00000000-0000-0000-0000-000000000011",
                "change_set_id": "00000000-0000-0000-0000-000000000012",
                "model_sha256": "2" * 64,
                "validation_kind": "geometry_model",
            }
        ],
    }


def _staged_review_inspection(workspace: Path) -> dict:
    (workspace / "evidence.json").write_text("{}", encoding="utf-8")
    staging = DesignLessonStagingStore(workspace)
    staging.stage(
        _review_package(),
        [{
            "evidence_id": "validation-evidence",
            "path": "evidence.json",
            "role": "geometry_validation",
            "media_type": "application/json",
            "working_copy_id": "00000000-0000-0000-0000-000000000011",
            "change_set_id": "00000000-0000-0000-0000-000000000012",
            "model_sha256": "2" * 64,
            "validation_kind": "geometry_model",
        }],
    )
    return staging.get("DL-REVIEW-001")


class _ReviewPreparationRepository:
    def __init__(self, context: dict) -> None:
        self.context = context
        self.create_calls: list[dict] = []
        self.reviews: dict[str, dict] = {}
        self.fail_next_insert = False
        self.required_lock_owner = None
        self.pre_commit_hook = None
        self.approve_calls: list[dict] = []
        self.public_calls: list[str] = []
        self.approved_assertion_count = 0
        self.lessons: dict[str, dict] = {}
        self.search_error: Exception | None = None
        self.probe_error: Exception | None = None
        self.probe_calls: list[dict] = []
        self.reject_calls: list[dict] = []
        self.invalidate_calls: list[dict] = []
        self.processed_projection_events: list[dict] = []

    def design_lesson_review_context(self, working_copy_id: str, **_scope) -> dict:
        if working_copy_id != str(self.context["working_copy"]["id"]):
            raise KeyError(f"unknown working_copy_id: {working_copy_id}")
        return copy.deepcopy(self.context)

    def get_working_copy(self, working_copy_id: str) -> dict:
        if working_copy_id != str(self.context["working_copy"]["id"]):
            raise KeyError(f"unknown working_copy_id: {working_copy_id}")
        return copy.deepcopy(self.context["working_copy"])

    def authorize_delivery_approval(self, **scope) -> None:
        working_copy = self.context["working_copy"]
        if (
            str(scope["working_copy_id"]) != str(working_copy["id"])
            or scope["organization_id"] != working_copy["organization_id"]
            or scope["design_group_id"] != working_copy["design_group_id"]
        ):
            raise PermissionError("delivery approval is outside configured scope")

    def create_design_lesson_review(self, **kwargs) -> dict:
        if self.required_lock_owner is not None and not self.required_lock_owner.locked:
            raise AssertionError("working-copy lock was released before repository insertion")
        verifier = kwargs.pop("pre_commit_verifier", None)
        reviews_before = copy.deepcopy(self.reviews)
        calls_before = list(self.create_calls)
        try:
            if self.fail_next_insert:
                self.fail_next_insert = False
                raise RuntimeError("injected review insert failure")
            if any(
                review["package_sha256"] == kwargs["package_sha256"]
                for review in self.reviews.values()
            ):
                raise ValueError("duplicate immutable package binding")
            predecessor_id = kwargs.get("supersedes_review_id")
            if predecessor_id:
                predecessor = self.reviews.get(predecessor_id)
                if predecessor is None:
                    raise KeyError(f"unknown supersedes_review_id: {predecessor_id}")
                if predecessor["status"] != "awaiting-engineer-review":
                    raise ValueError("superseded review must be awaiting-engineer-review")
                predecessor["status"] = "superseded"
            row = {
                **kwargs,
                "id": kwargs["review_id"],
                "job_id": str(self.context["working_copy"]["job_id"]),
                "status": "awaiting-engineer-review",
            }
            self.reviews[kwargs["review_id"]] = row
            if self.pre_commit_hook is not None:
                self.pre_commit_hook(row)
            if verifier is not None:
                verifier()
            self.create_calls.append(dict(kwargs))
            return row
        except Exception:
            self.reviews = reviews_before
            self.create_calls = calls_before
            raise

    def get_design_lesson_review(self, review_id: str) -> dict:
        review = self.reviews.get(review_id)
        if review is None:
            raise KeyError(f"unknown design lesson review: {review_id}")
        return copy.deepcopy(review)

    def approve_design_lesson(self, **kwargs) -> dict:
        self.public_calls.extend(kwargs)
        review_id = kwargs["review_id"]
        review = self.reviews[review_id]
        if review["status"] != "awaiting-engineer-review":
            raise ValueError("design lesson review must be awaiting-engineer-review")
        package = copy.deepcopy(kwargs["package"])
        assert package["lesson_id"] == review["lesson_id"]
        assert kwargs["package_sha256"] == review["package_sha256"]
        if kwargs.get("pre_commit_verifier") is not None:
            kwargs["pre_commit_verifier"]()
        lesson = {
            **package,
            "id": "approved-lesson-001",
            "status": "approved",
            "source_design_group_id": package["source"]["design_group_id"],
        }
        self.lessons[lesson["id"]] = lesson
        review.update(
            status="approved-retrieval-pending",
            published_design_lesson_id=lesson["id"],
            reviewed_by=kwargs["reviewer_id"],
            reviewer_text=kwargs["reviewer_text"],
        )
        self.approved_assertion_count = len(package["atomic_assertions"])
        self.approve_calls.append(dict(kwargs))
        return copy.deepcopy(lesson)

    def get_design_lesson(self, lesson_id: str, *, organization_id: str) -> dict:
        lesson = self.lessons.get(lesson_id)
        if lesson is None or lesson["source"]["organization_id"] != organization_id:
            raise KeyError(f"unknown design lesson: {lesson_id}")
        return copy.deepcopy(lesson)

    def search_approved_design_lessons(self, **kwargs) -> list[dict]:
        if self.search_error is not None:
            raise self.search_error
        query = kwargs["query"].strip().lower()
        return [
            copy.deepcopy(lesson)
            for lesson in self.lessons.values()
            if query in {term.strip().lower() for term in lesson["search_terms"]}
        ]

    def record_design_lesson_review_probe(
        self, *, review_id: str, probe: dict, successful: bool
    ) -> dict:
        self.probe_calls.append(
            {"review_id": review_id, "probe": copy.deepcopy(probe), "successful": successful}
        )
        if self.probe_error is not None:
            raise self.probe_error
        review = self.reviews[review_id]
        if review["status"] != "approved-retrieval-pending":
            raise ValueError("design lesson review must be approved-retrieval-pending")
        review["retrieval_probe"] = copy.deepcopy(probe)
        if successful:
            review["status"] = "stored-and-retrievable"
        return copy.deepcopy(review)

    def processed_design_lesson_review_projection_witnesses(
        self, *, review_id: str, lesson_id: str
    ) -> list[dict]:
        return [
            copy.deepcopy(event)
            for event in self.processed_projection_events
            if (
                event["event_type"],
                event["aggregate_type"],
                event["aggregate_id"],
            )
            in {
                ("design_lesson.approved", "design_lesson", lesson_id),
                (
                    "design_lesson_review.approved",
                    "design_lesson_review",
                    review_id,
                ),
            }
        ]

    def reject_design_lesson_review(
        self, *, review_id: str, reviewer_id: str, reviewer_text: str
    ) -> dict:
        self.reject_calls.append(
            {
                "review_id": review_id,
                "reviewer_id": reviewer_id,
                "reviewer_text": reviewer_text,
            }
        )
        review = self.reviews[review_id]
        if review["status"] != "awaiting-engineer-review":
            raise ValueError("design lesson review must be awaiting-engineer-review")
        review.update(
            status="rejected",
            reviewed_by=reviewer_id,
            reviewer_text=reviewer_text,
        )
        return copy.deepcopy(review)

    def invalidate_design_lesson_review(
        self, *, review_id: str, reviewer_id: str, reason: str
    ) -> dict:
        self.invalidate_calls.append(
            {
                "review_id": review_id,
                "reviewer_id": reviewer_id,
                "reason": reason,
            }
        )
        review = self.reviews[review_id]
        if review["status"] != "awaiting-engineer-review":
            raise ValueError("design lesson review must be awaiting-engineer-review")
        review.update(
            status="invalid",
            reviewed_by=reviewer_id,
            reviewer_text=reason,
        )
        return copy.deepcopy(review)


class _ReviewPreparationWorkspace:
    def __init__(
        self,
        current_sha256: str,
        working_path: Path,
        repository: _ReviewPreparationRepository,
    ) -> None:
        self.current_sha256 = current_sha256
        self.working_path = working_path
        self.repository = repository
        self.locked = False

    def current_hash(self, _working_copy_id: str) -> str:
        return self.current_sha256

    def approve_delivery(
        self,
        working_copy_id: str,
        _actor_id: str,
        confirmation: str,
        artifact_store: ArtifactStore,
        **_scope,
    ) -> dict:
        assert confirmation == f"批准 {working_copy_id}"
        snapshot = artifact_store.ingest_file(
            self.working_path, allowed_root=self.working_path.parent
        )
        working_copy = self.repository.context["working_copy"]
        working_copy.update(
            status="approved_for_delivery",
            approved_final_sha256=snapshot["sha256"],
            approved_final_artifact_path=snapshot["storage_path"],
        )
        return copy.deepcopy(working_copy)

    @contextmanager
    def locked_working_copy_path(self, _working_copy_id: str):
        assert not self.locked
        self.locked = True
        try:
            yield self.working_path.resolve()
        finally:
            self.locked = False

    @contextmanager
    def locked_job_working_copy(self, working_copy_id: str):
        working = self.repository.get_working_copy(working_copy_id)
        assert working.get("job_id")
        with self.locked_working_copy_path(working_copy_id) as path:
            yield (
                self.working_path.parent.resolve(),
                path,
                working,
                {
                    "id": working["job_id"],
                    "revision": 7,
                    "active_working_copy_id": working_copy_id,
                },
            )


def _review_preparation_fixture() -> tuple[
    tempfile.TemporaryDirectory, MechanicalDesignService, dict, list[dict]
]:
    temporary = tempfile.TemporaryDirectory()
    workspace = Path(temporary.name)
    working_copy_id = "00000000-0000-0000-0000-000000000011"
    change_set_id = "00000000-0000-0000-0000-000000000012"
    working_path = workspace / "final.working.FCStd"
    working_path.write_bytes(b"approved-final-model")
    final_sha256 = file_sha256(working_path)
    artifacts = ArtifactStore(workspace / "artifacts")
    approved_snapshot = artifacts.ingest_file(
        working_path, allowed_root=workspace
    )
    evidence_path = workspace / "validation.json"
    evidence_path.write_text('{"status":"passed"}\n', encoding="utf-8")
    evidence_sha256 = file_sha256(evidence_path)
    package = _review_package()
    package["source"].update(
        working_copy_id=working_copy_id,
        change_set_ids=[change_set_id],
        after_model_sha256=final_sha256,
    )
    changes = [
        {
            "id": change_set_id,
            "working_copy_id": working_copy_id,
            "status": "applied",
            "resulting_sha256": final_sha256,
            "changes": [{"target": "actuator.mount"}],
        }
    ]
    validations = [
        {
            "id": "validation-failed",
            "working_copy_id": working_copy_id,
            "change_set_id": change_set_id,
            "validation_kind": "geometry_model",
            "status": "failed",
            "working_sha256": "9" * 64,
            "report_path": "",
            "report_sha256": "",
            "checks": [{"check_id": "shape.valid", "status": "failed"}],
        },
        {
            "id": "validation-geometry-final",
            "working_copy_id": working_copy_id,
            "change_set_id": change_set_id,
            "validation_kind": "geometry_model",
            "status": "passed",
            "working_sha256": final_sha256,
            "report_path": str(evidence_path.resolve()),
            "report_sha256": evidence_sha256,
            "checks": [{"check_id": "shape.valid", "status": "passed"}],
        },
        {
            "id": "validation-assembly-final",
            "working_copy_id": working_copy_id,
            "change_set_id": change_set_id,
            "validation_kind": "assembly_completeness",
            "status": "passed",
            "working_sha256": final_sha256,
            "report_path": str(evidence_path.resolve()),
            "report_sha256": evidence_sha256,
            "checks": [{"check_id": "assembly.complete", "status": "passed"}],
        },
    ]
    context = {
        "working_copy": {
            "id": working_copy_id,
            "job_id": "00000000-0000-0000-0000-000000000010",
            "organization_id": "org-001",
            "design_group_id": "group-001",
            "family_id": "family-001",
            "status": "approved_for_delivery",
            "approved_final_sha256": final_sha256,
            "approved_final_artifact_path": approved_snapshot["storage_path"],
            "working_path": str(working_path),
        },
        "change_sets": changes,
        "validation_reports": validations,
    }
    repository = _ReviewPreparationRepository(context)
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.settings = type(
        "Settings",
        (),
        {"workspace": workspace, "actor_id": "engineer-001"},
    )()
    service.bootstrap_config = {
        "organization_id": "org-001",
        "design_group_id": "group-001",
    }
    service.repository = repository
    service.artifacts = artifacts
    service.design_workspace = _ReviewPreparationWorkspace(
        final_sha256, working_path, repository
    )
    service.design_lesson_staging = DesignLessonStagingStore(
        workspace,
        staging_parts=("knowledge", "design-lessons", "staging"),
    )
    service.design_lesson_reviews = DesignLessonReviewStore(
        workspace,
        review_parts=("knowledge", "design-lessons", "reviews"),
    )
    service._job_design_lesson_stores = lambda _job_root: (
        service.design_lesson_staging,
        service.design_lesson_reviews,
    )
    service._require_database = lambda: None
    def explicit_review_projection():
        review_id = next(iter(repository.reviews), "review-not-prepared")
        return {
            "processed": 2,
            "failed": [],
            "processed_events": [
                {
                    "event_type": "design_lesson.approved",
                    "aggregate_type": "design_lesson",
                    "aggregate_id": "approved-lesson-001",
                },
                {
                    "event_type": "design_lesson_review.approved",
                    "aggregate_type": "design_lesson_review",
                    "aggregate_id": review_id,
                },
            ],
        }

    service._safe_projection = explicit_review_projection
    evidence_items = [
        {
            "evidence_id": "validation-evidence",
            "path": "validation.json",
            "role": "geometry_validation",
            "media_type": "application/json",
            "working_copy_id": working_copy_id,
            "change_set_id": change_set_id,
            "model_sha256": final_sha256,
            "validation_kind": "geometry_model",
        }
    ]
    return temporary, service, package, evidence_items


def test_review_store_prepares_a_redacted_human_card_with_every_assertion():
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        staged_inspection = _staged_review_inspection(workspace)

        review = DesignLessonReviewStore(workspace).prepare(
            "DLR-test-001", staged_inspection
        )

        assert review["status"] == "awaiting-engineer-review"
        assert review["confirmation"] == "批准设计经验 DLR-test-001"
        assert "atomic_assertions" in review["review_card"]
        assert staged_inspection["package_sha256"] not in review["review_card_markdown"]
        assert (
            staged_inspection["package"]["evidence_manifest"][0]["sha256"]
            not in review["review_card_markdown"]
        )
        assert staged_inspection["package"]["source"]["working_copy_id"] not in review["review_card_markdown"]
        for assertion in staged_inspection["package"]["atomic_assertions"]:
            for field in ("subject_ref", "predicate", "object_value", "constraint_kind"):
                assert str(assertion[field]).replace("'", '"') in review["review_card_markdown"]


def test_review_store_verify_rejects_a_card_changed_after_preparation():
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        staged_inspection = _staged_review_inspection(workspace)
        store = DesignLessonReviewStore(workspace)
        review = store.prepare("DLR-test-001", staged_inspection)
        Path(review["review_card_path"]).write_text("tampered\n", encoding="utf-8")

        with pytest.raises(ValueError, match="review card changed after preparation"):
            store.verify("DLR-test-001", staged_inspection["package_sha256"])


@pytest.mark.parametrize(
    ("path_key", "label"),
    [
        ("review_json_path", "review record"),
        ("review_card_path", "review card"),
    ],
)
def test_review_store_inspect_rejects_a_dangling_immutable_file_symlink(
    path_key, label
):
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        staged_inspection = _staged_review_inspection(workspace)
        store = DesignLessonReviewStore(workspace)
        review = store.prepare("DLR-test-001", staged_inspection)
        review_path = Path(review[path_key])
        review_path.unlink()
        review_path.symlink_to(workspace / "missing-review-file")

        with pytest.raises(ValueError, match=f"{label} path must not be a symlink"):
            store.inspect("DLR-test-001")


def test_review_store_does_not_replace_an_existing_review_id():
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        staged_inspection = _staged_review_inspection(workspace)
        store = DesignLessonReviewStore(workspace)
        review = store.prepare("DLR-test-001", staged_inspection)
        original_card = Path(review["review_card_path"]).read_bytes()

        with pytest.raises(ValueError, match="design lesson review is already prepared"):
            store.prepare("DLR-test-001", staged_inspection)

        assert Path(review["review_card_path"]).read_bytes() == original_card


@pytest.mark.parametrize(
    ("source_field", "invalid_value", "message"),
    [
        (
            "working_copy_id",
            "00000000-0000-0000-0000-000000000099",
            "working_copy_id",
        ),
        ("organization_id", "org-other", "organization_id"),
        ("design_group_id", "group-other", "design_group_id"),
    ],
)
def test_service_review_preparation_rejects_wrong_source_scope(
    source_field, invalid_value, message
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        requested_working_copy_id = package["source"]["working_copy_id"]
        package["source"][source_field] = invalid_value

        with pytest.raises(ValueError, match=message):
            service.design_lesson_review_prepare(
                requested_working_copy_id, package, evidence_items
            )

        assert service.repository.create_calls == []
    finally:
        temporary.cleanup()


def test_service_review_preparation_rejects_stale_after_model_hash():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        package["source"]["after_model_sha256"] = "b" * 64

        with pytest.raises(ValueError, match="after_model_sha256"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert service.repository.create_calls == []
    finally:
        temporary.cleanup()


@pytest.mark.parametrize("change_state", ["unapplied", "foreign"])
def test_service_review_preparation_rejects_unapplied_or_foreign_change_set(
    change_state
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        change = service.repository.context["change_sets"][0]
        if change_state == "unapplied":
            change["status"] = "approved"
        else:
            change["working_copy_id"] = "00000000-0000-0000-0000-000000000099"

        with pytest.raises(ValueError, match="change set"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert service.repository.create_calls == []
    finally:
        temporary.cleanup()


def test_service_review_preparation_rejects_absent_final_validation_binding():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        service.repository.context["validation_reports"][1]["change_set_id"] = (
            "00000000-0000-0000-0000-000000000099"
        )

        with pytest.raises(ValueError, match="same-revision passed geometry_model"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert service.repository.create_calls == []
    finally:
        temporary.cleanup()


def test_service_review_preparation_rejects_missing_evidence_before_review_creation():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        evidence_items[0]["path"] = "missing-validation.json"

        with pytest.raises((FileNotFoundError, ValueError), match="missing|escapes"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert service.repository.create_calls == []
    finally:
        temporary.cleanup()


def test_service_review_preparation_creates_one_hash_free_immutable_review():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )

        assert prepared["review_id"].startswith("DLR-")
        assert prepared["status"] == "awaiting-engineer-review"
        assert prepared["confirmation"] == f"批准设计经验 {prepared['review_id']}"
        assert prepared["review_card"]["title"] == package["title"]
        assert "package_sha256" not in prepared
        assert "review_card_sha256" not in prepared
        assert len(service.repository.create_calls) == 1
        inserted = service.repository.create_calls[0]
        assert inserted["working_copy_id"] == package["source"]["working_copy_id"]
        assert inserted["final_model_sha256"] == package["source"]["after_model_sha256"]
        assert inserted["approved_final_artifact_path"] == service.repository.context[
            "working_copy"
        ]["approved_final_artifact_path"]
        assert Path(inserted["review_path"]).is_file()
        assert Path(inserted["package_path"]).is_file()
        serialized = json.dumps(prepared, ensure_ascii=False)
        assert prepared["review_card"]["evidence_summary"] == [
            {
                "evidence_id": "validation-evidence",
                "role": "geometry_validation",
                "media_type": "application/json",
                "validation_kind": "geometry_model",
            }
        ]
        assert prepared["review_card"]["validation_summary"][0]["validation_kind"] == "geometry_model"
        assert "## Evidence summary" in prepared["review_card_markdown"]
        assert "## Validation summary" in prepared["review_card_markdown"]
        persisted_markdown = Path(inserted["review_path"]).read_text(encoding="utf-8")
        assert "geometry_model" in persisted_markdown
        assert "shape.valid" in persisted_markdown
        assert "passed" in persisted_markdown
        assert package["source"]["after_model_sha256"] not in serialized
        evidence_sha256 = file_sha256(
            Path(evidence_items[0]["path"])
            if Path(evidence_items[0]["path"]).is_absolute()
            else service.settings.workspace / evidence_items[0]["path"]
        )
        assert evidence_sha256 not in serialized
        for hidden_digest in (
            package["source"]["after_model_sha256"],
            evidence_sha256,
            inserted["package_sha256"],
            inserted["review_card_sha256"],
        ):
            assert hidden_digest not in persisted_markdown
    finally:
        temporary.cleanup()


def test_service_review_preparation_is_contained_under_originating_job_knowledge():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"],
            package,
            evidence_items,
            job_id="00000000-0000-0000-0000-000000000010",
            expected_job_revision=7,
        )

        inserted = service.repository.create_calls[0]
        expected_root = (
            service.settings.workspace / "knowledge" / "design-lessons"
        ).resolve()
        package_relative = relative_managed_path(
            Path(inserted["package_path"]), expected_root
        )
        review_relative = relative_managed_path(
            Path(inserted["review_path"]), expected_root
        )
        assert package_relative.name == "lesson.json"
        assert review_relative.name == "review.md"
        assert prepared["job_id"] == "00000000-0000-0000-0000-000000000010"
        assert prepared["job_revision"] == 7
    finally:
        temporary.cleanup()


def test_service_review_preparation_rejects_foreign_job_evidence():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    with tempfile.TemporaryDirectory() as foreign_temporary:
        try:
            foreign_evidence = Path(foreign_temporary) / "validation.json"
            foreign_evidence.write_text('{"status":"passed"}\n', encoding="utf-8")
            evidence_items[0]["path"] = str(foreign_evidence)

            with pytest.raises(ValueError, match="originating Design Job"):
                service.design_lesson_review_prepare(
                    package["source"]["working_copy_id"],
                    package,
                    evidence_items,
                )
        finally:
            temporary.cleanup()


def test_service_review_preparation_rejects_foreign_or_stale_job_request():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        with pytest.raises(JobFailure) as foreign:
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"],
                package,
                evidence_items,
                job_id="00000000-0000-0000-0000-000000000099",
                expected_job_revision=7,
            )
        assert foreign.value.code == "JOB_BINDING_MISMATCH"

        with pytest.raises(JobFailure) as stale:
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"],
                package,
                evidence_items,
                job_id="00000000-0000-0000-0000-000000000010",
                expected_job_revision=6,
            )
        assert stale.value.code == "JOB_STALE_REVISION"
    finally:
        temporary.cleanup()


def test_service_review_card_exposes_complete_learning_scope_without_hashes_or_paths():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        raw_digest = "d" * 64
        package["prevention"] = {
            "required_checks": ["PREVENTION-SENTINEL", raw_digest],
            "design_review_questions": ["QUESTION-SENTINEL"],
            "workflow_gate": "WORKFLOW-GATE-SENTINEL",
            "detection_method": "DETECTION-SENTINEL",
        }
        package["applicability"] = {
            "component_classes": ["APPLICABILITY-SENTINEL"],
            "interface_types": ["INTERFACE-SENTINEL"],
            "design_stages": ["STAGE-SENTINEL"],
            "required_conditions": ["CONDITION-SENTINEL"],
        }
        package["non_applicable_conditions"] = ["NON-APPLICABLE-SENTINEL"]
        package["search_terms"] = ["SEARCH-SENTINEL", raw_digest]

        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        card = prepared["review_card"]
        markdown = Path(service.repository.create_calls[0]["review_path"]).read_text(
            encoding="utf-8"
        )

        assert card["prevention"] == {
            "required_checks": ["PREVENTION-SENTINEL", "[sha256-redacted]"],
            "design_review_questions": ["QUESTION-SENTINEL"],
            "workflow_gate": "WORKFLOW-GATE-SENTINEL",
            "detection_method": "DETECTION-SENTINEL",
        }
        assert card["applicability"] == package["applicability"]
        assert card["non_applicable_conditions"] == [
            "NON-APPLICABLE-SENTINEL"
        ]
        assert card["search_terms"] == ["SEARCH-SENTINEL", "[sha256-redacted]"]
        for heading in (
            "## Prevention",
            "## Applicability",
            "## Non-applicable conditions",
            "## Search terms",
        ):
            assert heading in markdown
        for sentinel in (
            "PREVENTION-SENTINEL",
            "APPLICABILITY-SENTINEL",
            "NON-APPLICABLE-SENTINEL",
            "SEARCH-SENTINEL",
        ):
            assert sentinel in markdown
        assert raw_digest not in json.dumps(card, ensure_ascii=False)
        assert raw_digest not in markdown
        assert str(service.settings.workspace) not in markdown
    finally:
        temporary.cleanup()


def test_review_card_digest_changes_when_prevention_changes():
    digests = []
    for prevention_check in ("CHECK-SENTINEL-A", "CHECK-SENTINEL-B"):
        temporary, service, package, evidence_items = _review_preparation_fixture()
        try:
            package["prevention"]["required_checks"] = [prevention_check]
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )
            digests.append(
                service.repository.create_calls[0]["review_card_sha256"]
            )
        finally:
            temporary.cleanup()

    assert digests[0] != digests[1]


def test_service_review_preparation_renders_id_labeled_validation_checks():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        service.repository.context["validation_reports"][1]["checks"] = [
            {"id": "assembly.identity.unique", "status": "passed"}
        ]

        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )

        assert "assembly.identity.unique" in prepared["review_card_markdown"]
        assert prepared["review_card"]["validation_summary"][0]["checks"] == [
            {"label": "assembly.identity.unique", "status": "passed"}
        ]
        persisted = Path(service.repository.create_calls[0]["review_path"]).read_text(
            encoding="utf-8"
        )
        assert "assembly.identity.unique: passed" in persisted
    finally:
        temporary.cleanup()


def test_service_review_preparation_recursively_redacts_digest_tokens_from_human_content():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        model_sha256 = package["source"]["after_model_sha256"]
        evidence_sha256 = service.repository.context["validation_reports"][1][
            "report_sha256"
        ]
        injected_sha256 = "d" * 64
        original_summary = (
            f"Never expose {model_sha256}, {evidence_sha256}, or {injected_sha256}."
        )
        package["problem"]["summary"] = original_summary
        service.repository.context["validation_reports"][1]["checks"] = [
            {"id": injected_sha256, "status": "passed"}
        ]

        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )

        inserted = service.repository.create_calls[0]
        human_text = json.dumps(prepared["review_card"], ensure_ascii=False) + prepared[
            "review_card_markdown"
        ]
        persisted = Path(inserted["review_path"]).read_text(encoding="utf-8")
        assert re.search(r"(?i)[0-9a-f]{64}", human_text) is None
        assert re.search(r"(?i)[0-9a-f]{64}", persisted) is None
        staged = service.design_lesson_staging.get_review(inserted["package_sha256"])
        assert staged["package"]["problem"]["summary"] == original_summary
    finally:
        temporary.cleanup()


def test_service_review_preparation_rejects_a_duplicate_package():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        inserted = service.repository.create_calls[0]
        original_package = Path(inserted["package_path"]).read_bytes()
        original_review = Path(inserted["review_path"]).read_bytes()

        with pytest.raises(ValueError, match="already staged"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert len(service.repository.create_calls) == 1
        assert Path(inserted["package_path"]).read_bytes() == original_package
        assert Path(inserted["review_path"]).read_bytes() == original_review
    finally:
        temporary.cleanup()


def test_service_review_replacement_atomically_supersedes_the_predecessor():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        predecessor = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        replacement_package = copy.deepcopy(package)
        replacement_package["corrections"] = [
            "Add clearance and motion-envelope checks"
        ]

        replacement = service.design_lesson_review_prepare(
            replacement_package["source"]["working_copy_id"],
            replacement_package,
            evidence_items,
            supersedes_review_id=predecessor["review_id"],
        )

        assert replacement["review_id"] != predecessor["review_id"]
        assert service.repository.reviews[predecessor["review_id"]]["status"] == "superseded"
        assert service.repository.create_calls[-1]["supersedes_review_id"] == predecessor["review_id"]
        assert service.repository.create_calls[0]["lesson_id"] == service.repository.create_calls[1]["lesson_id"]
        assert service.repository.create_calls[0]["package_sha256"] != service.repository.create_calls[1]["package_sha256"]
    finally:
        temporary.cleanup()


def test_service_review_preparation_keeps_working_copy_locked_through_insert():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        service.repository.required_lock_owner = service.design_workspace

        service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )

        assert not service.design_workspace.locked
        assert len(service.repository.create_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_preparation_uses_immutable_evidence_when_original_changes_during_staging():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        original_stage_review = service.design_lesson_staging.stage_review

        def replace_then_stage(package_value, evidence_value):
            staged = original_stage_review(package_value, evidence_value)
            (service.settings.workspace / "validation.json").write_text(
                '{"status":"replacement"}\n', encoding="utf-8"
            )
            return staged

        service.design_lesson_staging.stage_review = replace_then_stage

        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )

        inserted = service.repository.create_calls[0]
        inspection = service.design_lesson_staging.get_review(
            inserted["package_sha256"]
        )
        assert prepared["status"] == "awaiting-engineer-review"
        assert inspection["status"] == "verified-local-only"
        staged_evidence_path = inspection["package"]["evidence_manifest"][0]["path"]
        assert staged_evidence_path.startswith(
            "knowledge/design-lessons/evidence/"
        )
        assert staged_evidence_path != evidence_items[0]["path"]
    finally:
        temporary.cleanup()


def test_service_review_preparation_rejects_fcstd_mutated_during_staging_without_row():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        original_stage_review = service.design_lesson_staging.stage_review

        def mutate_model_then_stage(package_value, evidence_value):
            service.design_workspace.working_path.write_bytes(b"externally-mutated-model")
            return original_stage_review(package_value, evidence_value)

        service.design_lesson_staging.stage_review = mutate_model_then_stage

        with pytest.raises(ValueError, match="changed after delivery approval"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert service.repository.create_calls == []
    finally:
        temporary.cleanup()


def test_service_precommit_fcstd_mutation_rolls_back_row_and_predecessor_and_cleans_attempt():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        predecessor = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        replacement_package = copy.deepcopy(package)
        replacement_package["corrections"] = ["Revised correction"]

        def mutate_fcstd(_row):
            service.design_workspace.working_path.write_bytes(b"mutated-during-insert")

        service.repository.pre_commit_hook = mutate_fcstd
        with pytest.raises(ValueError, match="changed after delivery approval"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"],
                replacement_package,
                evidence_items,
                supersedes_review_id=predecessor["review_id"],
            )

        assert service.repository.reviews[predecessor["review_id"]]["status"] == (
            "awaiting-engineer-review"
        )
        assert len(service.repository.reviews) == 1
        assert len(list(service.design_lesson_staging.staging_root.glob("review-*"))) == 1
        assert len(list(service.design_lesson_reviews.review_root.glob("DLR-*"))) == 1
    finally:
        temporary.cleanup()


def test_service_precommit_evidence_mutation_rolls_back_row_and_cleans_attempt():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        def mutate_evidence(row):
            evidence = service.design_lesson_staging.review_evidence_paths(
                row["package_sha256"]
            )[0][1]
            os.chmod(evidence, 0o644)
            evidence.write_text('{"status":"mutated-in-transaction"}\n', encoding="utf-8")

        service.repository.pre_commit_hook = mutate_evidence
        with pytest.raises(
            (IOError, ValueError), match="checksum|writable|integrity|changed"
        ):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert service.repository.reviews == {}
        assert service.repository.create_calls == []
        assert list(service.design_lesson_staging.staging_root.glob("review-*")) == []
        assert list(service.design_lesson_reviews.review_root.glob("DLR-*")) == []
    finally:
        temporary.cleanup()


def test_service_attempt_owned_cleanup_removes_drifted_files_and_preserves_original_error():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        predecessor = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        predecessor_row = service.repository.create_calls[0]
        replacement_package = copy.deepcopy(package)
        replacement_package["corrections"] = ["Replacement with drift injection"]

        def tamper_attempt(row):
            Path(row["review_path"]).write_text("tampered review\n", encoding="utf-8")
            Path(row["package_path"]).write_text("{}\n", encoding="utf-8")

        service.repository.pre_commit_hook = tamper_attempt
        with pytest.raises(
            ValueError, match="staged design lesson changed before review insertion"
        ):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"],
                replacement_package,
                evidence_items,
                supersedes_review_id=predecessor["review_id"],
            )

        assert service.repository.reviews[predecessor["review_id"]]["status"] == (
            "awaiting-engineer-review"
        )
        assert len(service.repository.reviews) == 1
        assert Path(predecessor_row["review_path"]).is_file()
        assert Path(predecessor_row["package_path"]).is_file()
        assert len(list(service.design_lesson_reviews.review_root.glob("DLR-*"))) == 1
        assert len(list(service.design_lesson_staging.staging_root.glob("review-*"))) == 1
    finally:
        temporary.cleanup()


def test_attempt_owned_cleanup_does_not_follow_inner_symlinks():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        row = service.repository.create_calls[0]
        external = service.settings.workspace / "external.txt"
        external.write_text("preserve me\n", encoding="utf-8")
        review_path = Path(row["review_path"])
        review_path.unlink()
        review_path.symlink_to(external)
        package_path = Path(row["package_path"])
        package_path.unlink()
        package_path.symlink_to(external)

        service.design_lesson_reviews.discard_prepared_attempt_owned(
            prepared["review_id"]
        )
        service.design_lesson_staging.discard_review_attempt_owned(
            row["package_sha256"]
        )

        assert external.read_text(encoding="utf-8") == "preserve me\n"
        assert not review_path.parent.exists()
        assert not package_path.parent.exists()
    finally:
        temporary.cleanup()


def test_attempt_owned_cleanup_rejects_directory_symlinks_without_deleting_targets():
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        external_review = workspace / "external-review"
        external_stage = workspace / "external-stage"
        external_review.mkdir()
        external_stage.mkdir()
        review_store = DesignLessonReviewStore(workspace)
        staging_store = DesignLessonStagingStore(workspace)
        (review_store.review_root / "DLR-symlink").symlink_to(
            external_review, target_is_directory=True
        )
        digest = "a" * 64
        (staging_store.staging_root / f"review-{digest}").symlink_to(
            external_stage, target_is_directory=True
        )

        with pytest.raises(ValueError, match="symlink|reparse point"):
            review_store.discard_prepared_attempt_owned("DLR-symlink")
        with pytest.raises(ValueError, match="symlink|reparse point"):
            staging_store.discard_review_attempt_owned(digest)

        assert external_review.is_dir()
        assert external_stage.is_dir()


def test_service_cleanup_attempts_are_independent_and_preserve_verifier_exception():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        cleanup_calls: list[str] = []

        def tamper_package(row):
            Path(row["package_path"]).write_text("{}\n", encoding="utf-8")

        def fail_review_cleanup(_review_id):
            cleanup_calls.append("review")
            raise RuntimeError("injected review cleanup failure")

        original_stage_cleanup = (
            service.design_lesson_staging.discard_review_attempt_owned
        )

        def track_stage_cleanup(package_sha256):
            cleanup_calls.append("staging")
            original_stage_cleanup(package_sha256)

        service.repository.pre_commit_hook = tamper_package
        service.design_lesson_reviews.discard_prepared_attempt_owned = (
            fail_review_cleanup
        )
        service.design_lesson_staging.discard_review_attempt_owned = (
            track_stage_cleanup
        )

        with pytest.raises(
            ValueError, match="staged design lesson changed before review insertion"
        ) as caught:
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        assert cleanup_calls == ["review", "staging"]
        assert any(
            "injected review cleanup failure" in note
            for note in getattr(caught.value, "__notes__", [])
        )
        assert service.repository.reviews == {}
        assert list(service.design_lesson_staging.staging_root.glob("review-*")) == []
    finally:
        temporary.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-race regression")
def test_review_attempt_cleanup_root_swap_only_removes_from_pinned_root(monkeypatch):
    from mechanical_design_agent import secure_fs_posix

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        store = DesignLessonReviewStore(workspace)
        root = store.review_root
        review_id = "DLR-root-swap"
        attempt = root / review_id
        attempt.mkdir()
        (attempt / "attempt.txt").write_text("attempt\n", encoding="utf-8")
        external_root = workspace / "external-review-root"
        external_attempt = external_root / review_id
        external_attempt.mkdir(parents=True)
        external_victim = external_attempt / "victim.txt"
        external_victim.write_text("external\n", encoding="utf-8")
        pinned_root = workspace / "pinned-review-root"
        original_rmtree = secure_fs_posix.shutil.rmtree
        swapped = False

        def swap_root_then_remove(path, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                root.rename(pinned_root)
                root.symlink_to(external_root, target_is_directory=True)
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "mechanical_design_agent.secure_fs_posix.shutil.rmtree",
            swap_root_then_remove,
        )

        store.discard_prepared_attempt_owned(review_id)

        assert external_victim.read_text(encoding="utf-8") == "external\n"
        assert not (pinned_root / review_id).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-race regression")
def test_staging_attempt_cleanup_root_swap_only_removes_from_pinned_root(monkeypatch):
    from mechanical_design_agent import secure_fs_posix

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        store = DesignLessonStagingStore(workspace)
        root = store.staging_root
        digest = "b" * 64
        child_name = f"review-{digest}"
        attempt = root / child_name
        attempt.mkdir()
        (attempt / "attempt.txt").write_text("attempt\n", encoding="utf-8")
        external_root = workspace / "external-staging-root"
        external_attempt = external_root / child_name
        external_attempt.mkdir(parents=True)
        external_victim = external_attempt / "victim.txt"
        external_victim.write_text("external\n", encoding="utf-8")
        pinned_root = workspace / "pinned-staging-root"
        original_rmtree = secure_fs_posix.shutil.rmtree
        swapped = False

        def swap_root_then_remove(path, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                root.rename(pinned_root)
                root.symlink_to(external_root, target_is_directory=True)
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "mechanical_design_agent.secure_fs_posix.shutil.rmtree",
            swap_root_then_remove,
        )

        store.discard_review_attempt_owned(digest)

        assert external_victim.read_text(encoding="utf-8") == "external\n"
        assert not (pinned_root / child_name).exists()


def test_review_attempt_cleanup_ancestor_swap_does_not_follow_external_workspace():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        trusted_ancestor = base / "trusted-ancestor"
        workspace = trusted_ancestor / "workspace"
        workspace.mkdir(parents=True)
        store = DesignLessonReviewStore(workspace)
        review_id = "DLR-ancestor-swap"
        trusted_attempt = store.review_root / review_id
        trusted_attempt.mkdir()
        trusted_bytes = b"trusted attempt\n"
        (trusted_attempt / "attempt.txt").write_bytes(trusted_bytes)

        external_ancestor = base / "external-ancestor"
        external_attempt = (
            external_ancestor
            / "workspace"
            / "output"
            / "mechanical_design"
            / "lesson_reviews"
            / review_id
        )
        external_attempt.mkdir(parents=True)
        external_bytes = b"external same-name review victim\n"
        external_victim = external_attempt / "attempt.txt"
        external_victim.write_bytes(external_bytes)

        pinned_ancestor = base / "pinned-trusted-ancestor"
        trusted_ancestor.rename(pinned_ancestor)
        trusted_ancestor.symlink_to(external_ancestor, target_is_directory=True)

        failed_safely = False
        try:
            store.discard_prepared_attempt_owned(review_id)
        except ValueError:
            failed_safely = True

        assert external_victim.read_bytes() == external_bytes
        pinned_victim = (
            pinned_ancestor
            / "workspace"
            / "output"
            / "mechanical_design"
            / "lesson_reviews"
            / review_id
            / "attempt.txt"
        )
        if failed_safely:
            assert pinned_victim.read_bytes() == trusted_bytes
        else:
            assert not pinned_victim.parent.exists()


def test_staging_attempt_cleanup_ancestor_swap_does_not_follow_external_workspace():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        trusted_ancestor = base / "trusted-ancestor"
        workspace = trusted_ancestor / "workspace"
        workspace.mkdir(parents=True)
        store = DesignLessonStagingStore(workspace)
        digest = "c" * 64
        child_name = f"review-{digest}"
        trusted_attempt = store.staging_root / child_name
        trusted_attempt.mkdir()
        trusted_bytes = b"trusted staged attempt\n"
        (trusted_attempt / "attempt.txt").write_bytes(trusted_bytes)

        external_ancestor = base / "external-ancestor"
        external_attempt = (
            external_ancestor
            / "workspace"
            / "output"
            / "mechanical_design"
            / "lesson_staging"
            / child_name
        )
        external_attempt.mkdir(parents=True)
        external_bytes = b"external same-name staging victim\n"
        external_victim = external_attempt / "attempt.txt"
        external_victim.write_bytes(external_bytes)

        pinned_ancestor = base / "pinned-trusted-ancestor"
        trusted_ancestor.rename(pinned_ancestor)
        trusted_ancestor.symlink_to(external_ancestor, target_is_directory=True)

        failed_safely = False
        try:
            store.discard_review_attempt_owned(digest)
        except ValueError:
            failed_safely = True

        assert external_victim.read_bytes() == external_bytes
        pinned_victim = (
            pinned_ancestor
            / "workspace"
            / "output"
            / "mechanical_design"
            / "lesson_staging"
            / child_name
            / "attempt.txt"
        )
        if failed_safely:
            assert pinned_victim.read_bytes() == trusted_bytes
        else:
            assert not pinned_victim.parent.exists()


def test_service_review_preparation_db_failure_cleans_new_artifacts_and_exact_retry_succeeds():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        service.repository.fail_next_insert = True
        with pytest.raises(RuntimeError, match="injected review insert failure"):
            service.design_lesson_review_prepare(
                package["source"]["working_copy_id"], package, evidence_items
            )

        staging_root = service.design_lesson_staging.staging_root
        review_root = service.design_lesson_reviews.review_root
        assert list(staging_root.glob("review-*")) == []
        assert list(review_root.glob("DLR-*")) == []

        retried = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        assert retried["status"] == "awaiting-engineer-review"
        assert len(service.repository.create_calls) == 1
    finally:
        temporary.cleanup()


def test_repository_preparation_inserts_awaiting_review_and_enqueues_event():
    connection = _TrackedConnection()
    with _repository_with(connection) as repository:
        created = _create(repository)

    assert created["status"] == "awaiting-engineer-review"
    assert list(connection.reviews) == ["DLR-new-001"]
    assert connection.transaction_events == ["begin", "commit"]
    assert [event["event_type"] for event in connection.outbox_events] == [
        "design_lesson_review.prepared"
    ]
    payload = json.loads(connection.outbox_events[0]["payload"])
    assert payload == {
        "review_id": "DLR-new-001",
        "organization_id": "org-001",
        "design_group_id": "group-001",
        "working_copy_id": "working-001",
        "job_id": "job-001",
        "lesson_id": "DL-001",
        "package_sha256": "4" * 64,
        "review_card_sha256": "5" * 64,
        "final_model_sha256": "6" * 64,
        "approved_final_artifact_path": "/artifacts/approved-final.FCStd",
        "status": "awaiting-engineer-review",
        "supersedes_review_id": None,
    }


def test_repository_delivery_approval_persists_and_audits_exact_final_sha256():
    connection = _TrackedConnection()
    connection.working_copies["working-001"]["approved_final_sha256"] = None
    connection.working_copies["working-001"]["approved_final_artifact_path"] = None
    final_sha256 = "a" * 64
    with _repository_with(connection) as repository:
        approved = repository.approve_delivery(
            "working-001",
            "engineer-001",
            "批准 working-001",
            final_sha256,
            "/artifacts/approved-final.FCStd",
            organization_id="org-001",
            design_group_id="group-001",
        )

    assert approved["approved_final_sha256"] == final_sha256
    assert approved["approved_final_artifact_path"] == "/artifacts/approved-final.FCStd"
    payload = json.loads(connection.outbox_events[-1]["payload"])
    assert payload["approved_final_sha256"] == final_sha256
    assert payload["approved_final_artifact_path"] == "/artifacts/approved-final.FCStd"


@pytest.mark.parametrize(
    ("actor_id", "organization_id", "design_group_id"),
    [
        ("outsider-001", "org-001", "group-001"),
        ("engineer-role-001", "org-001", "group-001"),
        ("engineer-001", "org-other", "group-001"),
        ("engineer-001", "org-001", "group-other"),
    ],
)
def test_repository_delivery_approval_rechecks_scope_and_role_under_locks(
    actor_id, organization_id, design_group_id
):
    connection = _TrackedConnection()
    connection.working_copies["working-001"].update(
        status="draft",
        approved_final_sha256=None,
        approved_final_artifact_path=None,
    )
    with _repository_with(connection) as repository:
        with pytest.raises((PermissionError, KeyError), match="delivery approval"):
            repository.approve_delivery(
                "working-001",
                actor_id,
                "批准 working-001",
                "a" * 64,
                "/artifacts/approved-final.FCStd",
                organization_id=organization_id,
                design_group_id=design_group_id,
            )

    assert connection.working_copies["working-001"]["status"] == "draft"
    assert connection.outbox_events == []
    assert any("FOR UPDATE" in sql for sql, _ in connection.queries)


def test_repository_invalidates_drifted_review_transactionally_and_audits_it():
    connection = _TrackedConnection()
    connection.reviews["DLR-drifted-001"] = _review("DLR-drifted-001")

    with _repository_with(connection) as repository:
        invalidated = repository.invalidate_design_lesson_review(
            review_id="DLR-drifted-001",
            reviewer_id="engineer-001",
            reason="immutable review binding verification failed",
        )

    assert invalidated["status"] == "invalid"
    assert invalidated["reviewed_by"] == "engineer-001"
    assert [event["event_type"] for event in connection.outbox_events] == [
        "design_lesson_review.invalid"
    ]
    payload = json.loads(connection.outbox_events[0]["payload"])
    assert payload["review_id"] == "DLR-drifted-001"
    assert payload["status"] == "invalid"


@pytest.mark.parametrize(
    ("working_status", "approved_sha256", "approved_artifact_path", "message"),
    [
        ("draft", "6" * 64, "/artifacts/approved-final.FCStd", "delivery-approved"),
        (
            "approved_for_delivery",
            "7" * 64,
            "/artifacts/approved-final.FCStd",
            "final SHA",
        ),
        (
            "approved_for_delivery",
            "6" * 64,
            "/artifacts/another-final.FCStd",
            "final artifact",
        ),
    ],
)
def test_repository_preparation_rechecks_delivery_status_and_approval_binding(
    working_status, approved_sha256, approved_artifact_path, message
):
    connection = _TrackedConnection()
    connection.working_copies["working-001"].update(
        status=working_status,
        approved_final_sha256=approved_sha256,
        approved_final_artifact_path=approved_artifact_path,
    )
    with _repository_with(connection) as repository:
        with pytest.raises(ValueError, match=message):
            _create(repository)

    assert connection.reviews == {}
    assert connection.outbox_events == []


@pytest.mark.parametrize(
    ("design_group", "working_copy", "error"),
    [
        (
            {"id": "group-cross", "organization_id": "org-other"},
            {
                "id": "working-cross",
                "organization_id": "org-001",
                "design_group_id": "group-cross",
            },
            "design group",
        ),
        (
            {"id": "group-001", "organization_id": "org-001"},
            {
                "id": "working-cross",
                "organization_id": "org-other",
                "design_group_id": "group-001",
            },
            "working copy",
        ),
        (
            {"id": "group-001", "organization_id": "org-001"},
            {
                "id": "working-cross",
                "organization_id": "org-001",
                "design_group_id": "group-other",
            },
            "working copy",
        ),
    ],
)
def test_repository_preparation_rejects_incoherent_tenant_scope(
    design_group, working_copy, error
):
    connection = _TrackedConnection()
    connection.design_groups = {design_group["id"]: design_group}
    working_copy["job_id"] = "job-cross"
    connection.working_copies[working_copy["id"]] = working_copy
    with _repository_with(connection) as repository:
        with pytest.raises(ValueError, match=error):
            _create(
                repository,
                design_group_id=design_group["id"],
                working_copy_id=working_copy["id"],
            )


def test_repository_replacement_locks_and_supersedes_predecessor_atomically():
    connection = _TrackedConnection()
    connection.reviews["DLR-old-001"] = _review("DLR-old-001")
    with _repository_with(connection) as repository:
        replacement = _create(repository, supersedes_review_id="DLR-old-001")

    assert replacement["supersedes_review_id"] == "DLR-old-001"
    assert connection.reviews["DLR-old-001"]["status"] == "superseded"
    assert connection.transaction_events == ["begin", "commit"]
    assert any(
        sql.startswith("SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE")
        for sql, _ in connection.queries
    )
    assert [event["event_type"] for event in connection.outbox_events] == [
        "design_lesson_review.superseded",
        "design_lesson_review.prepared",
    ]
    superseded_payload = json.loads(connection.outbox_events[0]["payload"])
    prepared_payload = json.loads(connection.outbox_events[1]["payload"])
    assert superseded_payload["organization_id"] == "org-001"
    assert superseded_payload["design_group_id"] == "group-001"
    assert superseded_payload["package_sha256"] == "1" * 64
    assert superseded_payload["review_card_sha256"] == "2" * 64
    assert superseded_payload["final_model_sha256"] == "3" * 64
    assert superseded_payload["supersedes_review_id"] is None
    assert superseded_payload["superseded_by_review_id"] == "DLR-new-001"
    assert prepared_payload["supersedes_review_id"] == "DLR-old-001"


def test_repository_precommit_verifier_failure_rolls_back_insert_outbox_and_supersession():
    connection = _TrackedConnection()
    connection.reviews["DLR-old-001"] = _review("DLR-old-001")

    def reject_before_commit():
        assert connection.reviews["DLR-old-001"]["status"] == "superseded"
        assert "DLR-new-001" in connection.reviews
        assert len(connection.outbox_events) == 2
        raise ValueError("filesystem changed before commit")

    with _repository_with(connection) as repository:
        with pytest.raises(ValueError, match="filesystem changed before commit"):
            _create(
                repository,
                supersedes_review_id="DLR-old-001",
                pre_commit_verifier=reject_before_commit,
            )

    assert connection.reviews["DLR-old-001"]["status"] == "awaiting-engineer-review"
    assert "DLR-new-001" not in connection.reviews
    assert connection.outbox_events == []
    assert connection.transaction_events == ["begin", "rollback"]


def test_repository_can_supersede_a_legacy_review_with_null_artifact_path():
    connection = _TrackedConnection()
    predecessor = _review("DLR-old-001")
    predecessor["approved_final_artifact_path"] = None
    connection.reviews[predecessor["id"]] = predecessor

    with _repository_with(connection) as repository:
        replacement = _create(repository, supersedes_review_id=predecessor["id"])

    assert replacement["status"] == "awaiting-engineer-review"
    assert connection.reviews[predecessor["id"]]["status"] == "superseded"


def test_repository_can_reject_a_legacy_review_with_null_artifact_path():
    connection = _TrackedConnection()
    legacy = _review("DLR-old-001")
    legacy["approved_final_artifact_path"] = None
    connection.reviews[legacy["id"]] = legacy

    with _repository_with(connection) as repository:
        rejected = repository.reject_design_lesson_review(
            review_id=legacy["id"],
            reviewer_id="engineer-001",
            reviewer_text="Legacy review rejected",
        )

    assert rejected["status"] == "rejected"


def test_repository_rejects_a_new_review_without_an_immutable_artifact_binding():
    connection = _TrackedConnection()
    connection.working_copies["working-001"]["approved_final_artifact_path"] = None

    with _repository_with(connection) as repository:
        with pytest.raises(ValueError, match="immutable final artifact"):
            _create(repository, approved_final_artifact_path=None)

    assert connection.reviews == {}
    assert connection.outbox_events == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("design_group_id", "group-other"),
        ("working_copy_id", "working-other"),
        ("lesson_id", "DL-other"),
    ],
)
def test_repository_replacement_rejects_unrelated_predecessor(field, value):
    connection = _TrackedConnection()
    predecessor = _review("DLR-old-001")
    predecessor[field] = value
    connection.reviews["DLR-old-001"] = predecessor
    with _repository_with(connection) as repository:
        with pytest.raises(ValueError, match="same design lesson set"):
            _create(repository, supersedes_review_id="DLR-old-001")

    assert connection.reviews["DLR-old-001"]["status"] == "awaiting-engineer-review"
    assert connection.outbox_events == []


def test_repository_replacement_rolls_back_predecessor_and_outbox_when_insert_fails():
    connection = _TrackedConnection()
    connection.reviews["DLR-old-001"] = _review("DLR-old-001")
    connection.fail_on_review_insert = True
    with _repository_with(connection) as repository:
        with pytest.raises(RuntimeError, match="injected review insert failure"):
            _create(repository, supersedes_review_id="DLR-old-001")

    assert connection.reviews["DLR-old-001"]["status"] == "awaiting-engineer-review"
    assert "DLR-new-001" not in connection.reviews
    assert connection.outbox_events == []
    assert connection.transaction_events == ["begin", "rollback"]


def test_repository_replacement_rejects_unknown_or_finished_predecessor():
    connection = _TrackedConnection()
    connection.reviews["DLR-finished-001"] = _review(
        "DLR-finished-001", status="rejected"
    )
    with _repository_with(connection) as repository:
        with pytest.raises(KeyError, match="unknown supersedes_review_id"):
            _create(repository, supersedes_review_id="DLR-missing-001")
        with pytest.raises(ValueError, match="awaiting-engineer-review"):
            _create(repository, supersedes_review_id="DLR-finished-001")


def test_repository_rejection_authorizes_actor_and_only_rejects_awaiting_review():
    connection = _TrackedConnection()
    connection.reviews["DLR-review-001"] = _review("DLR-review-001")
    with _repository_with(connection) as repository:
        rejected = repository.reject_design_lesson_review(
            review_id="DLR-review-001",
            reviewer_id="engineer-001",
            reviewer_text="Not generalizable",
        )
        with pytest.raises(ValueError, match="awaiting-engineer-review"):
            repository.reject_design_lesson_review(
                review_id="DLR-review-001",
                reviewer_id="engineer-001",
                reviewer_text="Again",
            )

    assert rejected["status"] == "rejected"
    assert [event["event_type"] for event in connection.outbox_events] == [
        "design_lesson_review.rejected"
    ]


def test_repository_rejection_rejects_unknown_review_and_cross_organization_actor():
    connection = _TrackedConnection()
    connection.reviews["DLR-review-001"] = _review("DLR-review-001")
    with _repository_with(connection) as repository:
        with pytest.raises(KeyError, match="unknown design lesson review"):
            repository.reject_design_lesson_review(
                review_id="DLR-missing-001",
                reviewer_id="engineer-001",
                reviewer_text="No review",
            )
        with pytest.raises(PermissionError, match="configured organization"):
            repository.reject_design_lesson_review(
                review_id="DLR-review-001",
                reviewer_id="outsider-001",
                reviewer_text="Cross-scope attempt",
            )


def test_repository_probe_records_failure_and_only_successfully_transitions_pending():
    connection = _TrackedConnection()
    connection.reviews["DLR-pending-001"] = _review(
        "DLR-pending-001", status="approved-retrieval-pending"
    )
    with _repository_with(connection) as repository:
        pending = repository.record_design_lesson_review_probe(
            review_id="DLR-pending-001", probe={"matched": False}, successful=False
        )
        completed = repository.record_design_lesson_review_probe(
            review_id="DLR-pending-001", probe={"matched": True}, successful=True
        )

    assert pending["status"] == "approved-retrieval-pending"
    assert completed["status"] == "stored-and-retrievable"
    assert [event["event_type"] for event in connection.outbox_events] == [
        "design_lesson_review.retrieval_verified"
    ]


def test_repository_returns_only_exact_processed_projection_witnesses():
    connection = _TrackedConnection()
    connection.outbox_events = [
        {
            "id": "event-lesson",
            "event_type": "design_lesson.approved",
            "aggregate_type": "design_lesson",
            "aggregate_id": "lesson-001",
            "processed_at": 1,
        },
        {
            "id": "event-review",
            "event_type": "design_lesson_review.approved",
            "aggregate_type": "design_lesson_review",
            "aggregate_id": "DLR-review-001",
            "processed_at": 1,
        },
        {
            "id": "event-unprocessed",
            "event_type": "design_lesson_review.approved",
            "aggregate_type": "design_lesson_review",
            "aggregate_id": "DLR-review-001",
            "processed_at": None,
        },
        {
            "id": "event-foreign",
            "event_type": "design_lesson.approved",
            "aggregate_type": "design_lesson",
            "aggregate_id": "lesson-foreign",
            "processed_at": 1,
        },
    ]

    with _repository_with(connection) as repository:
        witnesses = repository.processed_design_lesson_review_projection_witnesses(
            review_id="DLR-review-001", lesson_id="lesson-001"
        )

    assert witnesses == [
        {
            "event_id": "event-lesson",
            "event_type": "design_lesson.approved",
            "aggregate_type": "design_lesson",
            "aggregate_id": "lesson-001",
        },
        {
            "event_id": "event-review",
            "event_type": "design_lesson_review.approved",
            "aggregate_type": "design_lesson_review",
            "aggregate_id": "DLR-review-001",
        },
    ]
    sql, parameters = connection.queries[-1]
    assert "processed_at IS NOT NULL" in sql
    assert parameters == ("lesson-001", "DLR-review-001")


def test_repository_projection_reviews_include_authoritative_state_and_version():
    connection = _TrackedConnection()
    connection.reviews["DLR-rebuild-001"] = {
        **_review("DLR-rebuild-001", status="invalid"),
        "created_at": "2026-08-12T01:00:00Z",
        "reviewed_at": "2026-08-12T02:00:00Z",
        "published_design_lesson_id": "lesson-001",
    }
    connection.outbox_events.append(
        {
            "aggregate_type": "design_lesson_review",
            "aggregate_id": "DLR-rebuild-001",
            "event_type": "design_lesson_review.invalid",
            "aggregate_version": 5,
            "payload": "{}",
        }
    )

    with _repository_with(connection) as repository:
        rows = repository.projection_design_lesson_reviews()

    assert rows[0]["review_id"] == "DLR-rebuild-001"
    assert rows[0]["status"] == "invalid"
    assert rows[0]["published_design_lesson_id"] == "lesson-001"
    assert rows[0]["occurred_at"] == "2026-08-12T02:00:00Z"
    assert rows[0]["aggregate_version"] == 5
    sql, parameters = connection.queries[-1]
    assert "max(o.aggregate_version)" in sql
    assert "AT TIME ZONE 'UTC'" in sql
    assert "AS occurred_at" in sql
    assert "ORDER BY r.created_at,r.id" in sql
    assert parameters == ()


def test_repository_probe_rejects_unknown_review_and_invalid_transition():
    connection = _TrackedConnection()
    connection.reviews["DLR-awaiting-001"] = _review("DLR-awaiting-001")
    with _repository_with(connection) as repository:
        with pytest.raises(KeyError, match="unknown design lesson review"):
            repository.record_design_lesson_review_probe(
                review_id="DLR-missing-001", probe={}, successful=True
            )
        with pytest.raises(ValueError, match="approved-retrieval-pending"):
            repository.record_design_lesson_review_probe(
                review_id="DLR-awaiting-001", probe={}, successful=True
            )


def test_repository_get_rejects_unknown_review_id():
    connection = _TrackedConnection()
    with _repository_with(connection) as repository:
        with pytest.raises(KeyError, match="unknown design lesson review"):
            repository.get_design_lesson_review("DLR-missing-001")


def test_repository_context_returns_delivery_approved_copy_and_complete_ordered_history():
    connection = _TrackedConnection()
    connection.working_copies["working-001"] = {
        "id": "working-001",
        "organization_id": "org-001",
        "design_group_id": "group-001",
        "status": "approved_for_delivery",
    }
    connection.change_sets = [
        {"id": "change-002", "working_copy_id": "working-001", "status": "applied", "created_at": 2},
        {"id": "change-001", "working_copy_id": "working-001", "status": "applied", "created_at": 1},
        {"id": "change-003", "working_copy_id": "working-001", "status": "rejected", "created_at": 3},
    ]
    connection.validation_reports = [
        {"id": "validation-002", "working_copy_id": "working-001", "status": "passed", "created_at": 2},
        {"id": "validation-001", "working_copy_id": "working-001", "status": "failed", "created_at": 1},
    ]
    connection.working_copies["working-001"]["approved_final_sha256"] = "a" * 64
    connection.standard_parts = [
        {
            "id": "part-002",
            "provider_id": "step-parts",
            "part_number": "B-002",
            "metadata": {"working_copy_id": "working-001", "model_sha256": "a" * 64},
        },
        {
            "id": "part-001",
            "provider_id": "fasteners",
            "part_number": "A-001",
            "metadata": {"working_copy_id": "working-001", "model_sha256": "a" * 64},
        },
        {
            "id": "part-foreign",
            "provider_id": "fasteners",
            "part_number": "SECRET",
            "metadata": {"working_copy_id": "working-other", "model_sha256": "a" * 64},
        },
        {
            "id": "part-stale",
            "provider_id": "fasteners",
            "part_number": "STALE",
            "metadata": {"working_copy_id": "working-001", "model_sha256": "b" * 64},
        },
    ]
    with _repository_with(connection) as repository:
        context = repository.design_lesson_review_context(
            "working-001", organization_id="org-001", design_group_id="group-001"
        )

    assert context["working_copy"]["status"] == "approved_for_delivery"
    assert [row["id"] for row in context["change_sets"]] == ["change-001", "change-002"]
    assert [row["status"] for row in context["validation_reports"]] == ["failed", "passed"]
    assert [row["id"] for row in context["standard_part_provenance"]] == ["part-001", "part-002"]
    assert connection.transaction_events == []
    assert all("ORDER BY created_at,id" in sql for sql, _ in connection.queries[1:3])
    assert "ORDER BY provider_id,part_number,id" in connection.queries[3][0]


def test_repository_context_rejects_copy_before_delivery_approval():
    connection = _TrackedConnection()
    connection.working_copies["working-001"] = {
        "id": "working-001",
        "status": "draft",
    }
    with _repository_with(connection) as repository:
        with pytest.raises(KeyError, match="delivery-approved"):
            repository.design_lesson_review_context(
                "working-001", organization_id="org-001", design_group_id="group-001"
            )


def test_repository_context_rejects_foreign_organization_or_design_group_in_query():
    connection = _TrackedConnection()
    connection.working_copies["working-001"].update(
        organization_id="org-001", design_group_id="group-001"
    )
    with _repository_with(connection) as repository:
        for organization_id, design_group_id in (
            ("org-other", "group-001"),
            ("org-001", "group-other"),
        ):
            with pytest.raises(KeyError, match="delivery-approved"):
                repository.design_lesson_review_context(
                    "working-001",
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )


def test_service_review_approval_uses_one_confirmation_for_the_whole_package():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        def forbid_legacy_staging(*_args, **_kwargs):
            raise AssertionError("review approval used a legacy lesson-id staging API")

        service.design_lesson_staging.verify = forbid_legacy_staging
        service.design_lesson_staging.package_paths = forbid_legacy_staging
        service.design_lesson_staging.evidence_paths = forbid_legacy_staging

        result = service.design_lesson_review_approve(
            review_id=prepared["review_id"],
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {prepared['review_id']}",
        )

        assert result["status"] == "stored-and-retrievable"
        assert service.repository.approved_assertion_count == 3
        assert "expected_package_sha256" not in service.repository.public_calls
        approval_call = service.repository.approve_calls[0]
        review_row = service.repository.reviews[prepared["review_id"]]
        assert approval_call["verified_review_card_sha256"] == review_row["review_card_sha256"]
        assert approval_call["verified_review_path"] == review_row["review_path"]
        assert approval_call["verified_package_path"] == review_row["package_path"]
    finally:
        temporary.cleanup()


def test_confirmed_to_retrievable_public_flow_publishes_every_assertion_once():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        working_copy_id = package["source"]["working_copy_id"]
        service.repository.context["working_copy"].update(
            status="draft",
            approved_final_sha256=None,
            approved_final_artifact_path=None,
        )

        delivery = service.design_delivery_approve(
            working_copy_id, f"批准 {working_copy_id}"
        )
        context = service.design_lesson_review_context(working_copy_id)
        review = service.design_lesson_review_prepare(
            working_copy_id=working_copy_id,
            package=package,
            evidence_items=evidence_items,
        )
        assert review["confirmation"] == f"批准设计经验 {review['review_id']}"
        result = service.design_lesson_review_approve(
            review_id=review["review_id"],
            reviewer_text="批准整组设计经验",
            confirmation=review["confirmation"],
        )

        assert delivery["design_lesson_review"] == {
            "required": True,
            "working_copy_id": working_copy_id,
            "next_action": "design_lesson_review_context",
        }
        assert context["working_copy_id"] == working_copy_id
        assert result["status"] == "stored-and-retrievable"
        assert result["retrieval_probe"]["eligible"] is True
        assert [call["review_id"] for call in service.repository.approve_calls] == [
            review["review_id"]
        ]
        searchable = service.design_lesson_search("actuator clearance")
        assert [item["id"] for item in searchable] == [result["lesson"]["id"]]
        assert {
            item["assertion_key"] for item in searchable[0]["atomic_assertions"]
        } == {
            "actuator-clearance",
            "mount-alignment",
            "mount-inspection",
        }
        assert service.repository.approved_assertion_count == 3
    finally:
        temporary.cleanup()


def test_service_review_public_results_redact_owner_only_hashes_and_storage_paths():
    def assert_public_result(result: dict, review_id: str) -> None:
        serialized = json.dumps(result, ensure_ascii=False)
        assert re.search(r"(?i)[0-9a-f]{64}", serialized) is None
        for internal_marker in ("/artifacts/", "/reviews/", "/staging/"):
            assert internal_marker not in serialized
        public_review = result["review"]
        assert public_review["id"] == review_id
        assert public_review["review_id"] == review_id
        assert public_review["status"] == result["status"]
        assert public_review["lesson_id"] == "DL-REVIEW-001"
        assert public_review["working_copy_id"] == "00000000-0000-0000-0000-000000000011"
        assert public_review.get("retrieval_probe") == result["retrieval_probe"]
        for field in (
            "package_sha256",
            "review_card_sha256",
            "final_model_sha256",
            "approved_final_artifact_path",
            "review_path",
            "package_path",
        ):
            assert field not in public_review

    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        raw_row = service.repository.reviews[review_id]
        for field in (
            "package_sha256",
            "review_card_sha256",
            "final_model_sha256",
            "approved_final_artifact_path",
            "review_path",
            "package_path",
        ):
            assert field in raw_row

        approved = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="Approve complete review",
            confirmation=f"批准设计经验 {review_id}",
        )
        assert_public_result(approved, review_id)
        status = service.design_lesson_review_status(review_id, retry=False)
        assert_public_result(status, review_id)
    finally:
        temporary.cleanup()

    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        rejected = service.design_lesson_review_reject(
            review_id=review_id,
            reviewer_text="Not sufficiently general",
            confirmation=f"拒绝设计经验 {review_id}",
        )
        assert_public_result(rejected, review_id)
    finally:
        temporary.cleanup()


def test_service_review_status_retries_projection_and_retrieval_without_reapproval():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        projection_results = iter(
            [
                {
                    "status": "deferred",
                    "reason": "Neo4j unavailable",
                    "authoritative_write_preserved": True,
                },
                {
                    "processed": 4,
                    "failed": [],
                    "processed_events": [
                        {
                            "event_type": "design_lesson.approved",
                            "aggregate_type": "design_lesson",
                            "aggregate_id": "approved-lesson-001",
                        },
                        {
                            "event_type": "design_lesson_review.approved",
                            "aggregate_type": "design_lesson_review",
                            "aggregate_id": prepared["review_id"],
                        },
                    ],
                },
            ]
        )
        service._safe_projection = lambda: next(projection_results)

        pending = service.design_lesson_review_approve(
            review_id=prepared["review_id"],
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {prepared['review_id']}",
        )
        assert pending["status"] == "approved-retrieval-pending"
        assert len(service.repository.approve_calls) == 1
        assert service.repository.approved_assertion_count == 3

        completed = service.design_lesson_review_status(prepared["review_id"])

        assert completed["status"] == "stored-and-retrievable"
        assert len(service.repository.approve_calls) == 1
        probe = completed["review"]["retrieval_probe"]
        assert probe["query"] == "actuator clearance"
        assert probe["conditions"] == ["moving-assembly"]
        assert probe["matched_lesson_id"] == "approved-lesson-001"
        assert probe["projection"] == {
            "processed": 4,
            "failed": [],
            "processed_events": [
                {
                    "event_type": "design_lesson.approved",
                    "aggregate_type": "design_lesson",
                    "aggregate_id": "approved-lesson-001",
                },
                {
                    "event_type": "design_lesson_review.approved",
                    "aggregate_type": "design_lesson_review",
                    "aggregate_id": prepared["review_id"],
                },
            ],
        }
        assert probe["status"] == "stored-and-retrievable"
    finally:
        temporary.cleanup()


def test_same_confirmation_on_pending_review_advances_without_republishing():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        projections = iter(
            [
                {"processed": 0, "failed": [], "processed_events": []},
                {
                    "processed": 2,
                    "failed": [],
                    "processed_events": [
                        {
                            "event_type": "design_lesson.approved",
                            "aggregate_type": "design_lesson",
                            "aggregate_id": "approved-lesson-001",
                        },
                        {
                            "event_type": "design_lesson_review.approved",
                            "aggregate_type": "design_lesson_review",
                            "aggregate_id": review_id,
                        },
                    ],
                },
            ]
        )
        service._safe_projection = lambda: next(projections)

        first = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=prepared["confirmation"],
        )
        repeated = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=prepared["confirmation"],
        )

        assert first["status"] == "approved-retrieval-pending"
        assert repeated["status"] == "stored-and-retrievable"
        assert repeated["idempotent"] is True
        assert len(service.repository.approve_calls) == 1
        assert service.repository.approved_assertion_count == 3
    finally:
        temporary.cleanup()


@pytest.mark.parametrize(
    "projection_factory",
    [
        lambda _review_id: {"processed": 0, "failed": []},
        lambda _review_id: {
            "processed": 2,
            "failed": [],
            "processed_aggregates": [
                {"aggregate_type": "design_lesson", "aggregate_id": "another-lesson"},
                {"aggregate_type": "design_lesson_review", "aggregate_id": "another-review"},
            ],
        },
        lambda _review_id: {
            "processed": 1,
            "failed": [],
            "processed_aggregates": [
                {"aggregate_type": "design_lesson", "aggregate_id": "approved-lesson-001"},
            ],
        },
        lambda review_id: {
            "processed": 1,
            "failed": [],
            "processed_aggregates": [
                {"aggregate_type": "design_lesson", "aggregate_id": "approved-lesson-001"},
            ],
            "claimed_aggregates": [
                {"aggregate_type": "design_lesson_review", "aggregate_id": review_id},
            ],
            "remaining_hint": 1,
        },
    ],
    ids=["empty", "unrelated", "partial", "leased-backlogged"],
)
def test_service_review_projection_requires_explicit_review_and_lesson_proof(
    projection_factory,
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service._safe_projection = lambda: projection_factory(review_id)

        result = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )

        assert result["status"] == "approved-retrieval-pending"
        assert len(service.repository.approve_calls) == 1
        assert result["review"]["retrieval_probe"]["projection_proved_review"] is False
    finally:
        temporary.cleanup()


def test_service_review_projection_explicitly_proves_the_review_and_lesson():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service._safe_projection = lambda: {
            "processed": 2,
            "failed": [],
            "processed_events": [
                {
                    "event_type": "design_lesson.approved",
                    "aggregate_type": "design_lesson",
                    "aggregate_id": "approved-lesson-001",
                },
                {
                    "event_type": "design_lesson_review.approved",
                    "aggregate_type": "design_lesson_review",
                    "aggregate_id": review_id,
                },
            ],
        }

        result = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )

        assert result["status"] == "stored-and-retrievable"
        assert result["review"]["retrieval_probe"]["projection_proved_review"] is True
        assert result["retrieval_probe"]["eligible"] is True
        assert result["retrieval_probe"]["match"]["eligible"] is True
    finally:
        temporary.cleanup()


def test_service_review_projection_rejects_aggregate_only_and_wrong_event_types():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service._safe_projection = lambda: {
            "processed": 2,
            "failed": [],
            "processed_aggregates": [
                {"aggregate_type": "design_lesson", "aggregate_id": "approved-lesson-001"},
                {"aggregate_type": "design_lesson_review", "aggregate_id": review_id},
            ],
            "processed_events": [
                {
                    "event_type": "design_lesson.superseded",
                    "aggregate_type": "design_lesson",
                    "aggregate_id": "approved-lesson-001",
                },
                {
                    "event_type": "design_lesson_review.prepared",
                    "aggregate_type": "design_lesson_review",
                    "aggregate_id": review_id,
                },
            ],
        }

        result = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )

        assert result["status"] == "approved-retrieval-pending"
        assert result["retrieval_probe"]["projection_witnesses"] == []
        assert result["retrieval_probe"]["projection_proved_review"] is False
    finally:
        temporary.cleanup()


def test_service_review_projection_accumulates_exact_events_across_retry_batches():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        lesson_event = {
            "event_id": "event-lesson-approved",
            "event_type": "design_lesson.approved",
            "aggregate_type": "design_lesson",
            "aggregate_id": "approved-lesson-001",
        }
        review_event = {
            "event_id": "event-review-approved",
            "event_type": "design_lesson_review.approved",
            "aggregate_type": "design_lesson_review",
            "aggregate_id": review_id,
        }
        projection_results = iter(
            [
                {"processed": 1, "failed": [], "processed_events": [lesson_event]},
                {"processed": 1, "failed": [], "processed_events": [review_event]},
                {"processed": 0, "failed": [], "processed_events": []},
            ]
        )
        service._safe_projection = lambda: next(projection_results)

        pending = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )
        assert pending["status"] == "approved-retrieval-pending"
        assert pending["retrieval_probe"]["projection_witnesses"] == [lesson_event]

        completed = service.design_lesson_review_status(review_id)
        assert completed["status"] == "stored-and-retrievable"
        assert completed["retrieval_probe"]["projection_witnesses"] == [
            lesson_event,
            review_event,
        ]

        repeated = service.design_lesson_review_status(review_id)
        assert repeated["status"] == "stored-and-retrievable"
        assert repeated["retrieval_probe"]["projection_witnesses"] == [
            lesson_event,
            review_event,
        ]
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_projection_partial_witness_persists_across_empty_retries():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        lesson_event = {
            "event_type": "design_lesson.approved",
            "aggregate_type": "design_lesson",
            "aggregate_id": "approved-lesson-001",
        }
        projection_results = iter(
            [
                {"processed": 1, "failed": [], "processed_events": [lesson_event]},
                {"processed": 0, "failed": [], "processed_events": []},
            ]
        )
        service._safe_projection = lambda: next(projection_results)

        first = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )
        second = service.design_lesson_review_status(review_id)

        assert first["status"] == "approved-retrieval-pending"
        assert second["status"] == "approved-retrieval-pending"
        assert second["retrieval_probe"]["projection_witnesses"] == [lesson_event]
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_projection_witnesses_survive_search_failure_until_empty_retry():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        lesson_event = {
            "event_type": "design_lesson.approved",
            "aggregate_type": "design_lesson",
            "aggregate_id": "approved-lesson-001",
        }
        review_event = {
            "event_type": "design_lesson_review.approved",
            "aggregate_type": "design_lesson_review",
            "aggregate_id": review_id,
        }
        projection_results = iter(
            [
                {"processed": 1, "failed": [], "processed_events": [lesson_event]},
                {"processed": 1, "failed": [], "processed_events": [review_event]},
                {"processed": 0, "failed": [], "processed_events": []},
            ]
        )
        service._safe_projection = lambda: next(projection_results)

        first = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )
        assert first["status"] == "approved-retrieval-pending"

        service.repository.search_error = RuntimeError("temporary search failure")
        failed = service.design_lesson_review_status(review_id)
        assert failed["status"] == "approved-retrieval-pending"
        assert failed["failure"]["stage"] == "search"
        assert failed["retrieval_probe"]["projection_witnesses"] == [
            lesson_event,
            review_event,
        ]

        service.repository.search_error = None
        completed = service.design_lesson_review_status(review_id)
        assert completed["status"] == "stored-and-retrievable"
        assert completed["retrieval_probe"]["projection_witnesses"] == [
            lesson_event,
            review_event,
        ]
        assert completed["retrieval_probe"]["eligible"] is True
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_witness_failure_returns_and_persists_pending_probe(monkeypatch):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )

        def fail_witness(*_args, **_kwargs):
            raise RuntimeError("witness failed")

        monkeypatch.setattr(
            "mechanical_design_agent.service.satisfying_conditions", fail_witness
        )
        result = service.design_lesson_review_approve(
            review_id=prepared["review_id"],
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {prepared['review_id']}",
        )

        assert result["status"] == "approved-retrieval-pending"
        assert result["failure"]["stage"] == "condition-witness"
        assert result["retrieval_probe"]["failure"]["error"] == "RuntimeError: witness failed"
        assert service.repository.reviews[prepared["review_id"]]["retrieval_probe"] == result["retrieval_probe"]
        assert len(service.repository.approve_calls) == 1

        retried = service.design_lesson_review_status(prepared["review_id"])
        assert retried["status"] == "approved-retrieval-pending"
        assert retried["failure"]["stage"] == "condition-witness"
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_search_failure_returns_and_persists_pending_probe():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        service.repository.search_error = RuntimeError("search failed")

        result = service.design_lesson_review_approve(
            review_id=prepared["review_id"],
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {prepared['review_id']}",
        )

        assert result["status"] == "approved-retrieval-pending"
        assert result["failure"]["stage"] == "search"
        assert result["retrieval_probe"]["failure"]["error"] == "RuntimeError: search failed"
        assert service.repository.reviews[prepared["review_id"]]["retrieval_probe"] == result["retrieval_probe"]
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_matcher_failure_returns_and_persists_pending_probe(monkeypatch):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )

        def fail_matcher(*_args, **_kwargs):
            raise RuntimeError("matcher failed")

        monkeypatch.setattr(
            "mechanical_design_agent.service.match_design_lesson", fail_matcher
        )
        result = service.design_lesson_review_approve(
            review_id=prepared["review_id"],
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {prepared['review_id']}",
        )

        assert result["status"] == "approved-retrieval-pending"
        assert result["failure"]["stage"] == "match"
        assert result["retrieval_probe"]["failure"]["error"] == "RuntimeError: matcher failed"
        assert service.repository.reviews[prepared["review_id"]]["retrieval_probe"] == result["retrieval_probe"]
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_probe_persistence_failure_returns_pending_without_reapproval():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        service.repository.probe_error = RuntimeError("probe persistence failed")

        result = service.design_lesson_review_approve(
            review_id=prepared["review_id"],
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {prepared['review_id']}",
        )

        assert result["status"] == "approved-retrieval-pending"
        assert result["failure"] == {
            "stage": "probe-persistence",
            "error": "RuntimeError: probe persistence failed",
        }
        assert result["retrieval_probe"]["failure"] == result["failure"]
        assert result["retrieval_probe"]["status"] == "approved-retrieval-pending"
        assert "retrieval_probe" not in service.repository.reviews[prepared["review_id"]]
        assert len(service.repository.probe_calls) == 1
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_recovers_acked_projection_witnesses_after_probe_persistence_failure():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        lesson_event = {
            "event_id": "event-lesson-approved",
            "event_type": "design_lesson.approved",
            "aggregate_type": "design_lesson",
            "aggregate_id": "approved-lesson-001",
        }
        review_event = {
            "event_id": "event-review-approved",
            "event_type": "design_lesson_review.approved",
            "aggregate_type": "design_lesson_review",
            "aggregate_id": review_id,
        }
        projections = iter(
            [
                {
                    "processed": 2,
                    "failed": [],
                    "processed_events": [lesson_event, review_event],
                },
                {"processed": 0, "failed": [], "processed_events": []},
            ]
        )

        def project_and_ack():
            projection = next(projections)
            service.repository.processed_projection_events.extend(
                projection["processed_events"]
            )
            return projection

        service._safe_projection = project_and_ack
        service.repository.probe_error = RuntimeError("probe persistence failed")

        first = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=prepared["confirmation"],
        )
        assert first["status"] == "approved-retrieval-pending"
        assert first["failure"]["stage"] == "probe-persistence"

        service.repository.probe_error = None
        completed = service.design_lesson_review_status(review_id)

        assert completed["status"] == "stored-and-retrievable"
        assert completed["retrieval_probe"]["projection_witnesses"] == [
            lesson_event,
            review_event,
        ]
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_exact_durable_projection_witnesses_override_unrelated_poison_failure():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service.repository.processed_projection_events.extend(
            [
                {
                    "event_id": "event-exact-lesson",
                    "event_type": "design_lesson.approved",
                    "aggregate_type": "design_lesson",
                    "aggregate_id": "approved-lesson-001",
                },
                {
                    "event_id": "event-exact-review",
                    "event_type": "design_lesson_review.approved",
                    "aggregate_type": "design_lesson_review",
                    "aggregate_id": review_id,
                },
            ]
        )
        poison = {
            "event_id": "event-unrelated-poison",
            "error": "RuntimeError: unrelated projection failed",
        }
        service._safe_projection = lambda: {
            "processed": 0,
            "failed": [poison],
            "remaining_hint": 1,
            "processed_events": [],
        }

        result = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=prepared["confirmation"],
        )

        assert result["status"] == "stored-and-retrievable"
        assert result["retrieval_probe"]["projection"]["failed"] == [poison]
        assert result["retrieval_probe"]["projection_proved_review"] is True
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


@pytest.mark.parametrize("missing", ["lesson", "review"])
def test_poison_failure_with_one_missing_exact_durable_witness_stays_pending(
    missing,
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        exact = {
            "lesson": {
                "event_id": "event-exact-lesson",
                "event_type": "design_lesson.approved",
                "aggregate_type": "design_lesson",
                "aggregate_id": "approved-lesson-001",
            },
            "review": {
                "event_id": "event-exact-review",
                "event_type": "design_lesson_review.approved",
                "aggregate_type": "design_lesson_review",
                "aggregate_id": review_id,
            },
        }
        service.repository.processed_projection_events.append(
            exact["review" if missing == "lesson" else "lesson"]
        )
        service._safe_projection = lambda: {
            "processed": 0,
            "failed": [
                {
                    "event_id": "event-unrelated-poison",
                    "error": "RuntimeError: unrelated projection failed",
                }
            ],
            "processed_aggregates": [
                {
                    "aggregate_type": exact[missing]["aggregate_type"],
                    "aggregate_id": exact[missing]["aggregate_id"],
                }
            ],
            "processed_events": [
                {
                    **exact[missing],
                    "event_type": "design_lesson.superseded"
                    if missing == "lesson"
                    else "design_lesson_review.prepared",
                }
            ],
        }

        result = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=prepared["confirmation"],
        )

        assert result["status"] == "approved-retrieval-pending"
        assert result["retrieval_probe"]["projection_proved_review"] is False
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def test_service_review_approval_rejects_a_symlinked_final_artifact():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        artifact_path = Path(
            service.repository.context["working_copy"][
                "approved_final_artifact_path"
            ]
        )
        replacement = service.settings.workspace / "replacement.FCStd"
        replacement.write_bytes(b"approved-final-model")
        os.chmod(artifact_path, 0o644)
        artifact_path.unlink()
        artifact_path.symlink_to(replacement)

        with pytest.raises(ValueError, match="stable regular file"):
            service.design_lesson_review_approve(
                review_id=prepared["review_id"],
                reviewer_text="批准整组设计经验",
                confirmation=f"批准设计经验 {prepared['review_id']}",
            )

        assert service.repository.approve_calls == []
        assert service.repository.approved_assertion_count == 0
    finally:
        temporary.cleanup()


def test_service_review_approval_marks_tampered_immutable_card_invalid():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        review_path = Path(service.repository.reviews[review_id]["review_path"])
        review_path.write_text("tampered review card\n", encoding="utf-8")

        with pytest.raises(ValueError, match="review card changed after preparation"):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=f"批准设计经验 {review_id}",
            )

        assert service.repository.reviews[review_id]["status"] == "invalid"
        assert service.repository.invalidate_calls == [
            {
                "review_id": review_id,
                "reviewer_id": service.settings.actor_id,
                "reason": "immutable review binding verification failed",
            }
        ]
        assert service.repository.lessons == {}
        assert service.repository.approve_calls == []
    finally:
        temporary.cleanup()


@pytest.mark.parametrize("artifact_kind", ["package", "evidence"])
def test_service_review_approval_marks_tampered_package_or_evidence_invalid(
    artifact_kind,
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        package_sha256 = service.repository.reviews[review_id]["package_sha256"]
        if artifact_kind == "package":
            target = Path(service.repository.reviews[review_id]["package_path"])
            target.write_text("{}\n", encoding="utf-8")
        else:
            _descriptor, target = service.design_lesson_staging.review_evidence_paths(
                package_sha256
            )[0]
            os.chmod(target, 0o644)
            target.write_text("tampered evidence\n", encoding="utf-8")

        with pytest.raises(ValueError):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=f"批准设计经验 {review_id}",
            )

        assert service.repository.reviews[review_id]["status"] == "invalid"
        assert service.repository.lessons == {}
        assert service.repository.approve_calls == []
    finally:
        temporary.cleanup()


def test_service_review_approval_marks_approved_final_cas_checksum_drift_invalid():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        artifact_path = Path(
            service.repository.reviews[review_id]["approved_final_artifact_path"]
        )
        os.chmod(artifact_path, 0o644)
        artifact_path.write_bytes(b"tampered-approved-model")
        os.chmod(artifact_path, 0o444)

        with pytest.raises(OSError, match="checksum mismatch"):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=prepared["confirmation"],
            )

        assert service.repository.reviews[review_id]["status"] == "invalid"
        assert len(service.repository.invalidate_calls) == 1
        assert service.repository.lessons == {}
        assert service.repository.approve_calls == []
    finally:
        temporary.cleanup()


def test_service_review_approval_does_not_invalidate_generic_storage_failure():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]

        def unavailable_storage(*_args, **_kwargs):
            raise OSError("storage infrastructure unavailable")

        service.artifacts.verify_file = unavailable_storage

        with pytest.raises(OSError, match="infrastructure unavailable"):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=prepared["confirmation"],
            )

        assert service.repository.reviews[review_id]["status"] == (
            "awaiting-engineer-review"
        )
        assert service.repository.invalidate_calls == []
        assert service.repository.lessons == {}
        assert service.repository.approve_calls == []
    finally:
        temporary.cleanup()


def test_service_review_approval_does_not_invalidate_generic_repository_failure():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]

        def unavailable_repository(**_kwargs):
            raise ValueError("database infrastructure unavailable")

        service.repository.approve_design_lesson = unavailable_repository

        with pytest.raises(ValueError, match="database infrastructure unavailable"):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=prepared["confirmation"],
            )

        assert service.repository.reviews[review_id]["status"] == (
            "awaiting-engineer-review"
        )
        assert service.repository.invalidate_calls == []
        assert service.repository.lessons == {}
    finally:
        temporary.cleanup()


@pytest.mark.parametrize(
    ("row_field", "mismatched_value"),
    [
        ("review_card_sha256", "9" * 64),
        ("review_path", "/reviews/another-review/review.md"),
        ("package_path", "/staging/review-another/lesson.json"),
    ],
)
def test_service_review_approval_rejects_database_mismatch_with_verified_local_review(
    row_field, mismatched_value
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service.repository.reviews[review_id][row_field] = mismatched_value

        with pytest.raises(ValueError, match="review.*binding"):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=f"批准设计经验 {review_id}",
            )

        assert service.repository.approve_calls == []
        assert service.repository.approved_assertion_count == 0
        assert service.repository.lessons == {}
        assert service.repository.reviews[review_id]["status"] == "invalid"
    finally:
        temporary.cleanup()


def test_service_review_approval_rejects_foreign_design_group_before_local_reads():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service.repository.reviews[review_id]["design_group_id"] = "group-foreign"

        def forbid_local_read(*_args, **_kwargs):
            raise AssertionError("foreign-group approval read local review artifacts")

        service.design_lesson_reviews.verify = forbid_local_read
        service.design_lesson_staging.review_package_paths = forbid_local_read

        with pytest.raises(PermissionError, match="configured scope"):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=f"批准设计经验 {review_id}",
            )

        assert service.repository.approve_calls == []
        assert service.repository.probe_calls == []
        assert service.repository.lessons == {}
        assert service.repository.reviews[review_id]["status"] == "awaiting-engineer-review"
    finally:
        temporary.cleanup()


@pytest.mark.parametrize(
    "review_status", ["approved-retrieval-pending", "stored-and-retrievable"]
)
def test_service_review_status_rejects_foreign_design_group_before_retry_or_lesson_read(
    review_status,
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service.repository.reviews[review_id].update(
            design_group_id="group-foreign",
            status=review_status,
            published_design_lesson_id="approved-lesson-001",
        )

        def forbid_foreign_work(*_args, **_kwargs):
            raise AssertionError("foreign-group status performed retry or lesson read")

        service._safe_projection = forbid_foreign_work
        service.repository.get_design_lesson = forbid_foreign_work

        with pytest.raises(PermissionError, match="configured scope"):
            service.design_lesson_review_status(review_id)

        assert service.repository.probe_calls == []
        assert service.repository.approve_calls == []
        assert service.repository.reviews[review_id]["status"] == review_status
    finally:
        temporary.cleanup()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_service_review_decision_requires_the_exact_confirmation(decision):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        if decision == "approve":
            operation = lambda: service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="Approve",
                confirmation=f" 批准设计经验 {review_id} ",
            )
        else:
            operation = lambda: service.design_lesson_review_reject(
                review_id=review_id,
                reviewer_text="Reject",
                confirmation=f" 拒绝设计经验 {review_id} ",
            )

        with pytest.raises(ValueError, match="canonical confirmation"):
            operation()

        assert service.repository.approved_assertion_count == 0
        assert service.repository.reviews[review_id]["status"] == "awaiting-engineer-review"
    finally:
        temporary.cleanup()


def test_service_review_rejection_publishes_nothing_and_cannot_be_approved():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]

        rejected = service.design_lesson_review_reject(
            review_id=review_id,
            reviewer_text="Not sufficiently general",
            confirmation=f"拒绝设计经验 {review_id}",
        )

        assert rejected["status"] == "rejected"
        assert service.repository.approved_assertion_count == 0
        assert service.repository.lessons == {}
        with pytest.raises(ValueError, match="awaiting-engineer-review"):
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="Changed mind",
                confirmation=f"批准设计经验 {review_id}",
            )
    finally:
        temporary.cleanup()


@pytest.mark.parametrize(
    ("row_field", "foreign_value"),
    [
        ("organization_id", "org-foreign"),
        ("design_group_id", "group-foreign"),
    ],
)
def test_service_review_rejection_rejects_foreign_scope_before_repository_mutation(
    row_field, foreign_value
):
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        service.repository.reviews[review_id][row_field] = foreign_value

        with pytest.raises(PermissionError, match="configured"):
            service.design_lesson_review_reject(
                review_id=review_id,
                reviewer_text="Reject foreign data",
                confirmation=f"拒绝设计经验 {review_id}",
            )

        assert service.repository.reject_calls == []
        assert service.repository.reviews[review_id]["status"] == "awaiting-engineer-review"
        assert service.repository.lessons == {}
    finally:
        temporary.cleanup()


def test_service_completed_review_approval_is_idempotent():
    temporary, service, package, evidence_items = _review_preparation_fixture()
    try:
        prepared = service.design_lesson_review_prepare(
            package["source"]["working_copy_id"], package, evidence_items
        )
        review_id = prepared["review_id"]
        first = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )

        repeated = service.design_lesson_review_approve(
            review_id=review_id,
            reviewer_text="批准整组设计经验",
            confirmation=f"批准设计经验 {review_id}",
        )

        assert first["status"] == "stored-and-retrievable"
        assert repeated["status"] == "stored-and-retrievable"
        assert repeated["idempotent"] is True
        assert repeated["lesson"]["id"] == first["lesson"]["id"]
        assert len(service.repository.approve_calls) == 1
    finally:
        temporary.cleanup()


def _live_fixture_database_state(
    repository: PostgresRepository, organization_id: str
) -> dict[str, list[str]]:
    with repository.connection() as connection:
        working_copy_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM design_working_copies WHERE organization_id=%s",
                (organization_id,),
            ).fetchall()
        ]
        lesson_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM design_lesson_events WHERE organization_id=%s",
                (organization_id,),
            ).fetchall()
        ]
        assertion_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM knowledge_assertions WHERE organization_id=%s",
                (organization_id,),
            ).fetchall()
        ]
        review_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM design_lesson_reviews WHERE organization_id=%s",
                (organization_id,),
            ).fetchall()
        ]
        family_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM product_families WHERE organization_id=%s",
                (organization_id,),
            ).fetchall()
        ]
        model_revision_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM model_revisions WHERE organization_id=%s",
                (organization_id,),
            ).fetchall()
        ]
    return {
        "working_copy_ids": working_copy_ids,
        "lesson_ids": lesson_ids,
        "assertion_ids": assertion_ids,
        "review_ids": review_ids,
        "family_ids": family_ids,
        "model_revision_ids": model_revision_ids,
    }


def _remove_live_design_lesson_fixture(
    service: MechanicalDesignService, organization_id: str
) -> None:
    state = _live_fixture_database_state(service.repository, organization_id)
    graph_ids = [
        *state["lesson_ids"],
        *state["assertion_ids"],
        *state["family_ids"],
        *state["model_revision_ids"],
    ]
    graph_cleanup_error = None
    try:
        with service.projection._driver() as driver, driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.projection_owner=$owner AND ("
                "n.id IN $graph_ids OR n.review_id IN $review_ids) DETACH DELETE n",
                owner="freecad-mechanical-design-agent",
                graph_ids=graph_ids,
                review_ids=state["review_ids"],
            ).consume()
    except Exception as error:
        graph_cleanup_error = error

    aggregate_ids = [
        *state["working_copy_ids"],
        *state["lesson_ids"],
        *state["assertion_ids"],
        *state["review_ids"],
        *state["family_ids"],
    ]
    with service.repository.connection() as connection, connection.transaction():
        connection.execute(
            "DELETE FROM design_lesson_reviews WHERE organization_id=%s",
            (organization_id,),
        )
        if state["lesson_ids"]:
            connection.execute(
                "DELETE FROM design_lesson_report_bindings WHERE lesson_event_id=ANY(%s::uuid[])",
                (state["lesson_ids"],),
            )
        if state["assertion_ids"]:
            connection.execute(
                "DELETE FROM review_events WHERE assertion_id=ANY(%s::uuid[])",
                (state["assertion_ids"],),
            )
            connection.execute(
                "DELETE FROM knowledge_search_documents WHERE assertion_id=ANY(%s::uuid[])",
                (state["assertion_ids"],),
            )
        connection.execute(
            "DELETE FROM design_lesson_events WHERE organization_id=%s",
            (organization_id,),
        )
        connection.execute(
            "DELETE FROM knowledge_assertions WHERE organization_id=%s",
            (organization_id,),
        )
        if state["working_copy_ids"]:
            connection.execute(
                "DELETE FROM validation_reports WHERE working_copy_id=ANY(%s::uuid[])",
                (state["working_copy_ids"],),
            )
            connection.execute(
                "DELETE FROM design_change_sets WHERE working_copy_id=ANY(%s::uuid[])",
                (state["working_copy_ids"],),
            )
        connection.execute(
            "DELETE FROM design_working_copies WHERE organization_id=%s",
            (organization_id,),
        )
        if aggregate_ids:
            connection.execute(
                "DELETE FROM outbox_events WHERE aggregate_id=ANY(%s)",
                (aggregate_ids,),
            )
        connection.execute(
            "DELETE FROM model_revisions WHERE organization_id=%s",
            (organization_id,),
        )
        connection.execute(
            "DELETE FROM product_families WHERE organization_id=%s",
            (organization_id,),
        )
        connection.execute(
            "DELETE FROM artifacts WHERE organization_id=%s",
            (organization_id,),
        )
        connection.execute(
            "DELETE FROM actors WHERE organization_id=%s", (organization_id,)
        )
        connection.execute(
            "DELETE FROM design_groups WHERE organization_id=%s",
            (organization_id,),
        )
        connection.execute(
            "DELETE FROM organizations WHERE id=%s", (organization_id,)
        )
    if graph_cleanup_error is not None:
        raise graph_cleanup_error


@pytest.mark.skipif(
    not (LIVE_DATABASE_URL and LIVE_NEO4J_CONFIGURED),
    reason="live PostgreSQL and Neo4j configuration is required",
)
def test_live_confirmed_to_retrievable_flow_is_atomic_projected_and_searchable():
    token = uuid.uuid4().hex[:12]
    organization_id = f"org-task8-{token}"
    design_group_id = f"group-task8-{token}"
    family_id = f"PF-TASK8-{token.upper()}"
    actor_id = f"owner-task8-{token}"
    foreign_organization_id = f"org-task8-foreign-{token}"
    foreign_design_group_id = f"group-task8-foreign-{token}"
    foreign_actor_id = f"owner-task8-foreign-{token}"
    unauthorized_actor_id = f"engineer-task8-{token}"
    sentinel_organization_id = f"org-task8-sentinel-{token}"
    sentinel_design_group_id = f"group-task8-sentinel-{token}"
    sentinel_actor_id = f"owner-task8-sentinel-{token}"
    sentinel_lesson_id = str(uuid.uuid4())
    sentinel_lesson_key = f"DL-TASK8-SENTINEL-{token.upper()}"
    package_root = Path(__file__).resolve().parents[1]
    service = None
    sentinel_before = None
    review_id = None
    lesson_id = None

    with tempfile.TemporaryDirectory(prefix="design-lesson-task8-") as temporary:
        workspace = Path(temporary)
        (workspace / "config").mkdir()
        (workspace / "config" / "standard_parts_sources.json").write_text(
            json.dumps(
                {
                    "verified_local_catalog": {
                        "global_root": str(workspace / "synthetic-standard-parts")
                    }
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        family_config = workspace / "family.json"
        family_config.write_text(
            json.dumps(
                {
                    "schema_version": "product-family-bootstrap/v1",
                    "organization_id": organization_id,
                    "organization_name": "Task 8 disposable organization",
                    "design_group_id": design_group_id,
                    "design_group_name": "Task 8 disposable group",
                    "family_id": family_id,
                    "family_name": "Task 8 disposable family",
                    "aliases": [],
                    "status": "active",
                    "subfamily_mode": "discover-and-confirm",
                    "question_batch_limit": 5,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        settings = Settings(
            workspace=workspace,
            package_root=package_root,
            database_url=LIVE_DATABASE_URL,
            neo4j_uri=os.environ["MECH_DESIGN_NEO4J_URI"],
            neo4j_user=os.environ["MECH_DESIGN_NEO4J_USER"],
            neo4j_password=os.environ["MECH_DESIGN_NEO4J_PASSWORD"],
            freecadcmd=Path("/bin/false"),
            actor_id=actor_id,
            artifact_root=workspace / "artifacts",
            family_config_path=family_config,
        )
        try:
            service = MechanicalDesignService(settings)
            assert service.bootstrap_error == ""
            assert service.projection.status()["status"] == "healthy"
            with service.repository.connection() as connection, connection.transaction():
                migration_rows = connection.execute(
                    "SELECT version,filename FROM schema_migrations ORDER BY version"
                ).fetchall()
                connection.execute(
                    "INSERT INTO organizations(id,name) VALUES (%s,%s)",
                    (sentinel_organization_id, "Synthetic untouched organization"),
                )
                connection.execute(
                    "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
                    (
                        sentinel_design_group_id,
                        sentinel_organization_id,
                        "Synthetic untouched group",
                    ),
                )
                connection.execute(
                    "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s)",
                    (
                        sentinel_actor_id,
                        sentinel_organization_id,
                        "Synthetic untouched owner",
                        "family_owner",
                    ),
                )
                connection.execute(
                    "INSERT INTO design_lesson_events("
                    "id,lesson_key,revision,organization_id,source_design_group_id,"
                    "codex_session_id,title,before_model_sha256,after_model_sha256,"
                    "problem,root_causes,corrections,prevention,applicability,"
                    "non_applicable_conditions,search_terms,evidence_manifest,"
                    "package_sha256,archived_package_path,status,approved_by,approval_text) "
                    "VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'[]'::jsonb,"
                    "'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'[]'::jsonb,ARRAY['sentinel'],"
                    "'[]'::jsonb,%s,%s,'approved',%s,%s)",
                    (
                        sentinel_lesson_id,
                        sentinel_lesson_key,
                        sentinel_organization_id,
                        sentinel_design_group_id,
                        f"task8-sentinel-session-{token}",
                        "Synthetic untouched design lesson",
                        "a" * 64,
                        "b" * 64,
                        (token * 6)[:64],
                        str(workspace / "synthetic-untouched-lesson.json"),
                        sentinel_actor_id,
                        "Synthetic sentinel approval",
                    ),
                )
                sentinel_before = connection.execute(
                    "SELECT id::text,lesson_key,status,package_sha256 FROM design_lesson_events "
                    "WHERE id=%s",
                    (sentinel_lesson_id,),
                ).fetchone()
            assert [int(row["version"]) for row in migration_rows] == list(range(1, 14))
            assert migration_rows[-1]["filename"] == "015_product_family_match_decisions.sql"
            assert sentinel_before is not None
            sentinel_before = dict(sentinel_before)

            source_path = workspace / "source.FCStd"
            source_path.write_bytes(b"task-8-disposable-final-model")
            source_sha256 = file_sha256(source_path)
            source_artifact_id = str(uuid.uuid4())
            source_model_revision_id = str(uuid.uuid4())
            with service.repository.connection() as connection, connection.transaction():
                connection.execute(
                    "INSERT INTO artifacts(id,organization_id,sha256,size_bytes,media_type,"
                    "storage_path,source_path) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        source_artifact_id,
                        organization_id,
                        source_sha256,
                        source_path.stat().st_size,
                        "application/x-freecad",
                        str(source_path),
                        str(source_path),
                    ),
                )
                connection.execute(
                    "INSERT INTO model_revisions(id,organization_id,design_group_id,family_id,"
                    "source_artifact_id,source_relative_path,family_folder,parser_version,status,manifest) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)",
                    (
                        source_model_revision_id,
                        organization_id,
                        design_group_id,
                        family_id,
                        source_artifact_id,
                        source_path.name,
                        "synthetic",
                        f"task8-{token}",
                        "confirmed",
                    ),
                )
            working = service.design_working_copy_create(
                source_path=str(source_path),
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id,
                model_revision_id=None,
            )
            working_copy_id = str(working["id"])
            final_sha256 = file_sha256(Path(working["working_path"]))
            retrieval = service.design_knowledge_retrieve(
                working_copy_id=working_copy_id,
                query="synthetic actuator alignment and clearance",
                design_features={"moving-assembly": True},
                used_knowledge_ids=[],
            )
            assert retrieval["retrieval_receipt"]["retrieval_status"] in {
                "completed",
                "completed_no_match",
            }
            change = service.design_change_record(
                working_copy_id=working_copy_id,
                change_phase="parameter_change",
                changes=[
                    {"target": "actuator.mount", "operation": "align"},
                    {"target": "actuator.mount", "operation": "verify-clearance"},
                ],
                knowledge_used=[],
                rationale="Task 8 isolated end-to-end proof",
                approval_envelope_draft={
                    "design_intent": {
                        "function": "Align a synthetic actuator mount"
                    },
                    "architecture": {
                        "mechanism": "synthetic fixed mounting interface"
                    },
                    "key_interfaces": [
                        {
                            "id": "synthetic-actuator-mount",
                            "contract": "aligned rigid interface",
                        }
                    ],
                    "user_constraints": [
                        {
                            "id": "synthetic-clearance",
                            "rule": "preserve positive clearance",
                        }
                    ],
                    "manufacturing_method": {"process": "synthetic machining"},
                    "material_constraints": [],
                    "validation_requirements": [
                        {
                            "id": "synthetic-shape-validity",
                            "rule": "valid model geometry",
                        }
                    ],
                },
            )
            change_set_id = str(change["id"])
            service.design_change_review(
                change_set_id,
                "approve",
                "Task 8 isolated change",
                f"批准 {change_set_id}",
            )
            service.design_change_applied(
                change_set_id, f"已应用 {change_set_id}"
            )
            geometry_report = workspace / "geometry-validation.json"
            geometry_report.write_text(
                '{"status":"passed","validator":"freecad-model-validation"}\n',
                encoding="utf-8",
            )
            assembly_report = workspace / "assembly-validation.json"
            assembly_report.write_text(
                '{"status":"passed","validator":"assembly-validation"}\n',
                encoding="utf-8",
            )
            service.design_validation_record(
                working_copy_id=working_copy_id,
                change_set_id=change_set_id,
                status="passed",
                checks=[
                    {
                        "check_id": "shape.valid",
                        "status": "passed",
                        "validator": "freecad-model-validation",
                    }
                ],
                report_path=str(geometry_report),
                validation_kind="geometry_model",
            )
            service.design_validation_record(
                working_copy_id=working_copy_id,
                change_set_id=change_set_id,
                status="passed",
                checks=[{"check_id": "assembly.complete", "status": "passed"}],
                report_path=str(assembly_report),
                validation_kind="assembly_completeness",
            )
            summary = service.design_confirmation_record(
                working_copy_id=working_copy_id,
                lesson_summary={
                    "lesson": "Preserve source identity and verify actuator clearance"
                },
                confirmation=f"模型设计确认 {working_copy_id}",
            )
            assert summary["summary_status"] == "completed"

            with service.repository.connection() as connection, connection.transaction():
                connection.execute(
                    "INSERT INTO organizations(id,name) VALUES (%s,%s)",
                    (foreign_organization_id, "Task 8 foreign authorization probe"),
                )
                connection.execute(
                    "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
                    (
                        foreign_design_group_id,
                        foreign_organization_id,
                        "Task 8 foreign group",
                    ),
                )
                connection.execute(
                    "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s)",
                    (
                        foreign_actor_id,
                        foreign_organization_id,
                        "Task 8 foreign owner",
                        "family_owner",
                    ),
                )
                connection.execute(
                    "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s)",
                    (
                        unauthorized_actor_id,
                        organization_id,
                        "Task 8 unauthorized engineer",
                        "engineer",
                    ),
                )
            artifact_files_before = {
                path.relative_to(settings.artifact_root)
                for path in settings.artifact_root.rglob("*")
                if path.is_file()
            }
            for unauthorized_scope in (
                {
                    "actor_id": foreign_actor_id,
                    "organization_id": organization_id,
                    "design_group_id": design_group_id,
                },
                {
                    "actor_id": actor_id,
                    "organization_id": foreign_organization_id,
                    "design_group_id": design_group_id,
                },
                {
                    "actor_id": actor_id,
                    "organization_id": organization_id,
                    "design_group_id": foreign_design_group_id,
                },
                {
                    "actor_id": unauthorized_actor_id,
                    "organization_id": organization_id,
                    "design_group_id": design_group_id,
                },
            ):
                with pytest.raises((PermissionError, KeyError), match="delivery approval"):
                    service.repository.approve_delivery(
                        working_copy_id,
                        unauthorized_scope["actor_id"],
                        f"批准 {working_copy_id}",
                        final_sha256,
                        "/must-not-be-bound/unauthorized.FCStd",
                        organization_id=unauthorized_scope["organization_id"],
                        design_group_id=unauthorized_scope["design_group_id"],
                    )
            with service.repository.connection() as connection:
                unauthorized_row = connection.execute(
                    "SELECT status,approved_final_sha256,approved_final_artifact_path "
                    "FROM design_working_copies WHERE id=%s",
                    (working_copy_id,),
                ).fetchone()
                unauthorized_outbox_count = connection.execute(
                    "SELECT count(*) AS count FROM outbox_events "
                    "WHERE aggregate_type='design_working_copy' AND aggregate_id=%s "
                    "AND event_type='design_working_copy.approved'",
                    (working_copy_id,),
                ).fetchone()["count"]
            assert unauthorized_row["status"] == "draft"
            assert unauthorized_row["approved_final_sha256"] is None
            assert unauthorized_row["approved_final_artifact_path"] is None
            assert int(unauthorized_outbox_count) == 0
            assert artifact_files_before == {
                path.relative_to(settings.artifact_root)
                for path in settings.artifact_root.rglob("*")
                if path.is_file()
            }

            delivery = service.design_delivery_approve(
                working_copy_id, f"批准 {working_copy_id}"
            )
            context = service.design_lesson_review_context(working_copy_id)
            assert delivery["design_lesson_review"]["required"] is True
            assert context["working_copy_id"] == working_copy_id
            assert context["final_model_sha256"] == final_sha256

            package = _review_package()
            package.update(
                lesson_id=f"DL-TASK8-{token.upper()}",
                codex_session_id=f"task8-session-{token}",
            )
            package["source"] = {
                "organization_id": organization_id,
                "design_group_id": design_group_id,
                "family_id": family_id,
                "working_copy_id": working_copy_id,
                "change_set_ids": [change_set_id],
                "before_model_sha256": str(working["source_sha256"]),
                "after_model_sha256": final_sha256,
            }
            package["applicability"].update(
                required_conditions=["moving-assembly"],
                required_condition_expression={
                    "any_of": [
                        "catalog-fastener-present",
                        {"all_of": ["alignment-sensitive", "service-access"]},
                    ]
                },
            )
            package["non_applicable_conditions"] = ["sealed-unit"]
            evidence_items = [
                {
                    "evidence_id": "validation-evidence",
                    "path": geometry_report.name,
                    "role": "geometry_validation",
                    "media_type": "application/json",
                    "working_copy_id": working_copy_id,
                    "change_set_id": change_set_id,
                    "model_sha256": final_sha256,
                    "validation_kind": "geometry_model",
                }
            ]
            package["evidence_manifest"] = [
                {**evidence_items[0], "sha256": file_sha256(geometry_report)}
            ]

            review = service.design_lesson_review_prepare(
                working_copy_id=working_copy_id,
                package=package,
                evidence_items=evidence_items,
            )
            review_id = str(review["review_id"])
            assert review["confirmation"] == f"批准设计经验 {review_id}"
            with service.repository.connection() as connection:
                review_count = connection.execute(
                    "SELECT count(*) AS count FROM design_lesson_reviews WHERE working_copy_id=%s",
                    (working_copy_id,),
                ).fetchone()["count"]
            assert int(review_count) == 1

            result = service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text="批准整组设计经验",
                confirmation=review["confirmation"],
            )
            for _ in range(20):
                if result["status"] == "stored-and-retrievable":
                    break
                result = service.design_lesson_review_status(review_id)
            assert result["status"] == "stored-and-retrievable"
            assert result["retrieval_probe"]["eligible"] is True
            lesson_id = str(result["lesson"]["id"])
            witnesses = {
                (
                    item["event_type"],
                    item["aggregate_type"],
                    item["aggregate_id"],
                )
                for item in result["retrieval_probe"]["projection_witnesses"]
            }
            assert (
                "design_lesson.approved",
                "design_lesson",
                lesson_id,
            ) in witnesses
            assert (
                "design_lesson_review.approved",
                "design_lesson_review",
                review_id,
            ) in witnesses

            with service.repository.connection() as connection:
                review_row = connection.execute(
                    "SELECT status,published_design_lesson_id::text FROM design_lesson_reviews WHERE id=%s",
                    (review_id,),
                ).fetchone()
                lesson_row = connection.execute(
                    "SELECT status,lesson_key FROM design_lesson_events WHERE id=%s",
                    (lesson_id,),
                ).fetchone()
                assertion_rows = connection.execute(
                    "SELECT a.status,a.source_kind FROM design_lesson_assertions l "
                    "JOIN knowledge_assertions a ON a.id=l.assertion_id "
                    "WHERE l.lesson_event_id=%s ORDER BY l.sort_order",
                    (lesson_id,),
                ).fetchall()
                projected_events = connection.execute(
                    "SELECT event_type,aggregate_type,aggregate_id FROM outbox_events "
                    "WHERE processed_at IS NOT NULL AND ((aggregate_type='design_lesson' AND aggregate_id=%s) "
                    "OR (aggregate_type='design_lesson_review' AND aggregate_id=%s))",
                    (lesson_id, review_id),
                ).fetchall()
            assert review_row["status"] == "stored-and-retrievable"
            assert review_row["published_design_lesson_id"] == lesson_id
            assert lesson_row["status"] == "approved"
            assert lesson_row["lesson_key"] == package["lesson_id"]
            assert len(assertion_rows) == 3
            assert {row["status"] for row in assertion_rows} == {"approved"}
            assert {row["source_kind"] for row in assertion_rows} == {
                "approved_design_lesson"
            }
            assert {
                (row["event_type"], row["aggregate_type"], row["aggregate_id"])
                for row in projected_events
            } >= {
                ("design_lesson.approved", "design_lesson", lesson_id),
                (
                    "design_lesson_review.approved",
                    "design_lesson_review",
                    review_id,
                ),
            }

            for _ in range(20):
                projection = service.projection_sync()
                with service.projection._driver() as driver, driver.session() as session:
                    graph_row = session.run(
                        "MATCH (r:DesignLessonReview {review_id:$review_id})-[:PUBLISHED_AS]->"
                        "(l:DesignLesson {id:$lesson_id}) RETURN r.status AS review_status,"
                        "l.status AS lesson_status",
                        review_id=review_id,
                        lesson_id=lesson_id,
                    ).single()
                if graph_row and graph_row["review_status"] == "stored-and-retrievable":
                    break
                assert projection["failed"] == []
            assert graph_row is not None
            assert graph_row["review_status"] == "stored-and-retrievable"
            assert graph_row["lesson_status"] == "approved"

            live_mcp = create_mcp(service=service)

            def public_search(
                features: dict, *, requested_organization_id: str = organization_id
            ) -> dict:
                content, structured = asyncio.run(
                    live_mcp.call_tool(
                        "design_lesson_search",
                        {
                            "query": "actuator clearance",
                            "organization_id": requested_organization_id,
                            "design_features_json": json.dumps(
                                features, ensure_ascii=False
                            ),
                            "limit": 20,
                            "cursor": "",
                        },
                    )
                )
                assert len(content) == 1
                assert structured == {"result": content[0].text}
                return json.loads(content[0].text)

            with pytest.raises(ToolError, match="configured organization"):
                public_search(
                    {"moving-assembly": True},
                    requested_organization_id=f"{organization_id}-foreign",
                )

            boolean_match = public_search(
                {"moving-assembly": True, "catalog-fastener-present": True}
            )
            explicit_match = public_search(
                {
                    "satisfied_conditions": [
                        "moving-assembly",
                        "alignment-sensitive",
                        "service-access",
                    ]
                }
            )
            unmet = public_search(
                {"moving-assembly": False, "catalog-fastener-present": False}
            )
            non_applicable = public_search(
                {
                    "moving-assembly": True,
                    "catalog-fastener-present": True,
                    "sealed-unit": True,
                }
            )
            assert len(boolean_match["matches"]) == 1
            assert len(explicit_match["matches"]) == 1
            assert boolean_match["matches"][0]["match"]["eligible"] is True
            assert explicit_match["matches"][0]["match"]["eligible"] is True
            assert len(boolean_match["matches"][0]["lesson"]["assertions"]) == 3
            assert len(explicit_match["matches"][0]["lesson"]["assertions"]) == 3
            assert unmet["matches"] == []
            assert non_applicable["matches"] == []
            with service.repository.connection() as connection:
                sentinel_after = connection.execute(
                    "SELECT id::text,lesson_key,status,package_sha256 FROM design_lesson_events "
                    "WHERE id=%s",
                    (sentinel_lesson_id,),
                ).fetchone()
            assert dict(sentinel_after) == sentinel_before
        finally:
            if service is not None:
                try:
                    _remove_live_design_lesson_fixture(service, organization_id)
                finally:
                    with service.repository.connection() as connection, connection.transaction():
                        connection.execute(
                            "DELETE FROM actors WHERE organization_id=%s",
                            (foreign_organization_id,),
                        )
                        connection.execute(
                            "DELETE FROM design_groups WHERE organization_id=%s",
                            (foreign_organization_id,),
                        )
                        connection.execute(
                            "DELETE FROM organizations WHERE id=%s",
                            (foreign_organization_id,),
                        )
                        connection.execute(
                            "DELETE FROM design_lesson_events WHERE id=%s",
                            (sentinel_lesson_id,),
                        )
                        connection.execute(
                            "DELETE FROM actors WHERE organization_id=%s",
                            (sentinel_organization_id,),
                        )
                        connection.execute(
                            "DELETE FROM design_groups WHERE organization_id=%s",
                            (sentinel_organization_id,),
                        )
                        connection.execute(
                            "DELETE FROM organizations WHERE id=%s",
                            (sentinel_organization_id,),
                        )
                    service.executor.shutdown(wait=True)

    assert service is not None
    assert review_id is not None
    assert lesson_id is not None
    assert not workspace.exists()
    assert _live_fixture_database_state(service.repository, organization_id) == {
        "working_copy_ids": [],
        "lesson_ids": [],
        "assertion_ids": [],
        "review_ids": [],
        "family_ids": [],
        "model_revision_ids": [],
    }
    with service.repository.connection() as connection:
        fixture_outbox = connection.execute(
            "SELECT aggregate_id FROM outbox_events WHERE aggregate_id=ANY(%s)",
            ([review_id, lesson_id],),
        ).fetchall()
        sentinel_count = connection.execute(
            "SELECT count(*) AS count FROM organizations WHERE id=%s",
            (sentinel_organization_id,),
        ).fetchone()["count"]
    assert fixture_outbox == []
    assert int(sentinel_count) == 0
    with service.projection._driver() as driver, driver.session() as session:
        remaining_review_nodes = session.run(
            "MATCH (r:DesignLessonReview {review_id:$review_id}) "
            "RETURN count(r) AS count",
            review_id=review_id,
        ).single()["count"]
        remaining_lesson_nodes = session.run(
            "MATCH (l:DesignLesson {id:$lesson_id}) RETURN count(l) AS count",
            lesson_id=lesson_id,
        ).single()["count"]
        remaining_model_revision_nodes = session.run(
            "MATCH (m:ModelRevision {id:$model_revision_id}) RETURN count(m) AS count",
            model_revision_id=source_model_revision_id,
        ).single()["count"]
        remaining_publications = session.run(
            "MATCH (:DesignLessonReview {review_id:$review_id})"
            "-[p:PUBLISHED_AS]->(:DesignLesson {id:$lesson_id}) "
            "RETURN count(p) AS count",
            review_id=review_id,
            lesson_id=lesson_id,
        ).single()["count"]
    assert {
        "review_nodes": int(remaining_review_nodes),
        "lesson_nodes": int(remaining_lesson_nodes),
        "model_revision_nodes": int(remaining_model_revision_nodes),
        "published_as": int(remaining_publications),
    } == {
        "review_nodes": 0,
        "lesson_nodes": 0,
        "model_revision_nodes": 0,
        "published_as": 0,
    }
