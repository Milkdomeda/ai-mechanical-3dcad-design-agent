from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .bootstrap_diagnostics import CapabilityRequest, DiagnosticGateError
from .bootstrap_runtime import BootstrapRuntime
from .config import Settings
from .context import DesignContextBuilder
from .design_lessons import match_design_lesson, normalize_design_features
from .engineering_obligations import validate_engineering_scope
from .models import require_safe_id
from .service import MechanicalDesignService
from .job_errors import safe_job_error_json
from .tool_profiles import ProfiledToolRegistrar, resolve_tool_profile
from .workspace_bootstrap import BootstrapFailure


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
            allow_nan=False,
        )
    except ValueError as exc:
        raise ValueError("non-finite JSON value cannot be serialized") from exc


def _job_json(call: Callable[[], object]) -> str:
    """Map every public Job failure to the redacted v1 transport contract."""
    try:
        return _json(call())
    except Exception as exc:
        raise ToolError(safe_job_error_json(exc)) from None


def _optional_job_binding(
    job_id: str, expected_job_revision: int
) -> dict[str, object]:
    has_job = bool(job_id.strip())
    has_revision = expected_job_revision >= 0
    if has_job != has_revision:
        raise ValueError(
            "job_id and expected_job_revision must be supplied together"
        )
    if not has_job:
        return {}
    return {
        "job_id": job_id.strip(),
        "expected_job_revision": expected_job_revision,
    }


def _strict_json_loads(value: str) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"strict JSON does not allow {constant}")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"strict JSON parse failed: {exc.msg}") from None

    def reject_non_finite(item: Any) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("strict JSON does not allow non-finite numbers")
        if isinstance(item, dict):
            for nested in item.values():
                reject_non_finite(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_non_finite(nested)

    reject_non_finite(parsed)
    return parsed


def _object(value: str, label: str) -> dict[str, Any]:
    parsed = _strict_json_loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _array(value: str, label: str) -> list[Any]:
    parsed = _strict_json_loads(value or "[]")
    if not isinstance(parsed, list):
        raise ValueError(f"{label} must be a JSON array")
    return parsed


def _required_object(value: str, label: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return _object(value, label)


def _required_array(value: str, label: str) -> list[Any]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return _array(value, label)


_STRING_LIST_FEATURES = (
    "component_classes",
    "interface_types",
    "design_stages",
    "failure_modes",
    "satisfied_conditions",
    "declared_conditions",
)


def _capability(
    name: str,
    *additional_components: str,
) -> CapabilityRequest:
    return CapabilityRequest(name, tuple(additional_components))


SERVICE_METHOD_CAPABILITIES: Mapping[str, CapabilityRequest] = MappingProxyType(
    {
        "bootstrap_config": _capability("design_knowledge", "product_family"),
        "family_bootstrap_get": _capability(
            "family_create_or_manage", "product_family"
        ),
        "family_bootstrap_update": _capability(
            "family_create_or_manage", "product_family"
        ),
        "design_group_register": _capability("family_create_or_manage"),
        "family_create": _capability("family_create_or_manage"),
        "family_folder_confirm": _capability(
            "family_create_or_manage", "product_family"
        ),
        "family_compare_models": _capability(
            "family_create_or_manage", "product_family"
        ),
        "subfamily_propose": _capability(
            "family_create_or_manage", "product_family"
        ),
        "subfamily_review": _capability(
            "family_create_or_manage", "product_family"
        ),
        "subfamily_get": _capability(
            "family_create_or_manage", "product_family"
        ),
        "family_profile_propose": _capability(
            "family_create_or_manage", "product_family"
        ),
        "family_profile_review": _capability(
            "family_create_or_manage", "product_family"
        ),
        "family_profile_get": _capability(
            "family_create_or_manage", "product_family"
        ),
        "library_register": _capability("library_ingest"),
        "library_scan": _capability("library_ingest"),
        "library_ingest_changes": _capability("library_ingest"),
        "design_job_create": _capability("design_job_workspace"),
        "design_job_list": _capability("design_job_workspace"),
        "design_job_get": _capability("design_job_workspace"),
        "design_job_resolve": _capability("design_job_workspace"),
        "design_job_close": _capability("design_job_workspace"),
        "design_job_reopen": _capability("design_job_workspace"),
        "design_job_obligations_resolve": _capability(
            "design_job_workspace", "postgresql"
        ),
        "product_family_onboarding_start": _capability("design_job_workspace"),
        "product_family_onboarding_analyze": _capability("design_job_workspace"),
        "product_family_onboarding_review": _capability("design_job_workspace"),
        "product_family_onboarding_publish": _capability("design_job_workspace"),
        "product_family_onboarding_status": _capability("design_job_workspace"),
        "job_get": _capability("library_ingest"),
        "model_get_analysis": _capability("library_ingest"),
        "model_identity_confirm": _capability("library_ingest"),
        "evidence_artifact_register": _capability("artifact_registration"),
        "learning_start_session": _capability(
            "design_knowledge", "product_family"
        ),
        "learning_next_targets": _capability(
            "design_knowledge", "product_family"
        ),
        "learning_record_exchange": _capability(
            "design_knowledge", "product_family"
        ),
        "learning_defer_targets": _capability(
            "design_knowledge", "product_family"
        ),
        "knowledge_propose_assertions": _capability(
            "design_knowledge", "product_family"
        ),
        "knowledge_review": _capability("design_knowledge", "product_family"),
        "knowledge_search": _capability("design_knowledge", "product_family"),
        "design_context_build": _capability("design_knowledge"),
        "design_lesson_review_context": _capability("design_knowledge"),
        "design_lesson_review_prepare": _capability("design_knowledge"),
        "design_lesson_review_approve": _capability("design_knowledge"),
        "design_lesson_review_reject": _capability("design_knowledge"),
        "design_lesson_review_status": _capability("design_knowledge"),
        "design_lesson_review_publish": _capability("design_knowledge"),
        "design_lesson_review_no_publish": _capability("design_knowledge"),
        "design_lesson_stage": _capability("design_knowledge"),
        "design_lesson_staged_get": _capability("design_knowledge"),
        "design_lesson_approve": _capability("design_knowledge"),
        "design_lesson_search_page": _capability("design_knowledge"),
        "design_lesson_get": _capability("design_knowledge"),
        "design_lesson_audit_get": _capability("design_knowledge"),
        "design_lesson_supersede": _capability("design_knowledge"),
        "design_lesson_revoke": _capability("design_knowledge"),
        "design_knowledge_retrieve": _capability("design_knowledge"),
        "design_retrieval_receipt_get": _capability("design_knowledge"),
        "product_family_inventory": _capability("product_family_discovery"),
        "product_family_match": _capability("product_family_discovery"),
        "design_working_copy_create": _capability("cad_working_copy"),
        "design_new_working_copy_create": _capability("cad_working_copy"),
        "design_job_working_copy_create": _capability(
            "design_job_workspace", "freecadcmd"
        ),
        "design_job_new_working_copy_create": _capability(
            "design_job_workspace", "freecadcmd"
        ),
        "design_change_record": _capability("cad_working_copy"),
        "design_change_review": _capability("cad_working_copy"),
        "design_approval_envelope_get": _capability("cad_working_copy"),
        "design_change_audit_history": _capability("cad_working_copy"),
        "design_change_mutation_authorize": _capability("cad_working_copy"),
        "design_change_applied": _capability("cad_working_copy"),
        "design_change_close": _capability("cad_working_copy"),
        "design_confirmation_record": _capability("cad_working_copy"),
        "design_delivery_approve": _capability("cad_working_copy"),
        "design_validation_record": _capability(
            "model_validation", "postgresql"
        ),
        "design_assembly_completeness_validate": _capability(
            "model_validation", "postgresql"
        ),
        "standard_part_providers_get": _capability(
            "standard_part_provider_list"
        ),
        "standard_part_download_register": _capability(
            "standard_part_catalog_write_or_reuse"
        ),
        "projection_sync": _capability("projection"),
        "projection_rebuild": _capability("projection"),
    }
)


class _LazyServiceProxy:
    def __init__(
        self,
        *,
        runtime: BootstrapRuntime,
        service_factory: Callable[[object], Any],
    ) -> None:
        self.runtime = runtime
        self.service_factory = service_factory
        self._services: dict[str, Any] = {}
        self._lock = Lock()

    def _mapping_failure(self, member: str) -> ToolError:
        response = {
            "schema_version": "MechanicalDesignSetupResponse/v1",
            "status": "blocked",
            "code": "CAPABILITY_MAPPING_MISSING",
            "message": f"operational member has no capability mapping: {member}",
            "capability": None,
            "next_steps": [],
            "diagnostics": self.runtime.status(),
        }
        return ToolError(_json(response))

    def _load(self, request: CapabilityRequest) -> Any:
        try:
            self.runtime.require_initialized(request.capability)
            self.runtime.require_capability(request, probe=True)
        except DiagnosticGateError as exc:
            raise ToolError(_json(exc.response)) from None
        if request.capability == "design_job_workspace":
            key = (
                "job_cad"
                if "freecadcmd" in request.additional_components
                else "job"
            )
        elif "product_family" in request.additional_components:
            key = "family_operational"
        else:
            key = "operational"
        if key in self._services:
            return self._services[key]
        with self._lock:
            if key in self._services:
                return self._services[key]
            try:
                if key == "job":
                    settings = self.runtime.job_operational_settings()
                elif key == "job_cad":
                    settings = self.runtime.job_cad_operational_settings()
                elif key == "family_operational":
                    settings = self.runtime.family_operational_settings()
                else:
                    settings = self.runtime.operational_settings()
                self._services[key] = self.service_factory(settings)
            except DiagnosticGateError as exc:
                raise ToolError(_json(exc.response)) from None
            except Exception as exc:
                response = self.runtime.blocked_response(
                    capability=request.capability,
                    code="SERVICE_CONFIGURATION_BLOCKED",
                    message=(
                        "legacy service configuration is not ready: "
                        f"{type(exc).__name__}"
                    ),
                )
                raise ToolError(_json(response)) from None
        return self._services[key]

    def __getattr__(self, member: str) -> Any:
        request = SERVICE_METHOD_CAPABILITIES.get(member)
        if request is None:
            def blocked(*args: object, **kwargs: object) -> None:
                del args, kwargs
                raise self._mapping_failure(member)

            return blocked
        service = self._load(request)
        return getattr(service, member)


def _design_features(value: str) -> dict[str, Any]:
    features = _object(value, "design_features_json")
    for name in _STRING_LIST_FEATURES:
        if name not in features:
            continue
        items = features[name]
        if not isinstance(items, list):
            raise ValueError(f"{name} must be a JSON array")
        if not all(isinstance(item, str) for item in items):
            raise ValueError(f"{name} must contain only strings")
    if "explicit_requirements" in features:
        requirements = features["explicit_requirements"]
        if not isinstance(requirements, list):
            raise ValueError("explicit_requirements must be a JSON array")
        if not all(isinstance(item, (dict, str)) for item in requirements):
            raise ValueError("explicit_requirements must contain only objects or strings")
    return normalize_design_features(features)


def _safe_design_lesson_search_response(
    lessons: list[dict[str, Any]],
    *,
    features: dict[str, Any],
    query: str,
    limit: int,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    excluded_candidates: list[dict[str, Any]] = []
    for lesson in lessons:
        match = match_design_lesson(lesson, features, query)
        design_lesson_ref = DesignContextBuilder._opaque_lesson_ref(lesson)
        explanation = DesignContextBuilder._render_lesson_match(
            lesson,
            match,
            source_family_authorized=False,
            design_lesson_ref=design_lesson_ref,
        )
        if not match["eligible"]:
            if len(excluded_candidates) < limit:
                excluded_candidates.append(
                    {
                        **explanation,
                        "reason": match["exclusion_reasons"][0],
                        "reasons": match["exclusion_reasons"],
                    }
                )
            continue
        if len(matches) >= limit:
            continue
        matches.append(
            {
                "lesson": DesignContextBuilder._render_lesson(
                    lesson,
                    source_family_authorized=False,
                    design_lesson_ref=design_lesson_ref,
                ),
                "match": explanation,
            }
        )
    return {
        "schema_version": "DesignLessonSearch/v1",
        "matches": matches,
        "excluded_candidates": excluded_candidates,
        "next_cursor": next_cursor,
    }


def create_mcp(
    *,
    service: Any | None = None,
    runtime: BootstrapRuntime | None = None,
    product_family_id: str | None = None,
    service_factory: Callable[[object], Any] | None = None,
    tool_profile: str | None = None,
) -> FastMCP:
    bootstrap_runtime = runtime or BootstrapRuntime.from_process(
        cwd=Path.cwd(),
        environ=os.environ,
        product_family_id=product_family_id,
    )
    operational_service = (
        service
        if service is not None
        else _LazyServiceProxy(
            runtime=bootstrap_runtime,
            service_factory=service_factory or MechanicalDesignService,
        )
    )
    selected_profile = resolve_tool_profile(tool_profile)
    mcp_server = FastMCP("FreeCAD Mechanical Design Knowledge")
    registrar = ProfiledToolRegistrar(mcp_server, selected_profile)
    mcp = registrar

    @mcp.tool()
    def design_system_status() -> str:
        """Inspect bootstrap and configured runtime status without constructing the full service."""
        return _json(bootstrap_runtime.status())

    @mcp.tool()
    def design_system_doctor() -> str:
        """Run bounded read-only readiness probes without migrations or implicit setup."""
        return _json(bootstrap_runtime.doctor())

    @mcp.tool()
    def workspace_product_family_list() -> str:
        """List workspace-registered product families without constructing the service."""
        try:
            return _json(bootstrap_runtime.list_product_families())
        except BootstrapFailure as exc:
            raise ToolError(_json(exc.as_dict())) from None

    @mcp.tool()
    def workspace_product_family_create(
        organization_id: str,
        organization_name: str,
        design_group_id: str,
        design_group_name: str,
        family_id: str,
        family_name: str,
        aliases_json: str = "[]",
        confirmation: str = "",
    ) -> str:
        """Atomically register a first-use workspace family without database access."""
        if confirmation != f"CREATE {family_id}":
            raise ToolError(
                _json(
                    {
                        "schema_version": "MechanicalDesignSetupResponse/v1",
                        "status": "blocked",
                        "code": "CONFIRMATION_REQUIRED",
                        "message": f"confirmation must equal CREATE {family_id}",
                        "capability": "family_create_or_manage",
                        "next_steps": [],
                    }
                )
            )
        aliases = _array(aliases_json, "aliases_json")
        if not all(isinstance(item, str) for item in aliases):
            raise ValueError("aliases_json must contain only strings")
        try:
            return _json(
                bootstrap_runtime.create_product_family(
                    organization_id=organization_id,
                    organization_name=organization_name,
                    design_group_id=design_group_id,
                    design_group_name=design_group_name,
                    family_id=family_id,
                    family_name=family_name,
                    aliases=aliases,
                )
            )
        except BootstrapFailure as exc:
            raise ToolError(_json(exc.as_dict())) from None

    @mcp.tool()
    def workspace_product_family_set_default(
        family_id: str,
        confirmation: str,
    ) -> str:
        """Explicitly persist one registered family as the workspace default."""
        if confirmation != f"SET DEFAULT {family_id}":
            raise ToolError(
                _json(
                    {
                        "schema_version": "MechanicalDesignSetupResponse/v1",
                        "status": "blocked",
                        "code": "CONFIRMATION_REQUIRED",
                        "message": f"confirmation must equal SET DEFAULT {family_id}",
                        "capability": "family_create_or_manage",
                        "next_steps": [],
                    }
                )
            )
        try:
            return _json(bootstrap_runtime.set_default_product_family(family_id))
        except BootstrapFailure as exc:
            raise ToolError(_json(exc.as_dict())) from None

    @mcp.tool()
    def workspace_product_family_active(product_family_id: str = "") -> str:
        """Inspect a request override or the immutable MCP session family selection."""
        runtime_for_request = (
            bootstrap_runtime.for_product_family(product_family_id)
            if product_family_id
            else bootstrap_runtime
        )
        try:
            return _json(runtime_for_request.active_product_family())
        except BootstrapFailure as exc:
            raise ToolError(_json(exc.as_dict())) from None

    service = operational_service

    @mcp.tool()
    def family_bootstrap_get() -> str:
        """Get the selected product-family configuration from file and PostgreSQL."""
        return _json(service.family_bootstrap_get())

    @mcp.tool()
    def family_bootstrap_update(patch_json: str, confirmation: str) -> str:
        """Update mutable family bootstrap fields with an explicit confirmation containing family_id and 更新."""
        return _json(service.family_bootstrap_update(_object(patch_json, "patch_json"), confirmation))

    @mcp.tool()
    def design_group_register(design_group_id: str, design_group_name: str, confirmation: str) -> str:
        """Register or rename a design group under the selected organization with explicit confirmation."""
        return _json(service.design_group_register(design_group_id, design_group_name, confirmation))

    @mcp.tool()
    def family_create(
        family_id: str,
        family_name: str,
        design_group_id: str,
        aliases_json: str = "[]",
        confirmation: str = "",
    ) -> str:
        """Create a generic awaiting-source-folder family; no product terminology is generated by the service."""
        aliases = _array(aliases_json, "aliases_json")
        if not all(isinstance(item, str) for item in aliases):
            raise ValueError("aliases_json must contain only strings")
        return _json(
            service.family_create(
                family_id=family_id,
                family_name=family_name,
                design_group_id=design_group_id,
                aliases=aliases,
                confirmation=confirmation,
            )
        )

    @mcp.tool()
    def library_register(root_path: str) -> str:
        """Register one user-selected CAD library root as read-only; this does not scan or ingest files."""
        return _json(service.library_register(root_path))

    @mcp.tool()
    def evidence_artifact_register(file_path: str, media_type: str) -> str:
        """Copy a screenshot, report, or other evidence file into the content-addressed audit store."""
        return _json(service.evidence_artifact_register(file_path, media_type))

    @mcp.tool()
    def library_scan(library_id: str = "") -> str:
        """Manually scan a registered root and report changes; never starts ingestion automatically."""
        return _json(service.library_scan(library_id))

    @mcp.tool()
    def family_folder_confirm(library_id: str, folder_name: str, family_id: str, confirmation: str) -> str:
        """Confirm that a first-level library folder is an engineer-declared product family mapping."""
        return _json(service.family_folder_confirm(library_id, folder_name, family_id, confirmation))

    @mcp.tool()
    def library_ingest_changes(
        selection_json: str,
        library_id: str = "",
        wait_for_completion: bool = False,
        reanalyze: bool = False,
    ) -> str:
        """Ingest selected pending paths, or explicitly reanalyze ingested paths as a new parser revision."""
        selection = _array(selection_json, "selection_json")
        if not all(isinstance(item, str) for item in selection):
            raise ValueError("selection_json must contain only relative path strings")
        return _json(
            service.library_ingest_changes(
                selection,
                library_id,
                wait_for_completion,
                reanalyze,
            )
        )

    @mcp.tool()
    def design_job_create(
        job_type: str,
        title: str,
        organization_id: str,
        design_group_id: str,
        idempotency_token: str,
        family_id: str = "",
        source_files: list[str] = [],
    ) -> str:
        """Create one scoped Design Job. Product operations do not create a Git worktree."""
        return _job_json(lambda:
            service.design_job_create(
                job_type=job_type,
                title=title,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id or None,
                idempotency_token=idempotency_token,
                source_files=source_files,
            )
        )

    @mcp.tool()
    def design_job_list(
        status: str = "",
        job_type: str = "",
        family_id: str = "",
    ) -> str:
        """List authorized Design Jobs. Product operations do not create a Git worktree."""
        return _job_json(lambda:
            service.design_job_list(
                status=status or None,
                job_type=job_type or None,
                family_id=family_id or None,
            )
        )

    @mcp.tool()
    def design_job_get(job_id: str) -> str:
        """Read one Job UUID/display ID, never a filesystem path. Product operations do not create a Git worktree."""
        return _job_json(lambda: service.design_job_get(job_id=job_id))

    @mcp.tool()
    def design_job_resolve(
        query: str,
        job_type: str = "",
        family_id: str = "",
        statuses_json: str = '["active", "blocked"]',
    ) -> str:
        """Return all authorized Job candidates without choosing one. Product operations do not create a Git worktree."""
        def invoke() -> object:
            statuses = _array(statuses_json, "statuses_json")
            if not all(isinstance(item, str) for item in statuses):
                raise ValueError("statuses_json must contain only status strings")
            return service.design_job_resolve(
                query=query, job_type=job_type or None, family_id=family_id or None,
                statuses=tuple(statuses),
            )
        return _job_json(invoke)

    @mcp.tool()
    def design_job_close(
        job_id: str,
        expected_revision: int,
        status: str,
        phase: str,
        reason: str,
        confirmation: str,
    ) -> str:
        """Close a Job with revision, reason, and user confirmation. Product operations do not create a Git worktree."""
        return _job_json(lambda:
            service.design_job_close(
                job_id=job_id,
                expected_revision=expected_revision,
                status=status,
                phase=phase,
                reason=reason,
                confirmation=confirmation,
            )
        )

    @mcp.tool()
    def design_job_reopen(
        job_id: str,
        expected_revision: int,
        phase: str,
        reason: str,
        confirmation: str,
    ) -> str:
        """Reopen a terminal Job with revision, reason, and user confirmation. Product operations do not create a Git worktree."""
        return _job_json(lambda:
            service.design_job_reopen(
                job_id=job_id,
                expected_revision=expected_revision,
                phase=phase,
                reason=reason,
                confirmation=confirmation,
            )
        )

    @mcp.tool()
    def design_job_obligations_resolve(
        job_id: str,
        working_copy_id: str,
        engineering_scope_json: str,
        decisions_json: str,
        confirmation: str = "",
    ) -> str:
        """Record exact-scope standard-parts and assembly conclusions for a Design Job."""
        engineering_scope = _object(
            engineering_scope_json, "engineering_scope_json"
        )
        decisions = _array(decisions_json, "decisions_json")
        if not all(isinstance(item, dict) for item in decisions):
            raise ValueError("decisions_json must contain only JSON objects")
        return _job_json(
            lambda: service.design_job_obligations_resolve(
                job_id=require_safe_id(job_id, "job_id"),
                working_copy_id=require_safe_id(
                    working_copy_id, "working_copy_id"
                ),
                engineering_scope=engineering_scope,
                decisions=decisions,
                confirmation=confirmation,
            )
        )

    @mcp.tool()
    def product_family_onboarding_start(
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        source_paths_json: str,
    ) -> str:
        """Snapshot Product Family source models into an active onboarding Job."""
        def invoke() -> object:
            source_paths = _required_array(source_paths_json, "source_paths_json")
            if not all(isinstance(item, str) for item in source_paths):
                raise ValueError("source_paths_json must contain only path strings")
            return service.product_family_onboarding_start(
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                family_id=family_id,
                source_paths=source_paths,
            )
        return _job_json(invoke)

    @mcp.tool()
    def product_family_onboarding_analyze(
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        analysis_json: str,
        candidate_knowledge_json: str,
    ) -> str:
        """Store deterministic analysis and reviewable family-knowledge candidates in the same Job."""
        return _job_json(lambda: service.product_family_onboarding_analyze(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            family_id=family_id,
            analysis=_required_object(analysis_json, "analysis_json"),
            candidate_knowledge=_required_array(
                candidate_knowledge_json, "candidate_knowledge_json"
            ),
        ))

    @mcp.tool()
    def product_family_onboarding_review(
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        package_sha256: str,
        decision: str,
        reviewer_text: str,
        confirmation: str,
    ) -> str:
        """Approve or reject one exact candidate package as the configured family owner."""
        return _job_json(lambda: service.product_family_onboarding_review(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            family_id=family_id,
            package_sha256=package_sha256,
            decision=decision,
            reviewer_text=reviewer_text,
            confirmation=confirmation,
        ))

    @mcp.tool()
    def product_family_onboarding_publish(
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        package_sha256: str,
        review_identity: str,
        confirmation: str,
    ) -> str:
        """Publish one approved package transactionally to PostgreSQL and the outbox."""
        return _job_json(lambda: service.product_family_onboarding_publish(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            family_id=family_id,
            package_sha256=package_sha256,
            review_identity=review_identity,
            confirmation=confirmation,
        ))

    @mcp.tool()
    def product_family_onboarding_status(job_id: str) -> str:
        """Read the authoritative onboarding run, review, and publication identities."""
        return _job_json(lambda: service.product_family_onboarding_status(job_id=job_id))

    @mcp.tool()
    def job_get(job_id: str) -> str:
        """Get a CAD ingestion job status, results, diagnostics, or failure."""
        return _json(service.job_get(job_id))

    @mcp.tool()
    def model_get_analysis(model_revision_id: str) -> str:
        """Get ModelManifest/v2 with source tree, fragments, relations, hypotheses, and vectors."""
        return _json(service.model_get_analysis(model_revision_id))

    @mcp.tool()
    def learning_start_session(model_revision_id: str) -> str:
        """Start an auditable engineer-learning session attached to one model revision."""
        return _json(service.learning_start_session(model_revision_id))

    @mcp.tool()
    def learning_next_targets(session_id: str) -> str:
        """Return at most five deterministic high-value targets for the current Codex agent to ask about."""
        return _json(service.learning_next_targets(session_id))

    @mcp.tool()
    def learning_record_exchange(
        session_id: str,
        question_ids_json: str,
        engineer_text: str,
        agent_interpretation_json: str = "{}",
    ) -> str:
        """Append the exact engineer answer separately from the current Codex agent's interpretation."""
        question_ids = _array(question_ids_json, "question_ids_json")
        if not all(isinstance(item, str) for item in question_ids):
            raise ValueError("question_ids_json must contain only ids")
        return _json(
            service.learning_record_exchange(
                session_id=session_id,
                question_ids=question_ids,
                engineer_text=engineer_text,
                agent_interpretation=_object(agent_interpretation_json, "agent_interpretation_json"),
            )
        )

    @mcp.tool()
    def learning_defer_targets(session_id: str, question_ids_json: str, reason: str) -> str:
        """Defer open targets so they are not repeated again in the same learning session."""
        question_ids = _array(question_ids_json, "question_ids_json")
        if not all(isinstance(item, str) for item in question_ids):
            raise ValueError("question_ids_json must contain only ids")
        return _json(service.learning_defer_targets(session_id, question_ids, reason))

    @mcp.tool()
    def knowledge_propose_assertions(session_id: str, proposals_json: str) -> str:
        """Validate and stage atomic assertions produced by the current Codex task; never auto-approves them."""
        proposals = _array(proposals_json, "proposals_json")
        if not all(isinstance(item, dict) for item in proposals):
            raise ValueError("proposals_json must contain assertion objects")
        return _json(service.knowledge_propose_assertions(session_id, proposals))

    @mcp.tool()
    def knowledge_review(
        assertion_id: str,
        decision: str,
        reviewer_text: str,
        corrected_object_value_json: str = "",
    ) -> str:
        """Approve, modify, reject, or supersede an assertion under the risk-based review policy."""
        corrected = None if not corrected_object_value_json else json.loads(corrected_object_value_json)
        return _json(service.knowledge_review(assertion_id, decision, reviewer_text, corrected))

    @mcp.tool()
    def knowledge_search(
        query: str,
        organization_id: str,
        design_group_id: str,
        requested_family_id: str = "",
        model_revision_id: str = "",
        explicit_family_authorization: bool = False,
        limit: int = 10,
    ) -> str:
        """Search approved exact aliases, part numbers, full text, and trigrams under strict scope authorization."""
        return _json(
            service.knowledge_search(
                query=query,
                organization_id=organization_id,
                design_group_id=design_group_id,
                requested_family_id=requested_family_id or None,
                model_revision_id=model_revision_id or None,
                explicit_family_authorization=explicit_family_authorization,
                limit=limit,
            )
        )

    @mcp.tool()
    def model_identity_confirm(
        model_revision_id: str,
        family_id: str,
        canonical_name: str,
        approved_assertion_id: str,
        confirmation: str,
        aliases_json: str = "[]",
    ) -> str:
        """Attach an engineer-confirmed product identity after its model-scoped assertion is approved."""
        aliases = _array(aliases_json, "aliases_json")
        if not all(isinstance(item, str) for item in aliases):
            raise ValueError("aliases_json must contain only strings")
        return _json(
            service.model_identity_confirm(
                model_revision_id=model_revision_id,
                family_id=family_id,
                canonical_name=canonical_name,
                aliases=aliases,
                approved_assertion_id=approved_assertion_id,
                confirmation=confirmation,
            )
        )

    @mcp.tool()
    def family_compare_models(family_id: str) -> str:
        """Compute deterministic cross-model statistics and enforce the three-model generalization gate."""
        return _json(service.family_compare_models(family_id))

    @mcp.tool()
    def subfamily_propose(
        subfamily_id: str,
        family_id: str,
        canonical_name: str,
        model_revision_ids_json: str,
        evidence_json: str,
        confirmation: str,
        aliases_json: str = "[]",
    ) -> str:
        """Propose, but never auto-create, a subfamily from compared models and engineer evidence."""
        model_ids = _array(model_revision_ids_json, "model_revision_ids_json")
        aliases = _array(aliases_json, "aliases_json")
        evidence = _array(evidence_json, "evidence_json")
        if not all(isinstance(item, str) for item in model_ids + aliases):
            raise ValueError("model ids and aliases must be strings")
        if not all(isinstance(item, dict) for item in evidence):
            raise ValueError("evidence_json must contain objects")
        return _json(
            service.subfamily_propose(
                subfamily_id=subfamily_id,
                family_id=family_id,
                canonical_name=canonical_name,
                aliases=aliases,
                model_revision_ids=model_ids,
                evidence=evidence,
                confirmation=confirmation,
            )
        )

    @mcp.tool()
    def subfamily_review(subfamily_id: str, decision: str, confirmation: str) -> str:
        """Approve or reject a proposed subfamily and its model memberships."""
        return _json(service.subfamily_review(subfamily_id, decision, confirmation))

    @mcp.tool()
    def subfamily_get(family_id: str) -> str:
        """List proposed, approved, and rejected subfamilies with auditable model assignments."""
        return _json(service.subfamily_get(family_id))

    @mcp.tool()
    def family_profile_propose(
        family_id: str,
        profile_json: str,
        evidence_json: str,
        source_kind: str = "statistical",
    ) -> str:
        """Stage a current-Codex-produced family profile; statistical profiles require three distinct models."""
        evidence = _array(evidence_json, "evidence_json")
        if not all(isinstance(item, dict) for item in evidence):
            raise ValueError("evidence_json must contain evidence objects")
        return _json(
            service.family_profile_propose(
                family_id,
                _object(profile_json, "profile_json"),
                evidence,
                source_kind,
            )
        )

    @mcp.tool()
    def family_profile_review(profile_id: str, decision: str, confirmation: str) -> str:
        """Approve or reject a proposed family profile as the configured family owner."""
        return _json(service.family_profile_review(profile_id, decision, confirmation))

    @mcp.tool()
    def family_profile_get(family_id: str) -> str:
        """Get the latest family profile revision, including evidence and approval state."""
        return _json(service.family_profile_get(family_id))

    @mcp.tool()
    def design_context_build(
        organization_id: str,
        design_group_id: str,
        requested_family_id: str = "",
        model_revision_id: str = "",
        explicit_family_authorization: bool = False,
        confirmed_in_current_session: bool = False,
        user_requested_analogy: bool = False,
        design_features_json: str = "{}",
        lesson_query: str = "",
    ) -> str:
        """Build DesignContext/v2; specialized knowledge is empty unless the current design has family authority."""
        configured_organization = str(service.bootstrap_config["organization_id"])
        if organization_id != configured_organization:
            raise PermissionError("organization_id does not match the configured organization")
        return _json(
            service.design_context_build(
                organization_id=organization_id,
                design_group_id=design_group_id,
                requested_family_id=requested_family_id or None,
                model_revision_id=model_revision_id or None,
                explicit_family_authorization=explicit_family_authorization,
                confirmed_in_current_session=confirmed_in_current_session,
                user_requested_analogy=user_requested_analogy,
                design_features=_design_features(design_features_json),
                lesson_query=lesson_query,
            )
        )

    @mcp.tool()
    def design_lesson_review_context(working_copy_id: str) -> str:
        """Get the required post-delivery context before Codex decides whether material, generalizable experience exists."""
        require_safe_id(working_copy_id, "working_copy_id")
        return _json(service.design_lesson_review_context(working_copy_id))

    @mcp.tool()
    def design_lesson_review_prepare(
        working_copy_id: str,
        package_json: str,
        evidence_items_json: str,
        supersedes_review_id: str = "",
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Prepare one immutable review card from a material, generalizable Codex summary; leave predecessor empty for a new review."""
        require_safe_id(working_copy_id, "working_copy_id")
        package = _required_object(package_json, "package_json")
        evidence_items = _required_array(evidence_items_json, "evidence_items_json")
        if not all(isinstance(item, dict) for item in evidence_items):
            raise ValueError("evidence_items_json must contain evidence objects")
        predecessor = supersedes_review_id.strip()
        if predecessor:
            require_safe_id(predecessor, "supersedes_review_id")
        return _json(
            service.design_lesson_review_prepare(
                working_copy_id,
                package,
                evidence_items,
                predecessor or None,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_review_approve(
        review_id: str,
        reviewer_text: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Approve the entire immutable review card as one batch decision using exactly `批准设计经验 <review_id>`; no digest is supplied."""
        require_safe_id(review_id, "review_id")
        if not isinstance(reviewer_text, str) or not reviewer_text.strip():
            raise ValueError("reviewer_text is required")
        expected_confirmation = f"批准设计经验 {review_id}"
        if confirmation != expected_confirmation:
            raise ValueError(
                "confirmation must use canonical confirmation: "
                + expected_confirmation
            )
        return _json(
            service.design_lesson_review_approve(
                review_id=review_id,
                reviewer_text=reviewer_text,
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_review_reject(
        review_id: str,
        reviewer_text: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Reject the entire immutable review card using exactly `拒绝设计经验 <review_id>` with a nonblank reviewer explanation."""
        require_safe_id(review_id, "review_id")
        if not isinstance(reviewer_text, str) or not reviewer_text.strip():
            raise ValueError("reviewer_text is required")
        expected_confirmation = f"拒绝设计经验 {review_id}"
        if confirmation != expected_confirmation:
            raise ValueError(
                "confirmation must use canonical confirmation: "
                + expected_confirmation
            )
        return _json(
            service.design_lesson_review_reject(
                review_id=review_id,
                reviewer_text=reviewer_text,
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_review_status(review_id: str, retry: bool = True) -> str:
        """Check review completion; retry performs one bounded internal projection/retrieval attempt and requires no engineer confirmation."""
        require_safe_id(review_id, "review_id")
        if type(retry) is not bool:
            raise ValueError("retry must be a boolean")
        return _json(service.design_lesson_review_status(review_id, retry=retry))

    @mcp.tool()
    def design_lesson_review_publish(
        review_id: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Publish the displayed immutable Review Card using exactly `确认发布设计经验`; internal IDs are supplied by the agent."""
        require_safe_id(review_id, "review_id")
        if not isinstance(confirmation, str) or confirmation.strip() != "确认发布设计经验":
            raise ValueError("confirmation must be exactly: 确认发布设计经验")
        return _json(
            service.design_lesson_review_publish(
                review_id=review_id,
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_review_no_publish(
        review_id: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Finalize the displayed screening card using exactly `确认无可发布设计经验` without creating shared knowledge."""
        require_safe_id(review_id, "review_id")
        if (
            not isinstance(confirmation, str)
            or confirmation.strip() != "确认无可发布设计经验"
        ):
            raise ValueError("confirmation must be exactly: 确认无可发布设计经验")
        return _json(
            service.design_lesson_review_no_publish(
                review_id=review_id,
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_stage(
        package_json: str,
        evidence_paths_json: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Stage a local immutable lesson review package; staging makes no PostgreSQL write."""
        package = _object(package_json, "package_json")
        if package.get("status") == "approved":
            raise ValueError("status is assigned by approval and cannot be supplied by the caller")
        evidence_items = _array(evidence_paths_json, "evidence_paths_json")
        if not all(isinstance(item, dict) for item in evidence_items):
            raise ValueError("evidence_paths_json must contain evidence objects")
        required_fields = ("path", "role", "media_type")
        for item in evidence_items:
            if not all(isinstance(item.get(field), str) for field in required_fields):
                raise ValueError("evidence paths, roles, and media types must be strings")
        return _json(
            service.design_lesson_stage(
                package,
                evidence_items,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_staged_get(
        lesson_id: str, working_copy_id: str = ""
    ) -> str:
        """Get one local-only staged lesson package by its opaque lesson id."""
        return _json(
            service.design_lesson_staged_get(
                lesson_id,
                working_copy_id=working_copy_id or None,
            )
        )

    @mcp.tool()
    def design_lesson_approve(
        lesson_id: str,
        expected_package_sha256: str,
        reviewer_text: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Approve a verified staged package only after service-level Chinese confirmation checks."""
        return _json(
            service.design_lesson_approve(
                lesson_id=lesson_id,
                expected_package_sha256=expected_package_sha256,
                reviewer_text=reviewer_text,
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_search(
        query: str,
        organization_id: str,
        design_features_json: str = "{}",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """Search one bounded page of approved lessons; pass next_cursor to continue safely."""
        if limit not in range(1, 51):
            raise ValueError("limit must be between 1 and 50")
        features = _design_features(design_features_json)
        if organization_id != service.bootstrap_config["organization_id"]:
            raise ValueError("organization_id does not match the configured organization")
        page = service.design_lesson_search_page(
            query=query,
            limit=limit,
            cursor=cursor.strip() or None,
        )
        return _json(
            _safe_design_lesson_search_response(
                page["items"],
                features=features,
                query=query,
                limit=limit,
                next_cursor=page.get("next_cursor"),
            )
        )

    @mcp.tool()
    def design_lesson_get(lesson_id: str) -> str:
        """Get one approved lesson through the redacted safe renderer; source-family details are omitted."""
        return _json(service.design_lesson_get(lesson_id))

    @mcp.tool()
    def design_lesson_audit_get(lesson_id: str, confirmation: str) -> str:
        """Get owner-only lesson audit history and immutable evidence after exact `审计 <lesson_id>` confirmation."""
        return _json(service.design_lesson_audit_get(lesson_id, confirmation))

    @mcp.tool()
    def design_lesson_supersede(
        lesson_id: str,
        replacement_lesson_id: str,
        expected_package_sha256: str,
        reviewer_text: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Replace an approved lesson with a separately staged package after explicit confirmation."""
        return _json(
            service.design_lesson_supersede(
                lesson_id=lesson_id,
                replacement_lesson_id=replacement_lesson_id,
                expected_package_sha256=expected_package_sha256,
                reviewer_text=reviewer_text,
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_lesson_revoke(
        lesson_id: str,
        reason: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Revoke an approved lesson with an auditable reason and Chinese confirmation."""
        return _json(
            service.design_lesson_revoke(
                lesson_id=lesson_id,
                reason=reason,
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_working_copy_create(
        source_path: str,
        organization_id: str,
        design_group_id: str,
        family_id: str = "",
        model_revision_id: str = "",
        compatibility_request_id: str = "",
    ) -> str:
        """Create a new FCStd working copy without modifying the STEP/FCStd source."""
        return _job_json(lambda:
            service.design_working_copy_create(
                source_path=source_path,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id or None,
                model_revision_id=model_revision_id or None,
                compatibility_request_id=compatibility_request_id or None,
            )
        )

    @mcp.tool()
    def design_job_working_copy_create(
        job_id: str,
        expected_job_revision: int,
        source_path: str,
        organization_id: str,
        design_group_id: str,
        family_id: str = "",
        model_revision_id: str = "",
    ) -> str:
        """Create a Job-bound immutable source snapshot and FCStd working copy."""
        return _job_json(lambda:
            service.design_job_working_copy_create(
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                source_path=source_path,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id or None,
                model_revision_id=model_revision_id or None,
            )
        )

    @mcp.tool()
    def design_new_working_copy_create(
        organization_id: str,
        design_group_id: str,
        family_id: str = "",
        explicit_family_authorization: bool = False,
        compatibility_request_id: str = "",
    ) -> str:
        """Create a neutral empty FCStd working copy; specialized knowledge is not applied without family authorization."""
        return _job_json(lambda:
            service.design_new_working_copy_create(
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id or None,
                explicit_family_authorization=explicit_family_authorization,
                compatibility_request_id=compatibility_request_id or None,
            )
        )

    @mcp.tool()
    def design_job_new_working_copy_create(
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str = "",
        explicit_family_authorization: bool = False,
    ) -> str:
        """Create one empty FCStd directly inside an active mechanical-design Job."""
        return _job_json(lambda:
            service.design_job_new_working_copy_create(
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id or None,
                explicit_family_authorization=explicit_family_authorization,
            )
        )

    @mcp.tool()
    def design_change_record(
        working_copy_id: str,
        change_phase: str,
        changes_json: str,
        knowledge_used_json: str,
        rationale: str,
        approval_envelope_draft_json: str = "",
        semantic_impact_json: str = "",
    ) -> str:
        """Record a structured proposed CAD change and the exact approved knowledge it used."""
        changes = _array(changes_json, "changes_json")
        knowledge_used = _array(knowledge_used_json, "knowledge_used_json")
        if not all(isinstance(item, dict) for item in changes) or not all(isinstance(item, str) for item in knowledge_used):
            raise ValueError("invalid changes or knowledge_used payload")
        approval_envelope_draft = (
            _object(
                approval_envelope_draft_json,
                "approval_envelope_draft_json",
            )
            if approval_envelope_draft_json.strip()
            else None
        )
        if selected_profile == "design" and approval_envelope_draft is not None:
            design_intent = approval_envelope_draft.get("design_intent")
            if not isinstance(design_intent, dict) or not isinstance(
                design_intent.get("engineering_scope"), dict
            ):
                raise ValueError(
                    "design profile approval envelope requires "
                    "design_intent.engineering_scope"
                )
            validate_engineering_scope(design_intent["engineering_scope"])
        return _json(
            service.design_change_record(
                working_copy_id=working_copy_id,
                change_phase=change_phase,
                changes=changes,
                knowledge_used=knowledge_used,
                rationale=rationale,
                approval_envelope_draft=approval_envelope_draft,
                semantic_impact=(
                    _object(semantic_impact_json, "semantic_impact_json")
                    if semantic_impact_json.strip()
                    else None
                ),
            )
        )

    @mcp.tool()
    def design_knowledge_retrieve(
        working_copy_id: str,
        query: str,
        design_features_json: str = "{}",
        used_knowledge_ids_json: str = "[]",
        non_use_reason: str = "",
    ) -> str:
        """Build scoped DesignContext and record its retrieval receipt before CAD changes."""
        used = _array(used_knowledge_ids_json, "used_knowledge_ids_json")
        if not all(isinstance(item, str) for item in used):
            raise ValueError("used knowledge IDs must be strings")
        return _json(
            service.design_knowledge_retrieve(
                working_copy_id=working_copy_id,
                query=query,
                design_features=_design_features(design_features_json),
                used_knowledge_ids=used,
                non_use_reason=non_use_reason,
            )
        )

    @mcp.tool()
    def product_family_inventory() -> str:
        """List PostgreSQL-authoritative Product Family discovery metadata."""
        return _json(service.product_family_inventory())

    @mcp.tool()
    def product_family_match(
        query: str,
        design_features_json: str = "{}",
        job_id: str = "",
        working_copy_id: str = "",
        source_model_revision_id: str = "",
        explicit_family_id: str = "",
    ) -> str:
        """Match a design request without authorizing semantic candidates."""
        return _json(
            service.product_family_match(
                query=query,
                design_features=_design_features(design_features_json),
                job_id=job_id or None,
                working_copy_id=working_copy_id or None,
                source_model_revision_id=source_model_revision_id or None,
                explicit_family_id=explicit_family_id or None,
            )
        )

    @mcp.tool()
    def design_retrieval_receipt_get(working_copy_id: str) -> str:
        """Return the latest knowledge-retrieval receipt for a working copy."""
        return _json(service.design_retrieval_receipt_get(working_copy_id))

    @mcp.tool()
    def design_change_review(
        change_set_id: str, decision: str, review_text: str, confirmation: str
    ) -> str:
        """Approve a design intent or request a revised proposal using simple user confirmation."""
        return _json(service.design_change_review(change_set_id, decision, review_text, confirmation))

    @mcp.tool()
    def design_approval_envelope_get(working_copy_id: str) -> str:
        """Return the active auditable design-intent approval envelope."""
        return _json(service.design_approval_envelope_get(working_copy_id))

    @mcp.tool()
    def design_change_audit_history(change_set_id: str) -> str:
        """Return append-only approval and autonomous-change audit events."""
        return _json(service.design_change_audit_history(change_set_id))

    @mcp.tool()
    def design_change_mutation_authorize(change_set_id: str) -> str:
        """Fail closed unless this exact change is authorized by the active approval envelope."""
        return _json(service.design_change_mutation_authorize(change_set_id))

    @mcp.tool()
    def design_change_applied(change_set_id: str, confirmation: str) -> str:
        """Record the post-edit FCStd hash after an approved change is applied in FreeCAD."""
        return _json(service.design_change_applied(change_set_id, confirmation))

    @mcp.tool()
    def design_change_close(
        change_set_id: str,
        disposition: str,
        reason: str,
        confirmation: str,
        successor_change_set_id: str = "",
    ) -> str:
        """Close an unapplied proposed/approved change as superseded or cancelled without rewriting history."""
        return _json(
            service.design_change_close(
                change_set_id=change_set_id,
                disposition=disposition,
                reason=reason,
                confirmation=confirmation,
                successor_change_set_id=successor_change_set_id or None,
            )
        )

    @mcp.tool()
    def design_confirmation_record(
        working_copy_id: str,
        lesson_summary_json: str,
        confirmation: str,
        job_id: str = "",
        expected_job_revision: int = -1,
    ) -> str:
        """Record the mandatory lesson summary immediately after explicit model-design confirmation."""
        return _json(
            service.design_confirmation_record(
                working_copy_id=working_copy_id,
                lesson_summary=_object(lesson_summary_json, "lesson_summary_json"),
                confirmation=confirmation,
                **_optional_job_binding(job_id, expected_job_revision),
            )
        )

    @mcp.tool()
    def design_validation_record(
        working_copy_id: str,
        status: str,
        checks_json: str,
        change_set_id: str = "",
        report_path: str = "",
        validation_kind: str = "geometry_model",
    ) -> str:
        """Record mandatory FreeCAD/model-validation results; failed checks cannot be recorded as passed."""
        checks = _array(checks_json, "checks_json")
        if not all(isinstance(item, dict) for item in checks):
            raise ValueError("checks_json must contain validation check objects")
        return _json(
            service.design_validation_record(
                working_copy_id=working_copy_id,
                change_set_id=change_set_id or None,
                status=status,
                checks=checks,
                report_path=report_path,
                validation_kind=validation_kind,
            )
        )

    @mcp.tool()
    def design_assembly_completeness_validate(
        working_copy_id: str,
        manifest_json: str,
        change_set_id: str = "",
    ) -> str:
        """Run the mandatory BOM, joint, placeholder-interface, load-path, and motion completeness gate."""
        return _json(
            service.design_assembly_completeness_validate(
                working_copy_id=working_copy_id,
                change_set_id=change_set_id or None,
                manifest=_object(manifest_json, "manifest_json"),
            )
        )

    @mcp.tool()
    def standard_part_providers_get(category: str = "") -> str:
        """List deterministic standard-part download channels in configured trust order."""
        return _json(bootstrap_runtime.standard_part_providers(category))

    @mcp.tool()
    def standard_part_sources_status() -> str:
        """Inspect the workspace's portable standard-part catalog binding."""
        return _json(bootstrap_runtime.standard_part_sources_status())

    @mcp.tool()
    def standard_part_catalog_enable(root_path: str) -> str:
        """Bind an existing external standard-part catalog directory."""
        return _json(bootstrap_runtime.standard_part_catalog_enable(root_path))

    @mcp.tool()
    def standard_part_catalog_disable() -> str:
        """Disable the workspace's standard-part catalog binding."""
        return _json(bootstrap_runtime.standard_part_catalog_disable())

    @mcp.tool()
    def standard_part_download_register(
        provider_id: str,
        file_path: str,
        part_number: str,
        standard: str,
        nominal_size: str,
        source_url: str,
        metadata_json: str = "{}",
        approval_reference: str = "",
        validation_report_path: str = "",
        working_copy_id: str = "",
    ) -> str:
        """Register a downloaded catalog part with exact provider, source URL, and checksum."""
        return _json(service.standard_part_download_register(
            provider_id=provider_id,
            file_path=file_path,
            part_number=part_number,
            standard=standard,
            nominal_size=nominal_size,
            source_url=source_url,
            metadata=_object(metadata_json, "metadata_json"),
            approval_reference=approval_reference,
            validation_report_path=validation_report_path,
            working_copy_id=working_copy_id,
        ))

    @mcp.tool()
    def design_delivery_approve(working_copy_id: str, confirmation: str) -> str:
        """Approve delivery only after the latest mandatory validation report passed."""
        return _json(service.design_delivery_approve(working_copy_id, confirmation))

    @mcp.tool()
    def projection_sync(limit: int = 100) -> str:
        """Replay pending PostgreSQL outbox events into the rebuildable Neo4j projection."""
        return _json(service.projection_sync(limit))

    @mcp.tool()
    def projection_rebuild(confirmation: str) -> str:
        """Replace only this module's Neo4j projection from authoritative PostgreSQL rows."""
        return _json(service.projection_rebuild(confirmation))

    registrar.assert_complete()
    return mcp_server


def build_server() -> FastMCP:
    """Build the MCP server for existing launchers that use the legacy factory name."""
    return create_mcp()


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
