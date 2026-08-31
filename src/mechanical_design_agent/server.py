from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .bootstrap_diagnostics import DiagnosticGateError
from .bootstrap_runtime import BootstrapRuntime
from .design_knowledge import DesignKnowledgeService
from .design_lesson_workflow import DesignLessonWorkflow
from .design_session import DesignSessionService
from .knowledge_repository import KnowledgeRepository, KnowledgeScope
from .knowledge_service import KnowledgeService
from .projection import Neo4jProjection
from .standard_parts import StandardPartRegistry
from .tool_profiles import ProfiledToolRegistrar, resolve_tool_profile


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


def _loads(value: str, label: str, expected: type) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"{label} does not allow {constant}")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {exc.msg}") from None

    def reject_non_finite(item: Any) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{label} does not allow non-finite numbers")
        if isinstance(item, dict):
            for nested in item.values():
                reject_non_finite(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_non_finite(nested)

    reject_non_finite(parsed)
    if not isinstance(parsed, expected):
        kind = "object" if expected is dict else "array"
        raise ValueError(f"{label} must be a JSON {kind}")
    return parsed


def _object(value: str, label: str) -> dict[str, Any]:
    return _loads(value or "{}", label, dict)


def _array(value: str, label: str) -> list[Any]:
    return _loads(value or "[]", label, list)


def _tool_call(call: Callable[[], object]) -> str:
    try:
        return _json(call())
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            _json(
                {
                    "schema_version": "MechanicalDesignToolError/v1",
                    "status": "blocked",
                    "code": type(exc).__name__,
                    "message": str(exc),
                }
            )
        ) from None


def create_mcp(
    *,
    runtime: BootstrapRuntime | None = None,
    design_service: Any | None = None,
    design_service_factory: Callable[[object], Any] | None = None,
    design_knowledge_service: Any | None = None,
    lesson_workflow: Any | None = None,
    knowledge_service: Any | None = None,
    knowledge_service_factory: Callable[[object], Any] | None = None,
    standard_part_service: Any | None = None,
    standard_part_service_factory: Callable[[object], Any] | None = None,
    tool_profile: str | None = None,
) -> FastMCP:
    bootstrap_runtime = runtime or BootstrapRuntime.from_process(
        cwd=Path.cwd(), environ=os.environ
    )
    selected_profile = resolve_tool_profile(tool_profile)
    server = FastMCP("AI Mechanical 3D CAD Design Agent")
    registrar = ProfiledToolRegistrar(server, selected_profile)
    local_design = design_service
    local_knowledge = design_knowledge_service
    local_lessons = lesson_workflow
    local_admin = knowledge_service
    local_parts = standard_part_service
    lock = Lock()

    def get_design() -> Any:
        nonlocal local_design
        if local_design is not None:
            return local_design
        with lock:
            if local_design is None:
                try:
                    settings = bootstrap_runtime.design_settings()
                    factory = design_service_factory or DesignSessionService
                    local_design = factory(settings)
                except DiagnosticGateError as exc:
                    raise ToolError(_json(exc.response)) from None
        return local_design

    def load_context(query: str, features: dict[str, object]) -> dict[str, object]:
        scope = bootstrap_runtime.design_knowledge_scope()
        service = get_admin()
        return service.design_context_build(
            organization_id=scope["organization_id"],
            design_group_id=scope["design_group_id"],
            requested_family_id=features.pop("requested_family_id", None),
            design_features=features,
            lesson_query=query,
        )

    def get_design_knowledge() -> Any:
        nonlocal local_knowledge
        design = get_design()
        if local_knowledge is None:
            with lock:
                if local_knowledge is None:
                    local_knowledge = DesignKnowledgeService(
                        design, load_context
                    )
        return local_knowledge

    def get_lessons() -> Any:
        nonlocal local_lessons
        design = get_design()
        if local_lessons is None:
            with lock:
                if local_lessons is None:
                    local_lessons = DesignLessonWorkflow(design)
        return local_lessons

    def get_admin() -> Any:
        nonlocal local_admin
        if local_admin is not None:
            return local_admin
        with lock:
            if local_admin is None:
                settings = bootstrap_runtime.knowledge_settings()
                if knowledge_service_factory is not None:
                    local_admin = knowledge_service_factory(settings)
                else:
                    repository = KnowledgeRepository(
                        settings.database_url,
                        KnowledgeScope(
                            settings.organization_id, settings.design_group_id
                        ),
                    )
                    projection = Neo4jProjection(
                        settings.neo4j_uri,
                        settings.neo4j_user,
                        settings.neo4j_password,
                    )
                    local_admin = KnowledgeService(
                        repository, projection, settings.workspace
                    )
        return local_admin

    def get_parts() -> Any:
        nonlocal local_parts
        if local_parts is not None:
            return local_parts
        with lock:
            if local_parts is None:
                settings = bootstrap_runtime.standard_part_settings()
                factory = standard_part_service_factory or StandardPartRegistry
                local_parts = factory(settings)
        return local_parts

    @registrar.tool()
    def design_system_status() -> str:
        """Inspect local design, FreeCAD, knowledge, and provider readiness."""
        return _json(bootstrap_runtime.status())

    @registrar.tool()
    def design_start(
        design_id: str,
        title: str,
        model_classification: str,
        requirements_json: str,
        proposal_summary: str,
        approval_text: str,
        source_path: str = "",
    ) -> str:
        """Start or resume a design after natural-language direction approval."""
        return _tool_call(
            lambda: get_design().start(
                design_id=design_id,
                title=title,
                model_classification=model_classification,
                requirements=_object(requirements_json, "requirements_json"),
                proposal_summary=proposal_summary,
                approval_text=approval_text,
                source_path=source_path or None,
            )
        )

    @registrar.tool()
    def design_status(design_id: str) -> str:
        """Read current model, validation, confirmation, and lesson state."""
        return _tool_call(lambda: get_design().get(design_id))

    @registrar.tool()
    def design_knowledge_retrieve(
        design_id: str,
        query: str,
        features_json: str = "{}",
        used_knowledge_ids_json: str = "[]",
        required: bool = False,
    ) -> str:
        """Retrieve applicable knowledge without making retrieval a CAD gate."""
        used_ids = _array(used_knowledge_ids_json, "used_knowledge_ids_json")
        if not all(isinstance(item, str) for item in used_ids):
            raise ValueError("used knowledge IDs must be strings")
        return _tool_call(
            lambda: get_design_knowledge().retrieve(
                design_id=design_id,
                query=query,
                features=_object(features_json, "features_json"),
                used_ids=used_ids,
                required=required,
            )
        )

    @registrar.tool()
    def design_record_result(
        design_id: str,
        model_path: str,
        validation_report_path: str,
        evidence_paths_json: str,
    ) -> str:
        """Bind the exact FCStd hash to passed JSON, Markdown, and PNG evidence."""
        evidence = _array(evidence_paths_json, "evidence_paths_json")
        if not all(isinstance(item, str) for item in evidence):
            raise ValueError("evidence_paths_json must contain strings")
        return _tool_call(
            lambda: get_design().record_result(
                design_id=design_id,
                model_path=model_path,
                validation_report_path=validation_report_path,
                evidence_paths=evidence,
            )
        )

    @registrar.tool()
    def design_confirm(
        design_id: str,
        confirmation_text: str,
        lesson_candidates_json: str = "[]",
    ) -> str:
        """Confirm the final model and immediately evaluate Design Lessons."""
        candidates = _array(lesson_candidates_json, "lesson_candidates_json")
        return _tool_call(
            lambda: get_lessons().confirm(
                design_id=design_id,
                confirmation_text=confirmation_text,
                candidates=candidates,
            )
        )

    @registrar.tool()
    def design_lesson_decide(
        design_id: str,
        decision_text: str,
        selected_lesson_numbers_json: str = "[]",
    ) -> str:
        """Approve all or selected review-card lessons, or decline publication."""
        return _tool_call(
            lambda: get_lessons().decide(
                design_id=design_id,
                decision_text=decision_text,
                publisher=get_admin(),
                selected_lesson_numbers=_array(
                    selected_lesson_numbers_json, "selected_lesson_numbers_json"
                ),
            )
        )

    @registrar.tool()
    def standard_part_providers_get(category: str = "") -> str:
        """List standard-part sources in configured trust order."""
        return _tool_call(lambda: bootstrap_runtime.standard_part_providers(category))

    @registrar.tool()
    def standard_part_sources_status() -> str:
        """Inspect the configured standard-part catalog binding."""
        return _tool_call(bootstrap_runtime.standard_part_sources_status)

    @registrar.tool()
    def standard_part_download_register(
        provider_id: str,
        file_path: str,
        part_number: str,
        standard: str,
        nominal_size: str,
        source_url: str,
        metadata_json: str = "{}",
        validation_report_path: str = "",
    ) -> str:
        """Register a validated downloaded standard part with full provenance."""
        return _tool_call(
            lambda: get_parts().register_download(
                provider_id=provider_id,
                file_path=file_path,
                part_number=part_number,
                standard=standard,
                nominal_size=nominal_size,
                source_url=source_url,
                metadata=_object(metadata_json, "metadata_json"),
                validation_report_path=validation_report_path,
            )
        )

    @registrar.tool()
    def product_family_onboarding_start(request_json: str) -> str:
        """Start independent Product Family Knowledge onboarding."""
        return _tool_call(
            lambda: get_admin().product_family_onboarding_start(
                **_object(request_json, "request_json")
            )
        )

    @registrar.tool()
    def product_family_onboarding_analyze(
        onboarding_id: str, analysis_json: str = "{\"assertions\":[]}"
    ) -> str:
        """Analyze an onboarding family workspace."""
        return _tool_call(
            lambda: get_admin().product_family_onboarding_analyze(
                onboarding_id=onboarding_id,
                analysis=_object(analysis_json, "analysis_json"),
            )
        )

    @registrar.tool()
    def product_family_onboarding_review(
        onboarding_id: str, decision_text: str, review_json: str = "{}"
    ) -> str:
        """Review Product Family Knowledge using natural-language semantics."""
        return _tool_call(
            lambda: get_admin().product_family_onboarding_review(
                onboarding_id=onboarding_id,
                decision_text=decision_text,
                review=_object(review_json, "review_json"),
            )
        )

    @registrar.tool()
    def product_family_onboarding_publish(onboarding_id: str) -> str:
        """Publish an approved Product Family Knowledge package."""
        return _tool_call(
            lambda: get_admin().product_family_onboarding_publish(
                onboarding_id=onboarding_id
            )
        )

    @registrar.tool()
    def product_family_onboarding_status(onboarding_id: str) -> str:
        """Inspect Product Family Knowledge onboarding status."""
        return _tool_call(
            lambda: get_admin().product_family_onboarding_status(
                onboarding_id=onboarding_id
            )
        )

    @registrar.tool()
    def knowledge_search(query: str, filters_json: str = "{}") -> str:
        """Search approved Product Family Knowledge and Design Lessons."""
        return _tool_call(
            lambda: get_admin().knowledge_search(
                query=query, filters=_object(filters_json, "filters_json")
            )
        )

    @registrar.tool()
    def knowledge_review(review_id: str, decision_text: str) -> str:
        """Record a natural-language knowledge review decision."""
        return _tool_call(
            lambda: get_admin().knowledge_review(
                review_id=review_id, decision_text=decision_text
            )
        )

    @registrar.tool()
    def design_lesson_search(
        query: str, features_json: str = "{}", limit: int = 20
    ) -> str:
        """Search published Design Lessons with applicability filtering."""
        return _tool_call(
            lambda: get_admin().design_lesson_search(
                query=query,
                features=_object(features_json, "features_json"),
                limit=limit,
            )
        )

    @registrar.tool()
    def design_lesson_get(lesson_id: str) -> str:
        """Read one published Design Lesson."""
        return _tool_call(lambda: get_admin().design_lesson_get(lesson_id=lesson_id))

    @registrar.tool()
    def design_lesson_supersede(
        lesson_id: str, replacement_lesson_id: str, decision_text: str
    ) -> str:
        """Supersede a lesson after a natural-language administrative decision."""
        return _tool_call(
            lambda: get_admin().design_lesson_supersede(
                lesson_id=lesson_id,
                replacement_lesson_id=replacement_lesson_id,
                decision_text=decision_text,
            )
        )

    @registrar.tool()
    def design_lesson_revoke(lesson_id: str, decision_text: str) -> str:
        """Revoke a lesson after a natural-language administrative decision."""
        return _tool_call(
            lambda: get_admin().design_lesson_revoke(
                lesson_id=lesson_id, decision_text=decision_text
            )
        )

    @registrar.tool()
    def projection_sync(limit: int = 100) -> str:
        """Replay knowledge outbox events into Neo4j."""
        return _tool_call(lambda: get_admin().projection_sync(limit=limit))

    @registrar.tool()
    def projection_rebuild(decision_text: str) -> str:
        """Rebuild the Neo4j knowledge projection after explicit approval."""
        return _tool_call(
            lambda: get_admin().projection_rebuild(decision_text=decision_text)
        )

    registrar.assert_complete()
    return server


def build_server() -> FastMCP:
    return create_mcp()


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
