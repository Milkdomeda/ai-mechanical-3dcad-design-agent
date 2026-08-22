from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mechanical_design_agent.artifacts import ArtifactStore
from mechanical_design_agent.config import Settings
from mechanical_design_agent.design import DesignWorkspace, derive_iteration_candidates
from mechanical_design_agent.hashing import file_sha256
from mechanical_design_agent.secure_fs import SecureFilesystemError, relative_managed_path


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


class DesignWorkspaceTests(unittest.TestCase):
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
            repository = FakeRepository()
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

            approved = DesignWorkspace(settings, repository).approve_delivery(
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
                relative_managed_path(snapshot_path, settings.artifact_root).parts
            )
            self.assertEqual(approved["approved_final_sha256"], file_sha256(snapshot_path))

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
            repository = FakeRepository()
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
            repository = FakeRepository()
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

            def create_empty(_freecadcmd, _script, arguments, timeout_seconds):
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
            repository = FakeRepository()
            repository.working_path = str(working)
            design = DesignWorkspace(settings, repository)

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
