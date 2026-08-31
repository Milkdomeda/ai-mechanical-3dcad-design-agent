from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import uuid
import xml.etree.ElementTree as ET
import zipfile

from .approval_semantics import APPROVE, classify_approval
from .config import DesignSettings
from .fcstd_security import inspect_fcstd_bytes
from .freecad_runner import run_freecad_script
from .hashing import file_sha256
from .models import canonical_json, require_safe_id
from .package_resources import freecad_scripts_directory
from .secure_fs import (
    SecureFilesystemError,
    atomic_publish_directory,
    atomic_publish_new,
    atomic_replace,
    ensure_managed_directory,
    exclusive_file_lock,
    read_managed_file,
    relative_managed_path,
    remove_owned_tree,
    set_managed_file_readonly,
    validate_external_read_path,
    validate_managed_path,
)


_SESSION_SCHEMA = "DesignSession/v1"
_MODEL_STATUSES = frozenset(
    {"approved", "modeling", "needs_attention", "completed"}
)
_MODEL_CLASSIFICATIONS = frozenset({"new_design", "existing_model"})
_KNOWLEDGE_STATUSES = frozenset(
    {"not_executed", "completed_matches", "completed_no_match", "unavailable"}
)

SeedCreator = Callable[[Path], None]
SourceNormalizer = Callable[[Path, Path], None]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _strict_json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        copied = json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{label} must be a JSON object")
    return copied


def _shape_free_fcstd(contents: bytes) -> bool:
    inspect_fcstd_bytes(contents)
    with zipfile.ZipFile(BytesIO(contents), "r") as archive:
        document = archive.read("Document.xml")
    root = ET.fromstring(document)
    for element in root.iter():
        if element.tag.casefold() == "object":
            return False
    return True


def _session_identity(state: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        state.get("design_id"),
        state.get("title"),
        state.get("model_classification"),
        state.get("requirements"),
        state.get("proposal_summary"),
        (state.get("direction_approval") or {}).get("state")
        if isinstance(state.get("direction_approval"), Mapping)
        else None,
    )


def _validate_state(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("design.json must contain an object")
    state = _strict_json_object(raw, "design state")
    if state.get("schema_version") != _SESSION_SCHEMA:
        raise ValueError("design.json schema_version is incompatible")
    require_safe_id(str(state.get("design_id", "")), "design_id")
    if not isinstance(state.get("title"), str) or not state["title"].strip():
        raise ValueError("design.json title is invalid")
    if state.get("model_classification") not in _MODEL_CLASSIFICATIONS:
        raise ValueError("design.json model_classification is invalid")
    if state.get("model_status") not in _MODEL_STATUSES:
        raise ValueError("design.json model_status is invalid")
    if not isinstance(state.get("requirements"), dict):
        raise ValueError("design.json requirements are invalid")
    approval = state.get("direction_approval")
    if not isinstance(approval, dict) or approval.get("state") != APPROVE:
        raise ValueError("design.json direction_approval is invalid")
    final_confirmation = state.get("final_confirmation")
    if not isinstance(final_confirmation, dict):
        raise ValueError("design.json final_confirmation is invalid")
    if final_confirmation.get("state") not in {"not_confirmed", APPROVE}:
        raise ValueError("design.json final_confirmation state is invalid")
    lesson_review = state.get("lesson_review")
    if not isinstance(lesson_review, dict) or not isinstance(
        lesson_review.get("status"), str
    ):
        raise ValueError("design.json lesson_review is invalid")
    knowledge = state.get("knowledge")
    if (
        not isinstance(knowledge, dict)
        or knowledge.get("status") not in _KNOWLEDGE_STATUSES
        or not isinstance(knowledge.get("used_ids"), list)
    ):
        raise ValueError("design.json knowledge state is invalid")
    model = state.get("model")
    validation = state.get("validation")
    if not isinstance(model, dict) or model.get("relative_path") != "model.FCStd":
        raise ValueError("design.json model state is invalid")
    if not isinstance(validation, dict):
        raise ValueError("design.json validation state is invalid")
    return state


class DesignSessionService:
    """Own local, inspectable design-session state without a database dependency."""

    def __init__(
        self,
        settings: DesignSettings,
        *,
        seed_creator: SeedCreator | None = None,
        source_normalizer: SourceNormalizer | None = None,
    ) -> None:
        self.settings = settings
        self.seed_creator = seed_creator or self._create_seed_with_freecad
        self.source_normalizer = (
            source_normalizer or self._normalize_source_with_freecad
        )

    def _create_seed_with_freecad(self, destination: Path) -> None:
        with freecad_scripts_directory() as scripts:
            completed = run_freecad_script(
                self.settings.freecadcmd,
                scripts / "create_empty_working_copy.py",
                [destination],
                timeout_seconds=120,
                expected_sha256=self.settings.freecadcmd_sha256,
                expected_identity=self.settings.freecadcmd_identity,
                controlled_directory=destination.parent,
            )
        if completed.returncode != 0 or not destination.is_file():
            diagnostic = (completed.stderr + "\n" + completed.stdout)[-4000:]
            raise RuntimeError(f"FreeCAD could not create the design seed: {diagnostic}")

    def _normalize_source_with_freecad(
        self, source: Path, destination: Path
    ) -> None:
        with freecad_scripts_directory() as scripts:
            completed = run_freecad_script(
                self.settings.freecadcmd,
                scripts / "normalize_working_copy.py",
                [source, destination],
                timeout_seconds=900,
                expected_sha256=self.settings.freecadcmd_sha256,
                expected_identity=self.settings.freecadcmd_identity,
                controlled_directory=destination.parent,
            )
        if completed.returncode != 0 or not destination.is_file():
            diagnostic = (completed.stderr + "\n" + completed.stdout)[-4000:]
            raise RuntimeError(f"FreeCAD could not normalize the source: {diagnostic}")

    def _ensure_root(self) -> Path:
        workspace = validate_managed_path(
            self.settings.workspace, allow_missing_leaf=False
        ).path
        root = self.settings.design_root.expanduser()
        if not root.is_absolute():
            root = workspace / root
        try:
            relative_root = relative_managed_path(
                root,
                workspace,
                allow_missing_leaf=True,
            )
        except ValueError as exc:
            raise ValueError("design root must remain inside the workspace") from exc
        return ensure_managed_directory(
            workspace / relative_root, parents=True, exist_ok=True
        ).path

    def _root_for(self, design_id: str, *, must_exist: bool) -> Path:
        normalized = require_safe_id(design_id, "design_id")
        root = self.settings.design_root.expanduser()
        if not root.is_absolute():
            root = self.settings.workspace / root
        candidate = root / normalized
        if must_exist:
            return validate_managed_path(candidate, allow_missing_leaf=False).path
        return candidate

    @staticmethod
    def _read_state(root: Path) -> dict[str, Any]:
        read = read_managed_file(root / "design.json")
        try:
            parsed = json.loads(read.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("design.json is not valid UTF-8 JSON") from exc
        return _validate_state(parsed)

    @staticmethod
    def _replace_state(root: Path, state: Mapping[str, Any]) -> None:
        checked = _validate_state(dict(state))
        atomic_replace(
            root / "design.json", canonical_json(checked).encode("utf-8")
        )

    def start(
        self,
        *,
        design_id: str,
        title: str,
        model_classification: str,
        requirements: Mapping[str, Any],
        proposal_summary: str,
        approval_text: str,
        source_path: str | None = None,
    ) -> dict[str, object]:
        approval_state = classify_approval(approval_text)
        if approval_state != APPROVE:
            return {
                "schema_version": "DesignStartResult/v1",
                "status": "not_started",
                "approval_state": approval_state,
                "next_action": (
                    "revise_design" if approval_state == "REJECT" else "clarify_approval"
                ),
            }

        normalized_id = require_safe_id(design_id, "design_id")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a nonblank string")
        if model_classification not in _MODEL_CLASSIFICATIONS:
            raise ValueError("model_classification must be new_design or existing_model")
        requirement_copy = _strict_json_object(requirements, "requirements")
        if not isinstance(proposal_summary, str) or not proposal_summary.strip():
            raise ValueError("proposal_summary must be a nonblank string")
        if model_classification == "existing_model" and not source_path:
            raise ValueError("existing_model requires source_path")

        intended = {
            "design_id": normalized_id,
            "title": title.strip(),
            "model_classification": model_classification,
            "requirements": requirement_copy,
            "proposal_summary": proposal_summary.strip(),
            "direction_approval": {
                "state": APPROVE,
                "text": approval_text.strip(),
            },
        }
        designs_root = self._ensure_root()
        final = designs_root / normalized_id
        lock_path = designs_root / ".designs.lock"
        with exclusive_file_lock(lock_path):
            if final.exists():
                existing = self._read_state(
                    validate_managed_path(final, allow_missing_leaf=False).path
                )
                if _session_identity(existing) != _session_identity(intended):
                    raise ValueError(
                        "design_id already belongs to a different design intent"
                    )
                return self._start_result(final, existing, resumed=True)

            stage = designs_root / f".creating-{normalized_id}-{uuid.uuid4().hex}"
            ensure_managed_directory(stage, parents=False, exist_ok=False)
            try:
                ensure_managed_directory(
                    stage / "validation", parents=False, exist_ok=False
                )
                ensure_managed_directory(
                    stage / "output", parents=False, exist_ok=False
                )
                model_path = stage / "model.FCStd"
                source_relative: str | None = None
                source_sha: str | None = None

                if model_classification == "new_design":
                    seed_sha = self._create_new_model(model_path, source_path)
                else:
                    source_relative, source_sha = self._create_existing_model(
                        stage, model_path, str(source_path)
                    )
                    seed_sha = None

                model_read = read_managed_file(model_path)
                inspect_fcstd_bytes(model_read.content)
                now = _timestamp()
                state = {
                    "schema_version": _SESSION_SCHEMA,
                    **intended,
                    "model_status": "approved",
                    "knowledge": {
                        "status": "not_executed",
                        "used_ids": [],
                        "warning": None,
                    },
                    "model": {
                        "relative_path": "model.FCStd",
                        "sha256": None,
                        "seed_sha256": seed_sha,
                        "source_relative_path": source_relative,
                        "source_sha256": source_sha,
                    },
                    "validation": {
                        "status": "not_executed",
                        "working_sha256": None,
                        "report_relative_path": None,
                        "evidence_relative_paths": [],
                    },
                    "final_confirmation": {
                        "state": "not_confirmed",
                        "text": None,
                        "model_sha256": None,
                        "confirmed_at": None,
                    },
                    "lesson_review": {
                        "status": "not_evaluated",
                        "review_relative_path": None,
                        "review_sha256": None,
                        "warning": None,
                    },
                    "created_at": now,
                    "updated_at": now,
                }
                atomic_publish_new(
                    stage / "design.json",
                    canonical_json(_validate_state(state)).encode("utf-8"),
                )
                atomic_publish_directory(stage, final)
            except Exception:
                if stage.exists():
                    remove_owned_tree(
                        stage,
                        expected_parent=designs_root,
                        label="design creation attempt",
                    )
                raise

        root = validate_managed_path(final, allow_missing_leaf=False).path
        return self._start_result(root, self._read_state(root), resumed=False)

    def _create_new_model(self, model_path: Path, source_path: str | None) -> str:
        if source_path:
            source = validate_external_read_path(
                Path(source_path).expanduser().resolve(strict=True)
            )
            if source.suffix.casefold() != ".fcstd":
                raise ValueError("new_design seed must be an FCStd file")
            contents = source.read_bytes()
            if not _shape_free_fcstd(contents):
                raise ValueError("new_design seed must be shape-free")
            atomic_publish_new(model_path, contents)
            if source.read_bytes() != contents:
                raise RuntimeError("new-design seed changed while being copied")
            return file_sha256(model_path)

        self.seed_creator(model_path)
        read = read_managed_file(model_path)
        if not _shape_free_fcstd(read.content):
            raise ValueError("created new-design seed must be shape-free")
        return read.sha256

    def _create_existing_model(
        self, stage: Path, model_path: Path, source_path: str
    ) -> tuple[str, str]:
        source = validate_external_read_path(
            Path(source_path).expanduser().resolve(strict=True)
        )
        suffix = source.suffix.casefold()
        if suffix not in {".fcstd", ".step", ".stp"}:
            raise ValueError("existing_model source must be FCStd, STEP, or STP")
        contents = source.read_bytes()
        if suffix == ".fcstd":
            inspect_fcstd_bytes(contents)
        source_sha = file_sha256(source)
        source_dir = ensure_managed_directory(
            stage / "source", parents=False, exist_ok=False
        ).path
        snapshot = source_dir / ("source.FCStd" if suffix == ".fcstd" else "source.step")
        atomic_publish_new(snapshot, contents)
        set_managed_file_readonly(snapshot)
        if suffix == ".fcstd":
            atomic_publish_new(model_path, contents)
        else:
            self.source_normalizer(snapshot, model_path)
        if file_sha256(source) != source_sha or source.read_bytes() != contents:
            raise RuntimeError("source CAD changed while the session was created")
        inspect_fcstd_bytes(read_managed_file(model_path).content)
        return snapshot.relative_to(stage).as_posix(), source_sha

    @staticmethod
    def _start_result(
        root: Path, state: Mapping[str, Any], *, resumed: bool
    ) -> dict[str, object]:
        return {
            "schema_version": "DesignStartResult/v1",
            "status": state["model_status"],
            "approval_state": APPROVE,
            "design_id": state["design_id"],
            "design_root": str(root),
            "model_path": str(root / "model.FCStd"),
            "resumed": resumed,
            "next_action": "retrieve_knowledge",
        }

    def get(self, design_id: str) -> dict[str, Any]:
        root = self._root_for(design_id, must_exist=True)
        with exclusive_file_lock(root / ".design.lock"):
            state = self._read_state(root)
            model_path = root / "model.FCStd"
            recorded_sha = state["model"].get("sha256")
            if state["model_status"] == "completed" and (
                not model_path.is_file() or file_sha256(model_path) != recorded_sha
            ):
                state["model_status"] = "needs_attention"
                state["validation"]["status"] = "stale"
                state["validation"]["warning"] = (
                    "model changed after the recorded validation"
                )
                state["final_confirmation"] = {
                    "state": "not_confirmed",
                    "text": None,
                    "model_sha256": None,
                    "confirmed_at": None,
                }
                state["lesson_review"] = {
                    "status": "invalidated",
                    "review_relative_path": None,
                    "review_sha256": None,
                    "warning": "model changed after final result recording",
                }
                state["updated_at"] = _timestamp()
                self._replace_state(root, state)
            return state

    def record_knowledge(
        self,
        *,
        design_id: str,
        status: str,
        used_ids: Sequence[str],
        warning: str | None,
    ) -> dict[str, Any]:
        if status not in _KNOWLEDGE_STATUSES - {"not_executed"}:
            raise ValueError("knowledge status is invalid")
        normalized_ids: list[str] = []
        for value in used_ids:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("knowledge IDs must be nonblank strings")
            if value not in normalized_ids:
                normalized_ids.append(value)
        if warning is not None and not isinstance(warning, str):
            raise ValueError("knowledge warning must be a string or null")
        root = self._root_for(design_id, must_exist=True)
        with exclusive_file_lock(root / ".design.lock"):
            state = self._read_state(root)
            state["knowledge"] = {
                "status": status,
                "used_ids": normalized_ids,
                "warning": warning,
            }
            state["updated_at"] = _timestamp()
            self._replace_state(root, state)
            return state

    def confirm(self, *, design_id: str, confirmation_text: str) -> dict[str, object]:
        """Record final-model confirmation after exact-hash validation."""
        confirmation_state = classify_approval(confirmation_text)
        if confirmation_state != APPROVE:
            return {
                "schema_version": "DesignConfirmationResult/v1",
                "design_id": design_id,
                "confirmation_state": confirmation_state,
                "status": "not_confirmed",
                "next_action": (
                    "revise_design"
                    if confirmation_state == "REJECT"
                    else "clarify_confirmation"
                ),
            }
        root = self._root_for(design_id, must_exist=True)
        with exclusive_file_lock(root / ".design.lock"):
            state = self._read_state(root)
            model_sha256 = self._require_confirmable(root, state)
            existing = state["final_confirmation"]
            if (
                existing.get("state") == APPROVE
                and existing.get("model_sha256") == model_sha256
            ):
                return {
                    "schema_version": "DesignConfirmationResult/v1",
                    "design_id": design_id,
                    "confirmation_state": APPROVE,
                    "status": "confirmed",
                    "model_sha256": model_sha256,
                    "resumed": True,
                    "next_action": "evaluate_design_lessons",
                }
            state["final_confirmation"] = {
                "state": APPROVE,
                "text": confirmation_text.strip(),
                "model_sha256": model_sha256,
                "confirmed_at": _timestamp(),
            }
            state["lesson_review"] = {
                "status": "evaluation_pending",
                "review_relative_path": None,
                "review_sha256": None,
                "warning": None,
            }
            state["updated_at"] = _timestamp()
            self._replace_state(root, state)
            return {
                "schema_version": "DesignConfirmationResult/v1",
                "design_id": design_id,
                "confirmation_state": APPROVE,
                "status": "confirmed",
                "model_sha256": model_sha256,
                "resumed": False,
                "next_action": "evaluate_design_lessons",
            }

    def confirmation_context(self, design_id: str) -> dict[str, object]:
        """Return exact-hash evidence available to the lesson evaluator."""
        root = self._root_for(design_id, must_exist=True)
        with exclusive_file_lock(root / ".design.lock"):
            state = self._read_state(root)
            model_sha256 = self._require_confirmable(root, state)
            if (
                state["final_confirmation"].get("state") != APPROVE
                or state["final_confirmation"].get("model_sha256")
                != model_sha256
            ):
                raise ValueError("the completed design has not been confirmed")
            report_relative = state["validation"]["report_relative_path"]
            evidence_relative = list(
                state["validation"]["evidence_relative_paths"]
            )
            paths = [report_relative, *evidence_relative]
            evidence = []
            for relative in paths:
                path = self._inside(root, str(relative), "validation evidence")
                evidence.append(
                    {
                        "relative_path": path.relative_to(root).as_posix(),
                        "sha256": file_sha256(path),
                    }
                )
            return {
                "design_root": str(root),
                "design_id": state["design_id"],
                "title": state["title"],
                "model_sha256": model_sha256,
                "validation_report_sha256": evidence[0]["sha256"],
                "evidence": evidence,
            }

    def record_lesson_review(
        self,
        *,
        design_id: str,
        model_sha256: str,
        status: str,
        review_relative_path: str | None = None,
        review_sha256: str | None = None,
        warning: str | None = None,
        publication_id: str | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "candidate_errors",
            "no_material_lessons",
            "review_pending",
            "declined",
            "published",
            "publish_retry_required",
        }
        if status not in allowed:
            raise ValueError("lesson review status is invalid")
        root = self._root_for(design_id, must_exist=True)
        with exclusive_file_lock(root / ".design.lock"):
            state = self._read_state(root)
            current_sha256 = self._require_confirmable(root, state)
            if current_sha256 != model_sha256:
                raise ValueError("lesson review model SHA-256 is stale")
            if state["final_confirmation"].get("model_sha256") != current_sha256:
                raise ValueError("lesson review requires final confirmation")
            if status == "review_pending":
                if not review_relative_path or not review_sha256:
                    raise ValueError("review_pending requires a review path and SHA-256")
                review_path = self._inside(
                    root, review_relative_path, "review_relative_path"
                )
                if file_sha256(review_path) != review_sha256:
                    raise ValueError("review card SHA-256 does not match")
                normalized_path = review_path.relative_to(root).as_posix()
            else:
                normalized_path = review_relative_path
            state["lesson_review"] = {
                "status": status,
                "review_relative_path": normalized_path,
                "review_sha256": review_sha256,
                "warning": warning,
                "publication_id": publication_id,
            }
            state["updated_at"] = _timestamp()
            self._replace_state(root, state)
            return state

    def _require_confirmable(
        self, root: Path, state: Mapping[str, Any]
    ) -> str:
        if state["model_status"] != "completed":
            raise ValueError("final confirmation requires a completed model")
        if state["validation"].get("status") != "passed":
            raise ValueError("final confirmation requires passed validation")
        model = root / "model.FCStd"
        model_read = read_managed_file(model)
        inspect_fcstd_bytes(model_read.content)
        recorded_sha256 = state["model"].get("sha256")
        validation_sha256 = state["validation"].get("working_sha256")
        if not (
            model_read.sha256 == recorded_sha256 == validation_sha256
        ):
            raise ValueError("final confirmation requires exact-hash validation")
        evidence = [
            self._inside(root, str(value), "validation evidence")
            for value in state["validation"].get("evidence_relative_paths", [])
        ]
        suffixes = {path.suffix.casefold() for path in evidence}
        if ".md" not in suffixes or ".png" not in suffixes:
            raise ValueError("final confirmation requires Markdown and PNG evidence")
        report = self._inside(
            root,
            str(state["validation"].get("report_relative_path")),
            "validation report",
        )
        if not report.is_file() or any(
            not path.is_file() or path.stat().st_size <= 0 for path in evidence
        ):
            raise ValueError("final confirmation evidence is incomplete")
        return model_read.sha256

    def _inside(
        self,
        root: Path,
        value: str,
        label: str,
        *,
        allow_missing_leaf: bool = False,
    ) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            relative_managed_path(
                candidate, root, allow_missing_leaf=allow_missing_leaf
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"{label} must remain inside the design session") from exc
        return validate_managed_path(
            candidate, allow_missing_leaf=allow_missing_leaf
        ).path

    def record_result(
        self,
        *,
        design_id: str,
        model_path: str,
        validation_report_path: str,
        evidence_paths: Sequence[str],
    ) -> dict[str, object]:
        root = self._root_for(design_id, must_exist=True)
        with exclusive_file_lock(root / ".design.lock"):
            state = self._read_state(root)
            model = self._inside(root, model_path, "model_path")
            if model != root / "model.FCStd":
                raise ValueError("model_path must identify the session model.FCStd")
            report_path = self._inside(
                root,
                validation_report_path,
                "validation_report_path",
                allow_missing_leaf=True,
            )
            evidence = [
                self._inside(
                    root, value, "evidence path", allow_missing_leaf=True
                )
                for value in evidence_paths
            ]
            model_read = read_managed_file(model)
            inspect_fcstd_bytes(model_read.content)
            result_status, validation_status, warning = self._check_validation(
                report_path=report_path,
                evidence=evidence,
                model_sha256=model_read.sha256,
            )
            state["model_status"] = result_status
            state["model"]["sha256"] = model_read.sha256
            state["validation"] = {
                "status": validation_status,
                "working_sha256": model_read.sha256,
                "report_relative_path": report_path.relative_to(root).as_posix(),
                "evidence_relative_paths": [
                    path.relative_to(root).as_posix() for path in evidence
                ],
                "warning": warning,
            }
            state["final_confirmation"] = {
                "state": "not_confirmed",
                "text": None,
                "model_sha256": None,
                "confirmed_at": None,
            }
            state["lesson_review"] = {
                "status": "not_evaluated",
                "review_relative_path": None,
                "review_sha256": None,
                "warning": None,
            }
            state["updated_at"] = _timestamp()
            self._replace_state(root, state)
            return {
                "schema_version": "DesignResult/v1",
                "design_id": design_id,
                "status": result_status,
                "working_sha256": model_read.sha256,
                "validation_status": validation_status,
                "warning": warning,
            }

    @staticmethod
    def _check_validation(
        *, report_path: Path, evidence: Sequence[Path], model_sha256: str
    ) -> tuple[str, str, str | None]:
        try:
            report = json.loads(read_managed_file(report_path).content.decode("utf-8"))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            OSError,
            SecureFilesystemError,
        ) as exc:
            return "needs_attention", "incomplete", f"invalid validation JSON: {exc}"
        if not isinstance(report, dict):
            return "needs_attention", "incomplete", "validation report is not an object"
        if report.get("working_sha256") != model_sha256:
            return "needs_attention", "stale", "validation report hash is stale"
        checks = report.get("checks")
        inventory = report.get("fastener_inventory")
        summary = report.get("summary")
        if not isinstance(checks, list) or not isinstance(inventory, list) or not isinstance(summary, dict):
            return "needs_attention", "incomplete", "validation report contract is incomplete"
        if summary.get("fasteners_detected") != len(inventory):
            return "needs_attention", "incomplete", "fastener inventory count is inconsistent"
        for check in checks:
            if not isinstance(check, dict) or any(
                field not in check
                for field in ("id", "validator", "status", "message", "mandatory")
            ):
                return "needs_attention", "incomplete", "validation check contract is incomplete"
            if check.get("mandatory") is True and check.get("status") != "passed":
                return "needs_attention", "failed", "mandatory validation check failed"
        suffixes = {path.suffix.casefold() for path in evidence if path.is_file()}
        if ".md" not in suffixes or ".png" not in suffixes:
            return "needs_attention", "incomplete", "Markdown and PNG evidence are required"
        if any(path.stat().st_size <= 0 for path in evidence):
            return "needs_attention", "incomplete", "validation evidence is empty"
        if report.get("status") != "passed":
            return "needs_attention", "failed", "validation report did not pass"
        return "completed", "passed", None


__all__ = ["DesignSessionService"]
