"""Safe, transport-neutral errors for the public Design Job interfaces."""

from __future__ import annotations

from typing import Any

from .jobs import JobFailure


_SAFE_MESSAGES = {
    "JOB_ACCESS_UNAVAILABLE": "The requested Job is unavailable in the authorized scope.",
    "JOB_NOT_FOUND_OR_UNAUTHORIZED": "The requested Job is unavailable in the authorized scope.",
    "JOB_AMBIGUOUS": "More than one authorized Job matches this reference.",
    "JOB_SOURCE_SNAPSHOTS_NOT_READY": "Source snapshots are not available for Job creation yet.",
    "JOB_SOURCE_CHANGED": "The selected CAD source changed while it was being bound.",
    "JOB_SOURCE_UNSAFE": "The selected CAD source cannot be read through a stable no-follow boundary.",
    "JOB_REVISION_STALE": "The Design Job revision is stale.",
    "JOB_STALE_REVISION": "The Design Job revision is stale.",
    "JOB_ACTIVE_WORKING_COPY_EXISTS": "The Design Job already has an active working copy.",
    "JOB_FCSTD_INVALID": "The controlled FCStd failed FreeCAD reopen and validation.",
    "JOB_NORMALIZATION_FAILED": "FreeCAD could not normalize the governed CAD source.",
    "JOB_OUTPUT_UNEXPECTED": "FreeCAD produced output outside the controlled file contract.",
    "JOB_DATABASE_COMMIT_UNKNOWN": "Working-copy database publication could not be reconciled.",
    "JOB_DATABASE_PUBLICATION_FAILED": "Working-copy database publication was not committed.",
    "JOB_ATTEMPT_RECOVERY_REQUIRED": "A preserved working-copy attempt requires explicit recovery.",
    "JOB_SOURCE_FILES_UNSUPPORTED_JOB_TYPE": "Source-file staging is unavailable for this Job type.",
    "JOB_SOURCE_FILES_COUNT_INVALID": "Source-file staging requires exactly one CAD source.",
    "JOB_SOURCE_FILE_INVALID": "The staged source reference is not a supported CAD file.",
}

_NEXT_ACTIONS = {
    "JOB_SOURCE_CHANGED": "Reopen the authoritative source revision and retry with its unchanged bytes.",
    "JOB_SOURCE_UNSAFE": "Select a regular non-linked FCStd or STEP source and retry.",
    "JOB_REVISION_STALE": "Run design_job_get, then retry with the returned revision.",
    "JOB_STALE_REVISION": "Run design_job_get, then retry with the returned revision.",
    "JOB_ACTIVE_WORKING_COPY_EXISTS": "Close the Job with design_job_close to release the slot before creating another working copy.",
    "JOB_FCSTD_INVALID": "Open and repair the FCStd in supported FreeCAD, then retry the governed creation.",
    "JOB_NORMALIZATION_FAILED": "Validate the source in supported FreeCAD and retry normalization.",
    "JOB_OUTPUT_UNEXPECTED": "Run mechanical-design job doctor --job <JOB-ID>; remove no files manually.",
    "JOB_DATABASE_COMMIT_UNKNOWN": "Run mechanical-design job doctor --job <JOB-ID>, then receipt-bound job repair before retrying.",
    "JOB_DATABASE_PUBLICATION_FAILED": "Refresh the Job state and retry the governed working-copy creation.",
    "JOB_ATTEMPT_RECOVERY_REQUIRED": "Run mechanical-design job doctor --job <JOB-ID>, then mechanical-design job repair --help; do not delete attempt bytes.",
    "JOB_SOURCE_FILES_UNSUPPORTED_JOB_TYPE": "Create a mechanical_design Job, then use design_job_working_copy_create.",
    "JOB_SOURCE_FILES_COUNT_INVALID": "Submit exactly one FCStd or STEP source reference.",
    "JOB_SOURCE_FILE_INVALID": "Submit one nonblank FCStd, STEP, or STP source reference.",
}


def safe_job_error(error: Exception) -> dict[str, object]:
    """Return a stable error without paths, titles, or exception internals."""
    code = error.code if isinstance(error, JobFailure) else "JOB_REQUEST_FAILED"
    message = _SAFE_MESSAGES.get(code, "The Design Job request could not be completed safely.")
    return {
        "schema_version": "MechanicalDesignJobError/v1",
        "status": "blocked",
        "code": code,
        "message": message,
        "next_action": _NEXT_ACTIONS.get(
            code,
            "Verify the Job reference and authorized scope, then retry.",
        ),
        "candidates": [],
    }


def safe_job_error_json(error: Exception) -> str:
    import json

    return json.dumps(safe_job_error(error), ensure_ascii=False, sort_keys=True)
