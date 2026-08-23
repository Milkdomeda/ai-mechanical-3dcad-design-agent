"""Safe, transport-neutral errors for the public Design Job interfaces."""

from __future__ import annotations

from typing import Any

from .jobs import JobFailure


_SAFE_MESSAGES = {
    "JOB_ACCESS_UNAVAILABLE": "The requested Job is unavailable in the authorized scope.",
    "JOB_NOT_FOUND_OR_UNAUTHORIZED": "The requested Job is unavailable in the authorized scope.",
    "JOB_AMBIGUOUS": "More than one authorized Job matches this reference.",
    "JOB_SOURCE_SNAPSHOTS_NOT_READY": "Source snapshots are not available for Job creation yet.",
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
        "next_action": "Verify the Job reference and authorized scope, then retry.",
        "candidates": [],
    }


def safe_job_error_json(error: Exception) -> str:
    import json

    return json.dumps(safe_job_error(error), ensure_ascii=False, sort_keys=True)
