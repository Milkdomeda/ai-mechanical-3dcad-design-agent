from __future__ import annotations

import tempfile
import unittest
import os
import json
import uuid
from io import BytesIO
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mechanical_design_agent.artifacts import ArtifactStore
from mechanical_design_agent.config import Settings
from mechanical_design_agent.design import DesignWorkspace, derive_iteration_candidates
from mechanical_design_agent.hashing import file_sha256
from mechanical_design_agent.jobs import JobFailure
from mechanical_design_agent.secure_fs import (
    SecureFilesystemError,
    read_managed_file,
    relative_managed_path,
)


class FakeRepository:
    def __init__(self) -> None:
        self.working_path = ""
        self.validation: dict | None = None
        self.approval: dict | None = None
        self.created: dict | None = None
        self.resolution_error: Exception | None = None

    def resolve_source_model_revision(self, **kwargs):
        if self.resolution_error:
            raise self.resolution_error
        return {
            "id": "model-revision-1",
            "family_id": "family-1",
            "artifact_sha256": kwargs["source_sha256"],
        }

    def create_working_copy(self, **kwargs):
        self.created = kwargs
        return {"id": "working-copy", **kwargs}

    def get_working_copy(self, working_copy_id):
        return {"id": working_copy_id, "working_path": self.working_path}

    def record_validation(
        self,
        working_copy_id,
        change_set_id,
        status,
        checks,
        working_sha256,
        report_path,
        validation_kind,
        report_sha256="",
    ):
        self.validation = {
            "working_copy_id": working_copy_id,
            "status": status,
            "checks": checks,
            "report_path": report_path,
            "validation_kind": validation_kind,
            "report_sha256": report_sha256,
        }
        return {"working_copy_id": working_copy_id, "status": status, "checks": checks}

    def approve_delivery(
        self,
        working_copy_id,
        actor_id,
        confirmation,
        current_sha256,
        approved_final_artifact_path,
        *,
        organization_id,
        design_group_id,
    ):
        self.approval = {
            "working_copy_id": working_copy_id,
            "actor_id": actor_id,
            "confirmation": confirmation,
            "approved_final_sha256": current_sha256,
            "approved_final_artifact_path": approved_final_artifact_path,
            "organization_id": organization_id,
            "design_group_id": design_group_id,
        }
        return dict(self.approval)


class FakeJobBindingRepository(FakeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.publication_error: Exception | None = None
        self.reconciliation_error: Exception | None = None
        self.committed_publication: dict | None = None
        self.reconciliations: list[dict] = []
        self.reconciliation_responses: list[object] = []
        self.job_active_working_copy_id: str | None = "copy"
        self.job_revision = 1

    def create_job_working_copy(self, **kwargs):
        self.created = kwargs
        snapshot = kwargs.get("source_snapshot")
        publication = {
            "working_copy": {
                "id": kwargs["working_copy_id"],
                "job_id": kwargs["job_id"],
                "working_path": kwargs["working_path"],
                "source_model_revision_id": kwargs["model_revision_id"],
                "working_sha256": kwargs["working_sha256"],
                "working_size_bytes": kwargs["working_size_bytes"],
                "working_relative_path": kwargs["working_relative_path"],
            },
            "source_snapshot": snapshot,
            "job": {
                "id": kwargs["job_id"],
                "revision": kwargs["expected_job_revision"] + 1,
            },
        }
        if self.publication_error is not None:
            if isinstance(self.publication_error, LostCommitAcknowledgement):
                self.committed_publication = publication
            raise self.publication_error
        self.committed_publication = publication
        return publication

    def get_working_copy(self, working_copy_id):
        if not self.working_path:
            return super().get_working_copy(working_copy_id)
        return {
            "id": working_copy_id,
            "job_id": "job-operational",
            "organization_id": "org-001",
            "design_group_id": "group-001",
            "family_id": None,
            "working_path": str(Path(self.working_path).resolve()),
            "working_relative_path": Path(self.working_path).name,
        }

    def get_design_job(self, **_scope):
        return {
            "id": "job-operational",
            "revision": self.job_revision,
            "status": "active",
            "phase": "delivery",
            "job_type": "mechanical_design",
            "active_working_copy_id": self.job_active_working_copy_id,
        }

    def reconcile_job_working_copy_publication(self, **kwargs):
        self.reconciliations.append(kwargs)
        if self.reconciliation_responses:
            response = self.reconciliation_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        if self.reconciliation_error is not None:
            raise self.reconciliation_error
        if self.committed_publication is None:
            return {"status": "not_committed"}
        return {
            "status": "committed",
            "publication": self.committed_publication,
        }


class LostCommitAcknowledgement(ConnectionError):
    pass


class FakeJobBindingManager:
    def __init__(self, root: Path, repository: FakeJobBindingRepository | None = None) -> None:
        self.root = root
        self.repository = repository
        self.calls: list[tuple[str, dict]] = []

    def reactivate_working_copy_for_delivery(self, **kwargs):
        self.calls.append(("reactivate", kwargs))
        if self.repository is not None:
            self.repository.job_active_working_copy_id = kwargs["working_copy_id"]
            self.repository.job_revision += 1
        return SimpleNamespace(
            revision=kwargs["expected_job_revision"] + 1,
            active_working_copy_id=kwargs["working_copy_id"],
        )

    @contextmanager
    def locked_active_mechanical_design_job(self, **kwargs):
        self.calls.append(("lock", kwargs))
        yield self.root, {
            "id": kwargs["job_id"],
            "revision": kwargs["expected_job_revision"],
            "status": "active",
            "job_type": "mechanical_design",
            "organization_id": kwargs["organization_id"],
            "design_group_id": kwargs["design_group_id"],
            "family_id": kwargs.get("family_id"),
            "active_working_copy_id": "copy",
        }

    def publish_authoritative_manifest_locked(self, **kwargs):
        self.calls.append(("publish", kwargs))
        return SimpleNamespace(
            as_dict=lambda: {
                "schema_version": "MechanicalDesignJob/v1",
                "job_id": kwargs["job_id"],
                "revision": kwargs["expected_job_revision"] + 1,
                "active_working_copy_id": kwargs["working_copy_id"],
            }
        )


def _job_binding_workspace(root: Path) -> tuple[Settings, FakeJobBindingRepository, FakeJobBindingManager]:
    package = root / "agent"
    package.mkdir()
    job_root = root / "jobs" / "JOB-20260823-001-unicode"
    (job_root / "inputs" / "source").mkdir(parents=True)
    (job_root / "models" / "working").mkdir(parents=True)
    repository = FakeJobBindingRepository()
    manager = FakeJobBindingManager(job_root)
    freecadcmd = package / "FreeCADCmd 1.1.3"
    freecadcmd.write_bytes(b"reviewed test executable boundary")
    freecad_boundary = read_managed_file(freecadcmd)
    settings = Settings(
        workspace=root,
        package_root=package,
        database_url="unused",
        neo4j_uri="unused",
        neo4j_user="unused",
        neo4j_password="unused",
        freecadcmd=freecadcmd,
        actor_id="owner",
        artifact_root=package / "data",
        family_config_path=package / "family.json",
        freecadcmd_sha256=freecad_boundary.sha256,
        freecadcmd_identity=freecad_boundary.identity,
        freecadcmd_version="1.1.3",
    )
    return settings, repository, manager


def _safe_fcstd_bytes(marker: str = "safe") -> bytes:
    output = BytesIO()
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document SchemaVersion="4" ProgramVersion="1.1.3">'
        f'<ObjectData><Object type="Part::Feature" name="{marker}"/></ObjectData>'
        "</Document>"
    ).encode("utf-8")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        document_entry = zipfile.ZipInfo(
            "Document.xml",
            date_time=(1980, 1, 1, 0, 0, 0),
        )
        document_entry.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(document_entry, document)
    return output.getvalue()


def _valid_job_freecad_run(_freecadcmd, script, arguments, timeout_seconds, **_trust):
    del timeout_seconds
    script_name = Path(script).name
    if script_name == "create_empty_working_copy.py":
        Path(arguments[0]).write_bytes(_safe_fcstd_bytes("new"))
    elif script_name == "normalize_working_copy.py":
        Path(arguments[1]).write_bytes(_safe_fcstd_bytes("normalized"))
    elif script_name == "validate_working_copy.py":
        working = Path(arguments[0])
        nonce = str(arguments[1])
        stdout = "MECHANICAL_DESIGN_FCSTD_VALIDATION_V1 " + json.dumps(
            {
                "schema_version": "MechanicalDesignWorkingCopyValidation/v2",
                "status": "valid",
                "nonce": nonce,
                "sha256": file_sha256(working),
                "size_bytes": working.stat().st_size,
                "document_name": "MechanicalDesignWorkingCopy",
                "object_count": 1,
                "recomputed": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    else:
        raise AssertionError(f"unexpected FreeCAD script: {script_name}")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class DesignWorkspaceTests(unittest.TestCase):
    def test_job_existing_model_creates_verified_snapshot_and_working_copy_without_source_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            source = root / "原始 model.FCStd"
            source_bytes = _safe_fcstd_bytes("immutable-source")
            source.write_bytes(source_bytes)

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=_valid_job_freecad_run,
            ):
                result = DesignWorkspace(settings, repository, manager).create_job_working_copy(
                    job_id="10000000-0000-4000-8000-000000000001",
                    expected_job_revision=4,
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )

            snapshot = manager.root / result["source_snapshot"]["stored_path"]
            working = Path(result["working_path"])
            self.assertEqual(snapshot.read_bytes(), source_bytes)
            self.assertEqual(working.read_bytes(), source_bytes)
            self.assertEqual(result["source_sha256"], file_sha256(source))
            self.assertNotIn("source_path", result)
            self.assertNotIn(str(source), str(result))
            self.assertEqual(repository.created["model_revision_id"], "model-revision-1")
            snapshot_receipt = json.loads(
                (snapshot.parent / ".binding-attempt.json").read_text(encoding="utf-8")
            )
            working_receipt = json.loads(
                (working.parent / ".binding-attempt.json").read_text(encoding="utf-8")
            )
            for receipt, expected_name, expected_file in (
                (snapshot_receipt, "source.FCStd", snapshot),
                (working_receipt, "working.FCStd", working),
            ):
                self.assertEqual(receipt["schema_version"], "MechanicalDesignJobBindingAttempt/v2")
                self.assertEqual(len(receipt["artifacts"]), 1)
                evidence = receipt["artifacts"][0]
                self.assertEqual(evidence["filename"], expected_name)
                self.assertEqual(evidence["sha256"], file_sha256(expected_file))
                self.assertEqual(evidence["size_bytes"], expected_file.stat().st_size)
                self.assertEqual(set(evidence["identity"]), {"volume", "file_index"})
            self.assertTrue(result["source_snapshot"]["stored_path"].startswith("inputs/source/"))
            self.assertEqual(
                relative_managed_path(
                    Path(result["working_path"]),
                    manager.root / "models" / "working",
                ).name,
                "working.FCStd",
            )
            self.assertEqual([name for name, _ in manager.calls], ["lock", "publish"])

    def test_job_existing_model_rejects_a_source_race_and_preserves_owned_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            source = root / "source.FCStd"
            source.write_bytes(_safe_fcstd_bytes("before"))
            from mechanical_design_agent import design as design_module

            original_read = design_module.read_managed_file
            first_source_read = True

            def mutate_after_first_source_read(path: Path):
                nonlocal first_source_read
                read = original_read(path)
                if Path(path) == source and first_source_read:
                    first_source_read = False
                    source.write_bytes(b"after")
                return read

            with patch(
                "mechanical_design_agent.design.read_managed_file",
                side_effect=mutate_after_first_source_read,
            ), patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=_valid_job_freecad_run,
            ):
                with self.assertRaises(JobFailure) as captured:
                    DesignWorkspace(settings, repository, manager).create_job_working_copy(
                        job_id="10000000-0000-4000-8000-000000000001",
                        expected_job_revision=4,
                        source_path=str(source),
                        organization_id="org",
                        design_group_id="group",
                        family_id=None,
                        model_revision_id=None,
                        actor_id="owner",
                    )

            self.assertEqual(captured.exception.code, "JOB_SOURCE_CHANGED")

            self.assertEqual(len(list((manager.root / "inputs" / "source").iterdir())), 1)
            self.assertEqual(len(list((manager.root / "models" / "working").iterdir())), 1)
            self.assertIsNone(repository.created)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires elevated test privileges")
    def test_job_existing_model_rejects_a_symlinked_external_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            real_source = root / "real.FCStd"
            real_source.write_bytes(_safe_fcstd_bytes("external-source"))
            source = root / "linked.FCStd"
            source.symlink_to(real_source)

            with self.assertRaises(JobFailure) as captured:
                DesignWorkspace(settings, repository, manager).create_job_working_copy(
                    job_id="10000000-0000-4000-8000-000000000001",
                    expected_job_revision=4,
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )
            self.assertEqual(captured.exception.code, "JOB_SOURCE_UNSAFE")

            self.assertEqual(list((manager.root / "inputs" / "source").iterdir()), [])
            self.assertEqual(list((manager.root / "models" / "working").iterdir()), [])
            self.assertIsNone(repository.created)

    def test_job_existing_model_never_opens_a_scripted_fcstd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            source = root / "scripted.FCStd"
            output = BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "Document.xml",
                    b'<Document SchemaVersion="4"><ObjectData>'
                    b'<Object type="App::FeaturePython" name="Unsafe"/>'
                    b"</ObjectData></Document>",
                )
            source.write_bytes(output.getvalue())

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=AssertionError("scripted FCStd must never reach FreeCAD"),
            ), self.assertRaises(JobFailure) as captured:
                DesignWorkspace(settings, repository, manager).create_job_working_copy(
                    job_id="10000000-0000-4000-8000-000000000001",
                    expected_job_revision=4,
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )

            self.assertEqual(captured.exception.code, "JOB_FCSTD_INVALID")
            self.assertIsNone(repository.created)

    def test_job_existing_model_retry_preserves_an_attempt_with_unknown_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            source = root / "source.FCStd"
            source.write_bytes(_safe_fcstd_bytes("retry-source"))
            job_id = "10000000-0000-4000-8000-000000000001"
            namespace = uuid.UUID(job_id)
            digest = file_sha256(source)
            snapshot_id = str(
                uuid.uuid5(namespace, f"source-snapshot:4:{digest}")
            )
            working_id = str(
                uuid.uuid5(namespace, f"working-copy:4:existing_model:{digest}")
            )
            attempts = (
                (
                    manager.root / "inputs" / "source" / snapshot_id,
                    "source_snapshot",
                    snapshot_id,
                ),
                (
                    manager.root / "models" / "working" / working_id,
                    "working_copy",
                    working_id,
                ),
            )
            for attempt, kind, artifact_id in attempts:
                attempt.mkdir()
                receipt = {
                    "schema_version": "MechanicalDesignJobBindingAttempt/v1",
                    "job_id": job_id,
                    "expected_job_revision": 4,
                    "artifact_kind": kind,
                    "artifact_id": artifact_id,
                    "source_sha256": digest,
                }
                (attempt / ".binding-attempt.json").write_text(
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                (attempt / "orphaned-crash-bytes.bin").write_bytes(b"owned incomplete")

            with self.assertRaises(JobFailure) as captured:
                DesignWorkspace(settings, repository, manager).create_job_working_copy(
                    job_id=job_id,
                    expected_job_revision=4,
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )

            self.assertEqual(captured.exception.code, "JOB_ATTEMPT_RECOVERY_REQUIRED")
            for attempt, _kind, _artifact_id in attempts:
                self.assertEqual(
                    (attempt / "orphaned-crash-bytes.bin").read_bytes(),
                    b"owned incomplete",
                )

    def test_job_retry_reconciles_before_touching_partial_mismatch_or_unavailable_attempts(self) -> None:
        for authority in (
            {"status": "unknown", "reason": "partial"},
            {"status": "unknown", "reason": "mismatch"},
            ConnectionError("authority unavailable"),
        ):
            with self.subTest(authority=authority), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings, repository, manager = _job_binding_workspace(root)
                repository.reconciliation_responses = [authority]
                source = root / "source.FCStd"
                source.write_bytes(_safe_fcstd_bytes("retry-unknown"))
                job_id = "10000000-0000-4000-8000-000000000001"
                digest = file_sha256(source)
                working_id = str(
                    uuid.uuid5(
                        uuid.UUID(job_id),
                        f"working-copy:4:existing_model:{digest}",
                    )
                )
                attempt = manager.root / "models" / "working" / working_id
                attempt.mkdir()
                preserved = attempt / "preserved.bin"
                preserved.write_bytes(b"never delete commit-unknown bytes")

                with patch(
                    "mechanical_design_agent.design.run_freecad_script",
                    side_effect=AssertionError("FreeCAD must not run before reconciliation"),
                ), self.assertRaises(JobFailure) as captured:
                    DesignWorkspace(settings, repository, manager).create_job_working_copy(
                        job_id=job_id,
                        expected_job_revision=4,
                        source_path=str(source),
                        organization_id="org",
                        design_group_id="group",
                        family_id=None,
                        model_revision_id=None,
                        actor_id="owner",
                    )

                self.assertEqual(captured.exception.code, "JOB_DATABASE_COMMIT_UNKNOWN")
                self.assertEqual(preserved.read_bytes(), b"never delete commit-unknown bytes")
                self.assertIsNone(repository.created)
                self.assertEqual(len(repository.reconciliations), 1)

    def test_job_retry_recovers_exact_committed_binding_without_running_freecad(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            source = root / "source.FCStd"
            source_bytes = _safe_fcstd_bytes("committed-retry")
            source.write_bytes(source_bytes)
            job_id = "10000000-0000-4000-8000-000000000001"
            revision = 4
            digest = file_sha256(source)
            namespace = uuid.UUID(job_id)
            snapshot_id = str(uuid.uuid5(namespace, f"source-snapshot:{revision}:{digest}"))
            working_id = str(
                uuid.uuid5(namespace, f"working-copy:{revision}:existing_model:{digest}")
            )
            snapshot_attempt = manager.root / "inputs" / "source" / snapshot_id
            working_attempt = manager.root / "models" / "working" / working_id
            snapshot_attempt.mkdir()
            working_attempt.mkdir()
            snapshot_path = snapshot_attempt / "source.FCStd"
            working_path = working_attempt / "working.FCStd"
            snapshot_path.write_bytes(source_bytes)
            working_path.write_bytes(source_bytes)
            publication = {
                "working_copy": {
                    "id": working_id,
                    "job_id": job_id,
                    "working_path": str(working_path),
                    "source_model_revision_id": "model-revision-1",
                    "working_sha256": digest,
                    "working_size_bytes": len(source_bytes),
                    "working_relative_path": f"models/working/{working_id}/working.FCStd",
                },
                "source_snapshot": {
                    "id": snapshot_id,
                    "job_id": job_id,
                    "stored_path": f"inputs/source/{snapshot_id}/source.FCStd",
                    "sha256": digest,
                    "source_model_revision_id": "model-revision-1",
                },
                "job": {"id": job_id, "revision": revision + 1},
            }
            repository.reconciliation_responses = [
                {"status": "committed", "publication": publication}
            ]

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=AssertionError("committed retry must not rerun FreeCAD"),
            ):
                result = DesignWorkspace(settings, repository, manager).create_job_working_copy(
                    job_id=job_id,
                    expected_job_revision=revision,
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )

            self.assertEqual(result["id"], working_id)
            self.assertEqual(snapshot_path.read_bytes(), source_bytes)
            self.assertEqual(working_path.read_bytes(), source_bytes)
            self.assertIsNone(repository.created)
            self.assertEqual([name for name, _ in manager.calls], ["lock", "publish"])

    def test_step_committed_retry_uses_authoritative_normalized_working_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            source = root / "source.step"
            source.write_bytes(b"ISO-10303-21; normalized output must differ")
            job_id = "10000000-0000-4000-8000-000000000001"
            revision = 4
            source_digest = file_sha256(source)
            namespace = uuid.UUID(job_id)
            snapshot_id = str(
                uuid.uuid5(namespace, f"source-snapshot:{revision}:{source_digest}")
            )
            working_id = str(
                uuid.uuid5(
                    namespace,
                    f"working-copy:{revision}:existing_model:{source_digest}",
                )
            )
            snapshot_path = manager.root / "inputs/source" / snapshot_id / "source.step"
            working_path = manager.root / "models/working" / working_id / "working.FCStd"
            snapshot_path.parent.mkdir()
            working_path.parent.mkdir()
            snapshot_path.write_bytes(source.read_bytes())
            normalized = _safe_fcstd_bytes("normalized-step")
            working_path.write_bytes(normalized)
            working_digest = file_sha256(working_path)
            repository.reconciliation_responses = [
                {
                    "status": "committed",
                    "publication": {
                        "working_copy": {
                            "id": working_id,
                            "job_id": job_id,
                            "working_path": str(working_path),
                            "source_model_revision_id": "model-revision-1",
                            "source_sha256": source_digest,
                            "working_sha256": working_digest,
                            "working_size_bytes": len(normalized),
                            "working_relative_path": f"models/working/{working_id}/working.FCStd",
                        },
                        "source_snapshot": {"id": snapshot_id},
                        "job": {"id": job_id, "revision": revision + 1},
                    },
                }
            ]

            result = DesignWorkspace(settings, repository, manager).create_job_working_copy(
                job_id=job_id,
                expected_job_revision=revision,
                source_path=str(source),
                organization_id="org",
                design_group_id="group",
                family_id=None,
                model_revision_id=None,
                actor_id="owner",
            )

            self.assertEqual(result["id"], working_id)
            self.assertNotEqual(source_digest, working_digest)

    def test_job_retry_proven_absent_preserves_a_prior_attempt_for_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            repository.reconciliation_responses = [{"status": "not_committed"}]
            job_id = "10000000-0000-4000-8000-000000000001"
            revision = 4
            source = root / "source.FCStd"
            source.write_bytes(_safe_fcstd_bytes("absent-retry"))
            digest = file_sha256(source)
            working_id = str(
                uuid.uuid5(
                    uuid.UUID(job_id),
                    f"working-copy:{revision}:existing_model:{digest}",
                )
            )
            attempt = manager.root / "models" / "working" / working_id
            attempt.mkdir()
            marker = attempt / "working.FCStd"
            marker.write_bytes(_safe_fcstd_bytes("preserved-attempt"))

            with self.assertRaises(JobFailure) as captured:
                DesignWorkspace(settings, repository, manager).create_job_working_copy(
                    job_id=job_id,
                    expected_job_revision=revision,
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )

            self.assertEqual(captured.exception.code, "JOB_ATTEMPT_RECOVERY_REQUIRED")
            self.assertEqual(marker.read_bytes(), _safe_fcstd_bytes("preserved-attempt"))
            self.assertIsNone(repository.created)

    def test_job_new_design_is_created_directly_under_models_working(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=_valid_job_freecad_run,
            ):
                result = DesignWorkspace(settings, repository, manager).create_job_new_working_copy(
                    job_id="10000000-0000-4000-8000-000000000001",
                    expected_job_revision=4,
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    actor_id="owner",
                )

            self.assertEqual(
                Path(result["working_path"]).read_bytes(), _safe_fcstd_bytes("new")
            )
            self.assertEqual(
                relative_managed_path(
                    Path(result["working_path"]),
                    manager.root / "models" / "working",
                ).name,
                "working.FCStd",
            )
            self.assertIsNone(result["source_snapshot"])
            self.assertIsNone(repository.created["model_revision_id"])
            self.assertEqual(repository.created["design_origin"], "new_design")

    def test_job_existing_model_reconciles_a_lost_commit_ack_without_deleting_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            repository.publication_error = LostCommitAcknowledgement("lost COMMIT ack")
            source = root / "source.FCStd"
            source.write_bytes(_safe_fcstd_bytes("committed-existing"))

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=_valid_job_freecad_run,
            ):
                result = DesignWorkspace(settings, repository, manager).create_job_working_copy(
                    job_id="10000000-0000-4000-8000-000000000001",
                    expected_job_revision=4,
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )

            self.assertEqual(len(repository.reconciliations), 2)
            self.assertEqual(Path(result["working_path"]).read_bytes(), source.read_bytes())
            snapshot = manager.root / result["source_snapshot"]["stored_path"]
            self.assertEqual(snapshot.read_bytes(), source.read_bytes())
            self.assertEqual([name for name, _ in manager.calls], ["lock", "publish"])

    def test_job_new_design_reconciles_a_lost_commit_ack_without_duplicate_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            repository.publication_error = LostCommitAcknowledgement("lost COMMIT ack")
            generated_artifacts: list[bytes] = []

            def capture_generated_artifact(*args, **kwargs):
                result = _valid_job_freecad_run(*args, **kwargs)
                script = Path(args[1])
                if script.name == "create_empty_working_copy.py":
                    generated_artifacts.append(Path(args[2][0]).read_bytes())
                return result

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=capture_generated_artifact,
            ):
                result = DesignWorkspace(settings, repository, manager).create_job_new_working_copy(
                    job_id="10000000-0000-4000-8000-000000000001",
                    expected_job_revision=4,
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    actor_id="owner",
                )

            self.assertEqual(len(repository.reconciliations), 2)
            self.assertEqual(len(generated_artifacts), 1)
            self.assertEqual(
                Path(result["working_path"]).read_bytes(), generated_artifacts[0]
            )
            self.assertEqual(
                repository.created["working_sha256"],
                file_sha256(Path(result["working_path"])),
            )
            self.assertEqual([name for name, _ in manager.calls], ["lock", "publish"])

    def test_job_new_design_retry_recovers_committed_binding_before_attempt_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            job_id = "10000000-0000-4000-8000-000000000001"
            revision = 4
            working_id = str(
                uuid.uuid5(
                    uuid.UUID(job_id),
                    f"working-copy:{revision}:new_design",
                )
            )
            working_attempt = manager.root / "models" / "working" / working_id
            working_attempt.mkdir()
            working_path = working_attempt / "working.FCStd"
            working_path.write_bytes(_safe_fcstd_bytes("committed-new-retry"))
            digest = file_sha256(working_path)
            repository.reconciliation_responses = [
                {
                    "status": "committed",
                    "publication": {
                        "working_copy": {
                            "id": working_id,
                            "job_id": job_id,
                            "working_path": str(working_path),
                            "source_model_revision_id": None,
                            "source_sha256": digest,
                            "working_sha256": digest,
                            "working_size_bytes": working_path.stat().st_size,
                            "working_relative_path": f"models/working/{working_id}/working.FCStd",
                        },
                        "source_snapshot": None,
                        "job": {"id": job_id, "revision": revision + 1},
                    },
                }
            ]

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=AssertionError("committed retry must not run FreeCAD"),
            ):
                result = DesignWorkspace(settings, repository, manager).create_job_new_working_copy(
                    job_id=job_id,
                    expected_job_revision=revision,
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    actor_id="owner",
                )

            self.assertEqual(result["id"], working_id)
            self.assertEqual(result["source_sha256"], digest)
            self.assertEqual(working_path.read_bytes(), _safe_fcstd_bytes("committed-new-retry"))
            self.assertIsNone(repository.created)

    def test_job_publication_unknown_preserves_owned_bytes_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings, repository, manager = _job_binding_workspace(root)
            repository.publication_error = ConnectionError("publication failed")
            repository.reconciliation_responses = [
                {"status": "not_committed"},
                ConnectionError("authority unavailable"),
            ]

            with patch(
                "mechanical_design_agent.design.run_freecad_script",
                side_effect=_valid_job_freecad_run,
            ), self.assertRaises(JobFailure) as captured:
                DesignWorkspace(settings, repository, manager).create_job_new_working_copy(
                    job_id="10000000-0000-4000-8000-000000000001",
                    expected_job_revision=4,
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    actor_id="owner",
                )

            self.assertEqual(captured.exception.code, "JOB_DATABASE_COMMIT_UNKNOWN")
            attempts = list((manager.root / "models" / "working").iterdir())
            self.assertEqual(len(attempts), 1)
            self.assertEqual(
                (attempts[0] / "working.FCStd").read_bytes(), _safe_fcstd_bytes("new")
            )

    def test_job_new_design_rejects_corrupt_zero_hardlinked_and_unexpected_output(self) -> None:
        cases = ("corrupt", "zero", "hardlink", "unexpected", "sibling")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings, repository, manager = _job_binding_workspace(root)
                outside = root / "outside.FCStd"
                outside.write_bytes(b"outside-owned-by-someone-else")

                def invalid_run(_freecadcmd, script, arguments, timeout_seconds, **_trust):
                    del timeout_seconds
                    if Path(script).name == "create_empty_working_copy.py":
                        working = Path(arguments[0])
                        if case == "zero":
                            working.write_bytes(b"")
                        elif case == "hardlink":
                            os.link(outside, working)
                        else:
                            working.write_bytes(b"not-an-fcstd")
                        if case == "unexpected":
                            (working.parent / "unexpected-output.bin").write_bytes(
                                b"must be preserved"
                            )
                        if case == "sibling":
                            (working.parent.parent / "unexpected-sibling.bin").write_bytes(
                                b"must be preserved"
                            )
                        return SimpleNamespace(returncode=0, stdout="", stderr="")
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="FreeCAD rejected corrupt FCStd",
                    )

                with patch(
                    "mechanical_design_agent.design.run_freecad_script",
                    side_effect=invalid_run,
                ), self.assertRaises(JobFailure) as captured:
                    DesignWorkspace(settings, repository, manager).create_job_new_working_copy(
                        job_id="10000000-0000-4000-8000-000000000001",
                        expected_job_revision=4,
                        organization_id="org",
                        design_group_id="group",
                        family_id=None,
                        actor_id="owner",
                    )

                self.assertIn(
                    captured.exception.code,
                    {"JOB_FCSTD_INVALID", "JOB_OUTPUT_UNEXPECTED"},
                )
                self.assertIsNone(repository.created)
                if case == "unexpected":
                    attempts = list((manager.root / "models" / "working").iterdir())
                    self.assertEqual(len(attempts), 1)
                    self.assertEqual(
                        (attempts[0] / "unexpected-output.bin").read_bytes(),
                        b"must be preserved",
                    )
                if case == "hardlink":
                    self.assertEqual(outside.read_bytes(), b"outside-owned-by-someone-else")
                if case == "sibling":
                    self.assertEqual(
                        (manager.root / "models/working/unexpected-sibling.bin").read_bytes(),
                        b"must be preserved",
                    )

    def test_job_fcstd_validation_rejects_forged_stdout_replacement_and_files(self) -> None:
        for case in ("extra_stdout", "wrong_nonce", "replacement", "forged_file"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings, repository, manager = _job_binding_workspace(root)

                def hostile_run(_freecadcmd, script, arguments, timeout_seconds, **_trust):
                    del timeout_seconds
                    if Path(script).name == "create_empty_working_copy.py":
                        Path(arguments[0]).write_bytes(_safe_fcstd_bytes("before"))
                        return SimpleNamespace(returncode=0, stdout="", stderr="")
                    working = Path(arguments[0])
                    nonce = str(arguments[1])
                    before_sha = file_sha256(working)
                    before_size = working.stat().st_size
                    payload = {
                        "schema_version": "MechanicalDesignWorkingCopyValidation/v2",
                        "status": "valid",
                        "nonce": "attacker" if case == "wrong_nonce" else nonce,
                        "sha256": before_sha,
                        "size_bytes": before_size,
                        "document_name": "MechanicalDesignWorkingCopy",
                        "object_count": 0,
                        "recomputed": True,
                    }
                    if case == "replacement":
                        working.write_bytes(_safe_fcstd_bytes("replacement"))
                    if case == "forged_file":
                        (working.parent / ".fcstd-validation.json").write_text(
                            json.dumps(payload), encoding="utf-8"
                        )
                    stdout = "MECHANICAL_DESIGN_FCSTD_VALIDATION_V1 " + json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ) + "\n"
                    if case == "extra_stdout":
                        stdout = "attacker chatter\n" + stdout
                    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

                with patch(
                    "mechanical_design_agent.design.run_freecad_script",
                    side_effect=hostile_run,
                ), self.assertRaises(JobFailure) as captured:
                    DesignWorkspace(settings, repository, manager).create_job_new_working_copy(
                        job_id="10000000-0000-4000-8000-000000000001",
                        expected_job_revision=4,
                        organization_id="org",
                        design_group_id="group",
                        family_id=None,
                        actor_id="owner",
                    )

                self.assertIn(
                    captured.exception.code,
                    {"JOB_FCSTD_INVALID", "JOB_OUTPUT_UNEXPECTED"},
                )
                self.assertIsNone(repository.created)
    def test_artifact_store_reports_reparse_target_as_an_invalid_stable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            store = ArtifactStore(workspace / "artifacts")
            digest = "a" * 64
            target = store.path_for(digest, ".FCStd")

            with patch(
                "mechanical_design_agent.artifacts.verify_cas_file",
                side_effect=SecureFilesystemError(
                    "WINDOWS_REPARSE_POINT_BLOCKED",
                    "managed paths must not contain a reparse point",
                ),
            ):
                with self.assertRaisesRegex(
                    SecureFilesystemError, "stable regular file"
                ) as captured:
                    store.verify_file(target, digest)

            self.assertEqual(captured.exception.code, "ARTIFACT_TARGET_INVALID")

    def test_artifact_store_publishes_read_only_snapshots_and_rejects_writable_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source.FCStd"
            source.write_bytes(b"immutable model")
            store = ArtifactStore(workspace / "artifacts")

            snapshot = store.ingest_file(source, allowed_root=workspace)
            snapshot_path = Path(str(snapshot["storage_path"]))

            self.assertEqual(snapshot_path.stat().st_mode & 0o222, 0)
            os.chmod(snapshot_path, 0o644)
            with self.assertRaisesRegex(ValueError, "writable"):
                store.verify_file(snapshot_path, str(snapshot["sha256"]))

    def test_artifact_store_read_only_publish_preserves_every_source_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "large.FCStd"
            payload = (b"a" * (1024 * 1024)) + b"final-chunk"
            source.write_bytes(payload)
            store = ArtifactStore(workspace / "artifacts")

            snapshot = store.ingest_file(source, allowed_root=workspace)

            self.assertEqual(Path(str(snapshot["storage_path"])).read_bytes(), payload)
            self.assertEqual(snapshot["size_bytes"], len(payload))

    def test_artifact_store_atomically_replaces_legacy_writable_target_from_fresh_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source.FCStd"
            source.write_bytes(b"trusted current model")
            store = ArtifactStore(workspace / "artifacts")
            digest = file_sha256(source)
            legacy_target = store.path_for(digest, source.suffix)
            legacy_target.parent.mkdir(parents=True)
            legacy_target.write_bytes(b"corrupted legacy bytes")
            os.chmod(legacy_target, 0o600)

            snapshot = store.ingest_file(source, allowed_root=workspace)

            self.assertEqual(Path(str(snapshot["storage_path"])).read_bytes(), source.read_bytes())
            self.assertEqual(legacy_target.stat().st_mode & 0o222, 0)
            self.assertEqual(store.verify_file(legacy_target, digest)["sha256"], digest)
            reapproval = store.ingest_file(source, allowed_root=workspace)
            self.assertEqual(reapproval["storage_path"], snapshot["storage_path"])
            self.assertEqual(reapproval["sha256"], digest)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor-relative CAS regression")
    def test_artifact_store_parent_swap_repairs_only_the_pinned_cas_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source.FCStd"
            source.write_bytes(b"trusted pinned publication")
            store = ArtifactStore(workspace / "artifacts")
            digest = file_sha256(source)
            target = store.path_for(digest, source.suffix)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"legacy writable bytes")
            os.chmod(target, 0o600)
            external_parent = workspace / "external-cas-parent"
            external_parent.mkdir()
            external_victim = external_parent / target.name
            external_victim.write_bytes(b"external victim")
            pinned_parent = workspace / "pinned-cas-parent"
            original_replace = os.replace
            swapped = False

            def swap_parent_then_replace(source_name, target_name, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    target.parent.rename(pinned_parent)
                    target.parent.symlink_to(external_parent, target_is_directory=True)
                return original_replace(source_name, target_name, *args, **kwargs)

            with patch(
                "mechanical_design_agent.secure_fs_posix.os.replace",
                side_effect=swap_parent_then_replace,
            ):
                with self.assertRaisesRegex(ValueError, "path changed during publication"):
                    store.ingest_file(source, allowed_root=workspace)

            pinned_target = pinned_parent / target.name
            self.assertEqual(external_victim.read_bytes(), b"external victim")
            self.assertEqual(pinned_target.read_bytes(), source.read_bytes())
            self.assertEqual(pinned_target.stat().st_mode & 0o222, 0)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor-relative CAS regression")
    def test_artifact_store_root_swap_never_returns_external_same_name_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source.FCStd"
            source_bytes = b"trusted root-pinned publication"
            source.write_bytes(source_bytes)
            store = ArtifactStore(workspace / "artifacts")
            digest = file_sha256(source)
            target = store.path_for(digest, source.suffix)
            external_root = workspace / "external-artifacts"
            external_target = (
                external_root / digest[:2] / digest[2:4] / target.name
            )
            external_target.parent.mkdir(parents=True)
            external_bytes = b"external same-name bytes"
            external_target.write_bytes(external_bytes)
            pinned_root = workspace / "pinned-artifacts"
            from mechanical_design_agent import secure_fs_posix

            original_open_or_create = secure_fs_posix.open_or_create_directory_at
            swapped = False

            def swap_root_then_open(parent_fd, name):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    store.root.rename(pinned_root)
                    store.root.symlink_to(external_root, target_is_directory=True)
                return original_open_or_create(parent_fd, name)

            snapshot = None
            with patch(
                "mechanical_design_agent.secure_fs_posix.open_or_create_directory_at",
                side_effect=swap_root_then_open,
            ):
                try:
                    snapshot = store.ingest_file(source, allowed_root=workspace)
                except ValueError:
                    pass

            self.assertEqual(external_target.read_bytes(), external_bytes)
            pinned_target = pinned_root / digest[:2] / digest[2:4] / target.name
            self.assertEqual(pinned_target.read_bytes(), source_bytes)
            if snapshot is not None:
                reported_path = Path(str(snapshot["storage_path"]))
                self.assertEqual(reported_path.read_bytes(), source_bytes)
                self.assertEqual(file_sha256(reported_path), digest)
                self.assertEqual(snapshot["sha256"], digest)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor-relative CAS regression")
    def test_artifact_store_ancestor_swap_does_not_redirect_legacy_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.FCStd"
            source.write_bytes(b"trusted legacy replacement")
            trusted_ancestor = base / "trusted-ancestor"
            workspace = trusted_ancestor / "workspace"
            workspace.mkdir(parents=True)
            store = ArtifactStore(workspace / "artifacts")
            digest = file_sha256(source)
            trusted_target = store.path_for(digest, source.suffix)
            trusted_target.parent.mkdir(parents=True)
            trusted_target.write_bytes(b"trusted legacy bytes")
            os.chmod(trusted_target, 0o600)

            external_ancestor = base / "external-ancestor"
            external_target = (
                external_ancestor
                / "workspace"
                / "artifacts"
                / digest[:2]
                / digest[2:4]
                / trusted_target.name
            )
            external_target.parent.mkdir(parents=True)
            external_bytes = b"external same-name victim"
            external_target.write_bytes(external_bytes)
            os.chmod(external_target, 0o600)

            pinned_ancestor = base / "pinned-trusted-ancestor"
            trusted_ancestor.rename(pinned_ancestor)
            trusted_ancestor.symlink_to(external_ancestor, target_is_directory=True)

            failed_safely = False
            try:
                store.ingest_file(source)
            except ValueError:
                failed_safely = True

            self.assertEqual(external_target.read_bytes(), external_bytes)
            pinned_target = (
                pinned_ancestor
                / "workspace"
                / "artifacts"
                / digest[:2]
                / digest[2:4]
                / trusted_target.name
            )
            if failed_safely:
                self.assertEqual(pinned_target.read_bytes(), b"trusted legacy bytes")
            else:
                self.assertEqual(pinned_target.read_bytes(), source.read_bytes())
                self.assertEqual(pinned_target.stat().st_mode & 0o222, 0)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor-relative CAS regression")
    def test_artifact_store_ancestor_swap_does_not_redirect_fresh_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.FCStd"
            source.write_bytes(b"trusted fresh publication")
            trusted_ancestor = base / "trusted-ancestor"
            workspace = trusted_ancestor / "workspace"
            workspace.mkdir(parents=True)
            store = ArtifactStore(workspace / "artifacts")
            digest = file_sha256(source)
            trusted_target = store.path_for(digest, source.suffix)

            external_ancestor = base / "external-ancestor"
            external_artifacts = external_ancestor / "workspace" / "artifacts"
            external_artifacts.mkdir(parents=True)
            external_victim = external_artifacts / "victim.bin"
            external_bytes = b"external tree must remain byte-for-byte intact"
            external_victim.write_bytes(external_bytes)
            external_target = (
                external_artifacts
                / digest[:2]
                / digest[2:4]
                / trusted_target.name
            )

            pinned_ancestor = base / "pinned-trusted-ancestor"
            trusted_ancestor.rename(pinned_ancestor)
            trusted_ancestor.symlink_to(external_ancestor, target_is_directory=True)

            failed_safely = False
            try:
                store.ingest_file(source)
            except ValueError:
                failed_safely = True

            self.assertEqual(external_victim.read_bytes(), external_bytes)
            self.assertFalse(external_target.exists())
            pinned_target = (
                pinned_ancestor
                / "workspace"
                / "artifacts"
                / digest[:2]
                / digest[2:4]
                / trusted_target.name
            )
            if failed_safely:
                self.assertFalse(pinned_target.exists())
            else:
                self.assertEqual(pinned_target.read_bytes(), source.read_bytes())
                self.assertEqual(pinned_target.stat().st_mode & 0o222, 0)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor lifetime regression")
    def test_artifact_store_forced_temporary_allocation_failure_closes_descriptors(self) -> None:
        descriptor_directory = next(
            (path for path in (Path("/dev/fd"), Path("/proc/self/fd")) if path.is_dir()),
            None,
        )
        if descriptor_directory is None:
            self.skipTest("platform does not expose process descriptors")

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source.FCStd"
            source.write_bytes(b"descriptor lifetime")
            store = ArtifactStore(workspace / "artifacts")
            before = len(os.listdir(descriptor_directory))

            with patch(
                "mechanical_design_agent.secure_fs_posix._create_temporary_at",
                side_effect=RuntimeError("forced temporary allocation failure"),
            ):
                for _ in range(20):
                    with self.assertRaisesRegex(
                        RuntimeError, "forced temporary allocation failure"
                    ):
                        store.ingest_file(source, allowed_root=workspace)

            self.assertEqual(len(os.listdir(descriptor_directory)), before)

    def test_delivery_approval_captures_and_persists_an_immutable_fcstd_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            working = workspace / "working.FCStd"
            working.write_bytes(b"approved model bytes")
            repository = FakeJobBindingRepository()
            repository.working_path = str(working)
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=package / "family.json",
            )

            approved = DesignWorkspace(
                settings,
                repository,
                FakeJobBindingManager(workspace),
            ).approve_delivery(
                "copy",
                "owner",
                "批准 copy",
                ArtifactStore(settings.artifact_root),
                organization_id="org-001",
                design_group_id="group-001",
            )

            snapshot_path = Path(approved["approved_final_artifact_path"])
            self.assertEqual(snapshot_path.read_bytes(), b"approved model bytes")
            self.assertTrue(
                relative_managed_path(snapshot_path, workspace).parts
            )
            self.assertEqual(
                relative_managed_path(snapshot_path, workspace).parts[:2],
                ("delivery", "copy"),
            )
            self.assertEqual(approved["approved_final_sha256"], file_sha256(snapshot_path))

    def test_delivery_approval_reactivates_the_unique_confirmed_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            working = workspace / "working.FCStd"
            working.write_bytes(b"reopened confirmed model")
            repository = FakeJobBindingRepository()
            repository.working_path = str(working)
            repository.job_active_working_copy_id = None
            manager = FakeJobBindingManager(workspace, repository)
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=package / "family.json",
            )

            approved = DesignWorkspace(settings, repository, manager).approve_delivery(
                "copy",
                "owner",
                "批准 copy",
                ArtifactStore(settings.artifact_root),
                organization_id="org-001",
                design_group_id="group-001",
            )

            self.assertEqual(repository.job_active_working_copy_id, "copy")
            self.assertEqual([name for name, _ in manager.calls].count("reactivate"), 1)
            self.assertEqual(approved["approved_final_sha256"], file_sha256(working))

    def test_invalid_delivery_confirmation_never_reactivates_a_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            working = workspace / "working.FCStd"
            working.write_bytes(b"must remain inactive")
            repository = FakeJobBindingRepository()
            repository.working_path = str(working)
            repository.job_active_working_copy_id = None
            manager = FakeJobBindingManager(workspace, repository)
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=package / "family.json",
            )

            with self.assertRaisesRegex(ValueError, "working_copy_id and 批准"):
                DesignWorkspace(settings, repository, manager).approve_delivery(
                    "copy",
                    "owner",
                    "do not approve copy",
                    ArtifactStore(settings.artifact_root),
                    organization_id="org-001",
                    design_group_id="group-001",
                )

            self.assertIsNone(repository.job_active_working_copy_id)
            self.assertNotIn("reactivate", [name for name, _ in manager.calls])

    def test_iteration_candidates_group_repeated_targets_and_failed_check_ids(self) -> None:
        changes = [
            {
                "id": "change-002",
                "changes": [
                    {"target": "nozzle.N1", "operation": "resize"},
                    {"target": "support.S1", "operation": "move"},
                ],
            },
            {
                "id": "change-001",
                "changes": [{"target": "nozzle.N1", "operation": "move"}],
            },
        ]
        validations = [
            {
                "id": "validation-002",
                "checks": [
                    {"check_id": "clearance.N1", "status": "failed"},
                    {"check_id": "shape.valid", "status": "passed"},
                    {"check_id": "legacy.no-status"},
                ],
            },
            {
                "id": "validation-001",
                "checks": [{"check_id": "clearance.N1", "status": "failed"}],
            },
        ]

        candidates = derive_iteration_candidates(changes, validations)

        self.assertEqual(
            candidates,
            [
                {
                    "candidate_kind": "repeated_change_target",
                    "target": "nozzle.N1",
                    "occurrences": 2,
                    "change_set_ids": ["change-001", "change-002"],
                },
                {
                    "candidate_kind": "failed_validation_check",
                    "check_id": "clearance.N1",
                    "occurrences": 2,
                    "validation_report_ids": ["validation-001", "validation-002"],
                },
            ],
        )

    def test_fcstd_source_is_preserved_and_copy_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            source = workspace / "source.FCStd"
            source.write_bytes(b"fixture-fcstd")
            config = package / "family.json"
            config.write_text("{}", encoding="utf-8")
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=config,
            )
            before = file_sha256(source)
            repository = FakeJobBindingRepository()
            result = DesignWorkspace(settings, repository).create_working_copy(
                source_path=str(source),
                organization_id="org",
                design_group_id="group",
                family_id=None,
                model_revision_id=None,
                actor_id="owner",
            )
            self.assertEqual(file_sha256(source), before)
            self.assertNotEqual(Path(result["working_path"]), source)
            self.assertEqual(Path(result["working_path"]).read_bytes(), source.read_bytes())
            self.assertEqual(repository.created["model_revision_id"], "model-revision-1")
            self.assertEqual(repository.created["design_origin"], "existing_model")

    def test_existing_model_identity_failure_happens_before_working_copy_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            source = workspace / "source.FCStd"
            source.write_bytes(b"ambiguous-fcstd")
            repository = FakeJobBindingRepository()
            repository.resolution_error = ValueError("found 2")
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=package / "family.json",
            )

            with self.assertRaisesRegex(ValueError, "found 2"):
                DesignWorkspace(settings, repository).create_working_copy(
                    source_path=str(source),
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    model_revision_id=None,
                    actor_id="owner",
                )

            self.assertIsNone(repository.created)
            self.assertFalse((workspace / "output" / "mechanical_design" / "working_copies").exists())

    def test_new_design_allows_null_source_model_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            repository = FakeRepository()
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=package / "family.json",
            )

            def create_empty(
                _freecadcmd, _script, arguments, timeout_seconds, **_trust
            ):
                Path(arguments[0]).write_bytes(b"new-design")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("mechanical_design_agent.design.run_freecad_script", side_effect=create_empty):
                DesignWorkspace(settings, repository).create_new_working_copy(
                    organization_id="org",
                    design_group_id="group",
                    family_id=None,
                    actor_id="owner",
                )

            self.assertIsNone(repository.created["model_revision_id"])
            self.assertEqual(repository.created["design_origin"], "new_design")

    def test_mandatory_failure_blocks_passed_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=package / "family.json",
            )
            design = DesignWorkspace(settings, FakeRepository())
            with self.assertRaises(ValueError):
                design.record_validation(
                    working_copy_id="copy",
                    change_set_id=None,
                    status="passed",
                    checks=[{"name": "shape", "mandatory": True, "status": "failed"}],
                )

    def test_typed_interface_validation_records_immutable_report_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            package = workspace / "agent"
            package.mkdir()
            working = workspace / "working.FCStd"
            working.write_bytes(b"working-model")
            report = workspace / "interface-validation.json"
            report.write_text('{"status":"passed"}\n', encoding="utf-8")
            settings = Settings(
                workspace=workspace,
                package_root=package,
                database_url="unused",
                neo4j_uri="unused",
                neo4j_user="unused",
                neo4j_password="unused",
                freecadcmd=Path("/bin/false"),
                actor_id="owner",
                artifact_root=package / "data",
                family_config_path=package / "family.json",
            )
            repository = FakeJobBindingRepository()
            repository.working_path = str(working)
            design = DesignWorkspace(
                settings,
                repository,
                FakeJobBindingManager(workspace),
            )

            for validation_kind in ("fastener_interfaces", "mechanical_interfaces"):
                with self.subTest(validation_kind=validation_kind):
                    design.record_validation(
                        working_copy_id="copy",
                        change_set_id=None,
                        status="passed",
                        checks=[{"name": "interface", "mandatory": True, "status": "passed"}],
                        report_path=str(report),
                        validation_kind=validation_kind,
                    )
                    self.assertEqual(repository.validation["validation_kind"], validation_kind)
                    self.assertEqual(repository.validation["report_sha256"], file_sha256(report))

    def test_passed_typed_validation_requires_a_report_file(self) -> None:
        design = DesignWorkspace.__new__(DesignWorkspace)

        with self.assertRaisesRegex(ValueError, "report_path"):
            design.record_validation(
                working_copy_id="copy",
                change_set_id=None,
                status="passed",
                checks=[{"name": "shape", "mandatory": True, "status": "passed"}],
                validation_kind="mechanical_interfaces",
            )


if __name__ == "__main__":
    unittest.main()
