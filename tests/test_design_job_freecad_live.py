from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest


LIVE_OPT_IN = "MECH_DESIGN_JOB_FREECAD_LIVE"
EXISTING_SOURCE = "MECH_DESIGN_JOB_LIVE_EXISTING_SOURCE"


def _job_root(workspace: Path, job: dict[str, object]) -> Path:
    directory_name = job.get("directory_name")
    assert isinstance(directory_name, str) and directory_name
    root = (workspace / "jobs" / directory_name).resolve()
    assert root.is_relative_to((workspace / "jobs").resolve())
    assert root.is_dir()
    return root


def _assert_governed_job_tree(root: Path, working_path: object) -> None:
    working = Path(str(working_path)).resolve()
    assert working.is_file()
    assert working.is_relative_to(root)
    assert working.suffix.casefold() == ".fcstd"
    assert not any(path.name == ".git" for path in root.rglob(".git"))


@pytest.mark.skipif(
    os.environ.get(LIVE_OPT_IN) != "1",
    reason="Design Job FreeCAD acceptance requires explicit live opt-in",
)
def test_new_existing_resume_and_independent_design_jobs_use_freecad() -> None:
    """Exercise the release-candidate Job/FreeCAD boundary against live services.

    The caller must provide an isolated initialized workspace, package-owned
    migrations 001-014, configured PostgreSQL/Neo4j, pinned FreeCADCmd 1.1.3,
    a selected synthetic product family, and a non-sensitive FCStd/STEP source.
    The acceptance target is disposable because this test creates durable Jobs.
    """
    from mechanical_design_agent.config import Settings
    from mechanical_design_agent.service import MechanicalDesignService

    source_raw = os.environ.get(EXISTING_SOURCE, "").strip()
    assert source_raw, f"{EXISTING_SOURCE} is required for live acceptance"
    source = Path(source_raw).expanduser().resolve(strict=True)
    assert source.is_file()
    assert source.suffix.casefold() in {".fcstd", ".step", ".stp"}

    settings = Settings.from_environment()
    service = MechanicalDesignService(settings)
    organization_id = str(service.bootstrap_config["organization_id"])
    design_group_id = str(service.bootstrap_config["design_group_id"])
    run = uuid4().hex

    new_job = service.design_job_create(
        job_type="mechanical_design",
        title=f"v0.3 live new design {run}",
        organization_id=organization_id,
        design_group_id=design_group_id,
        family_id=None,
        idempotency_token=f"v03-live-new-{run}",
    )
    new_working = service.design_job_new_working_copy_create(
        job_id=str(new_job["job_id"]),
        expected_job_revision=int(new_job["revision"]),
        organization_id=organization_id,
        design_group_id=design_group_id,
    )
    new_root = _job_root(settings.workspace.resolve(), new_job)
    _assert_governed_job_tree(new_root, new_working["working_path"])

    resumed = service.design_job_resolve(
        query=str(new_job["display_id"]),
        job_type="mechanical_design",
        statuses=("active", "blocked"),
    )
    candidates = resumed["candidates"]
    assert isinstance(candidates, list)
    assert [candidate["job_id"] for candidate in candidates] == [new_job["job_id"]]

    existing_staged = service.design_job_create(
        job_type="mechanical_design",
        title=f"v0.3 live existing design {run}",
        organization_id=organization_id,
        design_group_id=design_group_id,
        family_id=None,
        idempotency_token=f"v03-live-existing-{run}",
        source_files=[str(source)],
    )
    existing_job = existing_staged["job"]
    assert isinstance(existing_job, dict)
    existing_working = service.design_job_working_copy_create(
        job_id=str(existing_job["job_id"]),
        expected_job_revision=int(existing_job["revision"]),
        source_path=str(source),
        organization_id=organization_id,
        design_group_id=design_group_id,
    )
    existing_root = _job_root(settings.workspace.resolve(), existing_job)
    _assert_governed_job_tree(existing_root, existing_working["working_path"])
    snapshot = existing_working["source_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot_path = existing_root / str(snapshot["stored_path"])
    assert snapshot_path.is_file()
    assert snapshot_path.read_bytes() == source.read_bytes()

    independent = service.design_job_create(
        job_type="mechanical_design",
        title=f"v0.3 live new design independent {run}",
        organization_id=organization_id,
        design_group_id=design_group_id,
        family_id=None,
        idempotency_token=f"v03-live-independent-{run}",
    )
    assert independent["job_id"] not in {
        new_job["job_id"],
        existing_job["job_id"],
    }
    independent_root = _job_root(settings.workspace.resolve(), independent)
    assert not any(path.name == ".git" for path in independent_root.rglob(".git"))
