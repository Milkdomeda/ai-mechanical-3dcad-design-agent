from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError
import pytest

from mechanical_design_agent import server
from mechanical_design_agent.bootstrap_diagnostics import CapabilityRequest
from mechanical_design_agent.bootstrap_runtime import (
    BootstrapRuntime,
    DoctorProbes,
    ProbeResult,
)
from mechanical_design_agent.server import (
    SERVICE_METHOD_CAPABILITIES,
    _LazyServiceProxy,
    create_mcp,
)
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


def clear_bootstrap_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MECH_DESIGN_WORKSPACE",
        "MECH_DESIGN_ENV_FILE",
        "MECH_DESIGN_ACTOR_ID",
        "MECH_DESIGN_DATABASE_URL",
        "MECH_DESIGN_NEO4J_URI",
        "MECH_DESIGN_NEO4J_USER",
        "MECH_DESIGN_NEO4J_PASSWORD",
        "MECH_DESIGN_FREECADCMD",
        "MECH_DESIGN_ARTIFACT_ROOT",
        "MECH_DESIGN_PRODUCT_FAMILY_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def tool(mcp: object, name: str):
    return mcp._tool_manager._tools[name].fn


def request(
    capability: str,
    *additional_components: str,
) -> CapabilityRequest:
    return CapabilityRequest(capability, tuple(additional_components))


EXPECTED_SERVICE_METHOD_CAPABILITIES = {
    "bootstrap_config": request("design_knowledge", "product_family"),
    "family_bootstrap_get": request("family_create_or_manage", "product_family"),
    "family_bootstrap_update": request("family_create_or_manage", "product_family"),
    "design_group_register": request("family_create_or_manage"),
    "family_create": request("family_create_or_manage"),
    "family_folder_confirm": request("family_create_or_manage", "product_family"),
    "family_compare_models": request("family_create_or_manage", "product_family"),
    "subfamily_propose": request("family_create_or_manage", "product_family"),
    "subfamily_review": request("family_create_or_manage", "product_family"),
    "subfamily_get": request("family_create_or_manage", "product_family"),
    "family_profile_propose": request("family_create_or_manage", "product_family"),
    "family_profile_review": request("family_create_or_manage", "product_family"),
    "family_profile_get": request("family_create_or_manage", "product_family"),
    "library_register": request("library_ingest"),
    "library_scan": request("library_ingest"),
    "library_ingest_changes": request("library_ingest"),
    "design_job_create": request("design_job_workspace"),
    "design_job_list": request("design_job_workspace"),
    "design_job_get": request("design_job_workspace"),
    "design_job_resolve": request("design_job_workspace"),
    "design_job_close": request("design_job_workspace"),
    "design_job_reopen": request("design_job_workspace"),
    "job_get": request("library_ingest"),
    "model_get_analysis": request("library_ingest"),
    "model_identity_confirm": request("library_ingest"),
    "evidence_artifact_register": request("artifact_registration"),
    "learning_start_session": request("design_knowledge", "product_family"),
    "learning_next_targets": request("design_knowledge", "product_family"),
    "learning_record_exchange": request("design_knowledge", "product_family"),
    "learning_defer_targets": request("design_knowledge", "product_family"),
    "knowledge_propose_assertions": request("design_knowledge", "product_family"),
    "knowledge_review": request("design_knowledge", "product_family"),
    "knowledge_search": request("design_knowledge", "product_family"),
    "design_context_build": request("design_knowledge", "product_family"),
    "design_lesson_review_context": request("design_knowledge", "product_family"),
    "design_lesson_review_prepare": request("design_knowledge", "product_family"),
    "design_lesson_review_approve": request("design_knowledge", "product_family"),
    "design_lesson_review_reject": request("design_knowledge", "product_family"),
    "design_lesson_review_status": request("design_knowledge", "product_family"),
    "design_lesson_stage": request("design_knowledge", "product_family"),
    "design_lesson_staged_get": request("design_knowledge", "product_family"),
    "design_lesson_approve": request("design_knowledge", "product_family"),
    "design_lesson_search_page": request("design_knowledge", "product_family"),
    "design_lesson_get": request("design_knowledge", "product_family"),
    "design_lesson_audit_get": request("design_knowledge", "product_family"),
    "design_lesson_supersede": request("design_knowledge", "product_family"),
    "design_lesson_revoke": request("design_knowledge", "product_family"),
    "design_knowledge_retrieve": request("design_knowledge", "product_family"),
    "design_retrieval_receipt_get": request("design_knowledge", "product_family"),
    "design_working_copy_create": request("cad_working_copy", "product_family"),
    "design_new_working_copy_create": request("cad_working_copy", "product_family"),
    "design_change_record": request("cad_working_copy", "product_family"),
    "design_change_review": request("cad_working_copy", "product_family"),
    "design_change_applied": request("cad_working_copy", "product_family"),
    "design_change_close": request("cad_working_copy", "product_family"),
    "design_confirmation_record": request("cad_working_copy", "product_family"),
    "design_delivery_approve": request("cad_working_copy", "product_family"),
    "design_validation_record": request("model_validation", "postgresql"),
    "design_assembly_completeness_validate": request(
        "model_validation",
        "postgresql",
    ),
    "standard_part_providers_get": request("standard_part_provider_list"),
    "standard_part_download_register": request(
        "standard_part_catalog_write_or_reuse"
    ),
    "projection_sync": request("projection"),
    "projection_rebuild": request("projection"),
}


def test_service_method_capability_mapping_is_exact_and_immutable() -> None:
    assert dict(SERVICE_METHOD_CAPABILITIES) == EXPECTED_SERVICE_METHOD_CAPABILITIES
    with pytest.raises(TypeError):
        SERVICE_METHOD_CAPABILITIES["new"] = request("projection")  # type: ignore[index]


def test_uninitialized_mcp_registers_tools_without_constructing_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_bootstrap_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("MCP startup constructed operational settings or service")

    monkeypatch.setattr(server.Settings, "from_environment", fail)
    monkeypatch.setattr(server, "MechanicalDesignService", fail)

    mcp = create_mcp()

    assert "design_system_status" in mcp._tool_manager._tools
    assert "design_system_doctor" in mcp._tool_manager._tools
    assert "family_bootstrap_get" in mcp._tool_manager._tools
    assert "workspace_product_family_list" in mcp._tool_manager._tools
    assert "workspace_product_family_create" in mcp._tool_manager._tools
    assert "workspace_product_family_set_default" in mcp._tool_manager._tools
    assert "workspace_product_family_active" in mcp._tool_manager._tools
    assert "standard_part_providers_get" in mcp._tool_manager._tools
    assert "standard_part_sources_status" in mcp._tool_manager._tools
    assert "standard_part_catalog_enable" in mcp._tool_manager._tools
    assert "standard_part_catalog_disable" in mcp._tool_manager._tools
    assert list(tmp_path.iterdir()) == []


def test_workspace_family_mcp_tools_are_bootstrap_safe_and_confirmation_bound(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-mcp", dry_run=False)
    runtime = BootstrapRuntime.from_process(cwd=workspace, environ={})

    def fail_service(settings: object) -> object:
        del settings
        pytest.fail("workspace family MCP tool constructed operational service")

    mcp = create_mcp(runtime=runtime, service_factory=fail_service)
    empty = json.loads(tool(mcp, "workspace_product_family_list")())
    unselected = json.loads(tool(mcp, "workspace_product_family_active")(""))

    with pytest.raises(ToolError) as wrong_confirmation:
        tool(mcp, "workspace_product_family_create")(
            "org-001",
            "Example organization",
            "group-001",
            "Example group",
            "family-001",
            "Example family",
            "[]",
            "wrong",
        )
    assert list((workspace / "config/product_families").iterdir()) == []

    created = json.loads(
        tool(mcp, "workspace_product_family_create")(
            "org-001",
            "Example organization",
            "group-001",
            "Example group",
            "family-001",
            "Example family",
            "[]",
            "CREATE family-001",
        )
    )
    with pytest.raises(ToolError):
        tool(mcp, "workspace_product_family_set_default")(
            "family-001", "wrong"
        )
    default = json.loads(
        tool(mcp, "workspace_product_family_set_default")(
            "family-001", "SET DEFAULT family-001"
        )
    )
    selected = json.loads(tool(mcp, "workspace_product_family_active")(""))

    assert empty["state"] == "empty"
    assert unselected["code"] == "PRODUCT_FAMILY_REQUIRED"
    assert json.loads(str(wrong_confirmation.value))["code"] == "CONFIRMATION_REQUIRED"
    assert created["result"] == "created"
    assert default["result"] == "updated"
    assert selected["family_id"] == "family-001"


def test_selected_family_mcp_constructs_normal_service_once(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-mcp", dry_run=False)
    runtime_without_family = BootstrapRuntime.from_process(cwd=workspace, environ={})
    runtime_without_family.create_product_family(
        organization_id="org-001",
        organization_name="Example organization",
        design_group_id="group-001",
        design_group_name="Example group",
        family_id="family-001",
        family_name="Example family",
        aliases=[],
    )
    runtime_without_family.set_default_product_family("family-001")
    calls: list[object] = []
    runtime = BootstrapRuntime.from_process(
        cwd=workspace,
        environ={"MECH_DESIGN_DATABASE_URL": "postgresql://configured"},
        probes=DoctorProbes(
            postgresql=lambda value: ProbeResult(available=True),
            neo4j=lambda uri, user, password: ProbeResult(available=True),
            freecadcmd=lambda path: ProbeResult(available=True),
            artifact_root=lambda path: ProbeResult(available=True),
        ),
    )

    class ReadyService:
        def family_bootstrap_get(self) -> dict[str, object]:
            return {"family_id": "family-001"}

    def service_factory(settings: object) -> ReadyService:
        calls.append(settings)
        return ReadyService()

    mcp = create_mcp(runtime=runtime, service_factory=service_factory)
    first = json.loads(tool(mcp, "family_bootstrap_get")())
    second = json.loads(tool(mcp, "family_bootstrap_get")())

    assert first == second == {"family_id": "family-001"}
    assert len(calls) == 1
    assert calls[0].family_config_path.name == "family-001.json"


def test_uninitialized_mcp_status_doctor_and_operational_guard_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_bootstrap_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("guarded MCP call constructed operational service")

    monkeypatch.setattr(server.Settings, "from_environment", fail)
    monkeypatch.setattr(server, "MechanicalDesignService", fail)
    mcp = create_mcp()

    status = json.loads(tool(mcp, "design_system_status")())
    doctor = json.loads(tool(mcp, "design_system_doctor")())
    with pytest.raises(ToolError) as captured:
        tool(mcp, "family_bootstrap_get")()
    guarded = json.loads(str(captured.value))
    status_after_error = json.loads(tool(mcp, "design_system_status")())

    assert status["status"] == {"overall": "setup_required"}
    assert doctor["status"] == {"overall": "setup_required"}
    assert guarded["schema_version"] == "MechanicalDesignSetupResponse/v1"
    assert guarded["status"] == "setup_required"
    assert guarded["code"] == "WORKSPACE_NOT_INITIALIZED"
    assert status_after_error == status
    assert list(tmp_path.iterdir()) == []


def test_lazy_proxy_constructs_service_only_after_a_ready_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    clear_bootstrap_environment(monkeypatch)
    monkeypatch.chdir(workspace)
    calls: list[str] = []

    class ProviderService:
        def standard_part_providers_get(self, category: str = "") -> dict[str, object]:
            calls.append(f"provider:{category}")
            return {"providers": []}

    settings = object()

    def settings_factory() -> object:
        calls.append("settings")
        return settings

    def service_factory(value: object) -> ProviderService:
        assert value is settings
        calls.append("service")
        return ProviderService()

    monkeypatch.setattr(server.Settings, "from_environment", settings_factory)
    monkeypatch.setattr(server, "MechanicalDesignService", service_factory)

    mcp = create_mcp()
    assert calls == []
    result = json.loads(tool(mcp, "standard_part_providers_get")(""))

    assert result["schema_version"] == "StandardPartProviders/v1"
    assert result["providers"]
    assert calls == []


def test_standard_part_configuration_mcp_tools_are_bootstrap_safe(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fail_service(settings: object) -> object:
        calls.append("service")
        pytest.fail("standard-part configuration constructed the service")

    uninitialized = BootstrapRuntime.from_process(cwd=tmp_path, environ={})
    uninitialized_mcp = create_mcp(
        runtime=uninitialized,
        service_factory=fail_service,
    )

    providers = json.loads(tool(uninitialized_mcp, "standard_part_providers_get")(""))
    missing_workspace = json.loads(
        tool(uninitialized_mcp, "standard_part_sources_status")()
    )

    assert providers["schema_version"] == "StandardPartProviders/v1"
    assert providers["providers"]
    assert missing_workspace["schema_version"] == (
        "StandardPartConfigurationResult/v1"
    )
    assert missing_workspace["status"] == "setup_required"
    assert missing_workspace["code"] == "WORKSPACE_NOT_INITIALIZED"
    assert calls == []
    assert list(tmp_path.iterdir()) == []

    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-test", dry_run=False)
    runtime = BootstrapRuntime.from_process(cwd=workspace, environ={})
    mcp = create_mcp(runtime=runtime, service_factory=fail_service)
    status_before = json.loads(tool(mcp, "design_system_status")())
    disabled = json.loads(tool(mcp, "standard_part_sources_status")())
    missing = tmp_path / "missing-catalog"
    missing_result = json.loads(
        tool(mcp, "standard_part_catalog_enable")(str(missing))
    )
    status_after_failure = json.loads(tool(mcp, "design_system_status")())

    assert disabled["status"] == "warning"
    assert disabled["code"] == "STANDARD_PART_CATALOG_DISABLED"
    assert missing_result["status"] == "blocked"
    assert missing_result["code"] == "STANDARD_PART_CATALOG_ROOT_NOT_FOUND"
    assert not missing.exists()
    assert status_after_failure == status_before

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    enabled = json.loads(tool(mcp, "standard_part_catalog_enable")(str(catalog)))
    repeated = json.loads(tool(mcp, "standard_part_catalog_enable")(str(catalog)))
    disabled_result = json.loads(tool(mcp, "standard_part_catalog_disable")())
    repeated_disable = json.loads(tool(mcp, "standard_part_catalog_disable")())

    assert enabled["code"] == "STANDARD_PART_CATALOG_CONFIGURED"
    assert repeated["code"] == "STANDARD_PART_CATALOG_ALREADY_CONFIGURED"
    assert disabled_result["code"] == "STANDARD_PART_CATALOG_DISABLED"
    assert repeated_disable["code"] == "STANDARD_PART_CATALOG_ALREADY_DISABLED"
    assert list(catalog.iterdir()) == []
    assert calls == []


def test_injected_service_bypasses_bootstrap_for_existing_boundary_tests(
    tmp_path: Path,
) -> None:
    class InjectedService:
        def family_create(self, **kwargs: object) -> dict[str, object]:
            return {"family_id": kwargs["family_id"]}

    mcp = create_mcp(
        service=InjectedService(),
        runtime=BootstrapRuntime.from_process(cwd=tmp_path, environ={}),
    )

    result = json.loads(
        tool(mcp, "family_create")(
            "family-001",
            "Family",
            "group-001",
            "[]",
            "confirm",
        )
    )
    assert result == {"family_id": "family-001"}


def test_design_job_mcp_tools_have_exact_names_and_do_not_accept_paths_as_identity(
    tmp_path: Path,
) -> None:
    class JobService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def design_job_create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("create", kwargs))
            return {"schema_version": "MechanicalDesignJob/v1", "job_id": "job-1"}

        def design_job_list(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("list", kwargs))
            return {"schema_version": "MechanicalDesignJobList/v1", "jobs": []}

        def design_job_get(self, *, job_id: str) -> dict[str, object]:
            self.calls.append(("get", {"job_id": job_id}))
            return {"schema_version": "MechanicalDesignJob/v1", "job_id": job_id}

        def design_job_resolve(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("resolve", kwargs))
            return {"schema_version": "MechanicalDesignJobResolution/v1", "candidates": []}

        def design_job_close(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("close", kwargs))
            return {"schema_version": "MechanicalDesignJob/v1", "job_id": kwargs["job_id"]}

        def design_job_reopen(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("reopen", kwargs))
            return {"schema_version": "MechanicalDesignJob/v1", "job_id": kwargs["job_id"]}

    service = JobService()
    mcp = create_mcp(
        service=service,
        runtime=BootstrapRuntime.from_process(cwd=tmp_path, environ={}),
    )
    names = {
        "design_job_create",
        "design_job_list",
        "design_job_get",
        "design_job_resolve",
        "design_job_close",
        "design_job_reopen",
    }

    assert names <= set(mcp._tool_manager._tools)
    assert not {"design_job_doctor", "design_job_repair"} & set(mcp._tool_manager._tools)
    for name in names:
        description = mcp._tool_manager._tools[name].description
        assert "do not create a Git worktree" in description
    assert mcp._tool_manager._tools["design_job_create"].parameters == {
        "type": "object",
        "title": "design_job_createArguments",
        "required": ["job_type", "title", "organization_id", "design_group_id", "idempotency_token"],
        "properties": {
            "job_type": {"title": "Job Type", "type": "string"},
            "title": {"title": "Title", "type": "string"},
            "organization_id": {"title": "Organization Id", "type": "string"},
            "design_group_id": {"title": "Design Group Id", "type": "string"},
            "idempotency_token": {"title": "Idempotency Token", "type": "string"},
            "family_id": {"default": "", "title": "Family Id", "type": "string"},
            "source_files": {"default": [], "items": {"type": "string"}, "title": "Source Files", "type": "array"},
        },
    }
    created = json.loads(
        tool(mcp, "design_job_create")(
            "mechanical_design",
            "Pump design",
            "org-001",
            "group-001",
            "job-create-001",
            "",
        )
    )
    resolved = json.loads(tool(mcp, "design_job_resolve")("pump"))

    assert created["schema_version"] == "MechanicalDesignJob/v1"
    assert resolved["candidates"] == []
    assert "path" not in tool(mcp, "design_job_get").__annotations__


def test_design_job_mcp_uses_the_shared_redacted_error_contract(tmp_path: Path) -> None:
    class FailingJobService:
        def design_job_get(self, *, job_id: str) -> dict[str, object]:
            raise PermissionError(f"unauthorized title at /private/{job_id}")

    mcp = create_mcp(
        service=FailingJobService(),
        runtime=BootstrapRuntime.from_process(cwd=tmp_path, environ={}),
    )
    with pytest.raises(ToolError) as captured:
        tool(mcp, "design_job_get")("00000000-0000-4000-8000-000000000499")
    response = json.loads(str(captured.value))
    assert response["schema_version"] == "MechanicalDesignJobError/v1"
    assert response["code"] == "JOB_REQUEST_FAILED"
    assert response["next_action"]
    assert "private" not in json.dumps(response)


def test_unmapped_proxy_member_is_blocked_without_constructing_service(
    tmp_path: Path,
) -> None:
    runtime = BootstrapRuntime.from_process(cwd=tmp_path, environ={})

    def fail() -> object:
        pytest.fail("unmapped member constructed a service")

    proxy = _LazyServiceProxy(runtime=runtime, service_factory=fail)

    with pytest.raises(ToolError) as captured:
        proxy.not_mapped()

    response = json.loads(str(captured.value))
    assert response["status"] == "blocked"
    assert response["code"] == "CAPABILITY_MAPPING_MISSING"


def test_job_proxy_uses_family_independent_job_operational_settings() -> None:
    job_settings = object()

    class JobRuntime:
        def require_initialized(self, capability: str) -> None:
            assert capability == "design_job_workspace"

        def require_capability(self, request: object, *, probe: bool) -> None:
            assert getattr(request, "capability", request) == "design_job_workspace"
            assert probe is True

        def job_operational_settings(self) -> object:
            return job_settings

        def operational_settings(self) -> object:
            pytest.fail("Job operations must not require a selected product family")

        def blocked_response(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        def status(self) -> dict[str, object]:
            return {}

    class JobService:
        def design_job_list(self) -> dict[str, object]:
            return {"schema_version": "MechanicalDesignJobList/v1", "jobs": []}

    constructed: list[object] = []
    proxy = _LazyServiceProxy(
        runtime=JobRuntime(),
        service_factory=lambda settings: constructed.append(settings) or JobService(),
    )
    assert proxy.design_job_list()["jobs"] == []
    assert constructed == [job_settings]
