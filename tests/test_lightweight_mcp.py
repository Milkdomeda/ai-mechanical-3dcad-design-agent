from __future__ import annotations

import json
from pathlib import Path

import pytest

from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
from mechanical_design_agent.secure_fs import read_managed_file
from mechanical_design_agent.server import create_mcp
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


def _tool(server: object, name: str):
    return server._tool_manager._tools[name].fn


def test_default_tools_use_injected_lightweight_services_without_legacy_startup() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class LocalDesign:
        def start(self, **kwargs: object) -> dict[str, object]:
            calls.append(("start", kwargs))
            return {"status": "approved", "design_id": kwargs["design_id"]}

        def record_result(self, **kwargs: object) -> dict[str, object]:
            calls.append(("result", kwargs))
            return {"status": "completed", "design_id": kwargs["design_id"]}

    class Knowledge:
        def retrieve(self, **kwargs: object) -> dict[str, object]:
            calls.append(("knowledge", kwargs))
            return {"status": "completed_no_match", "blocking": False}

    def forbidden_legacy(_settings: object) -> object:
        pytest.fail("default start/result instantiated the governed service")

    server = create_mcp(
        lightweight_service=LocalDesign(),
        lightweight_knowledge_service=Knowledge(),
        service_factory=forbidden_legacy,
        tool_profile="design",
    )

    started = json.loads(
        _tool(server, "design_start")(
            "carrier",
            "Carrier",
            "new_design",
            '{"capacity":4}',
            "One-piece PLA carrier",
            "确认",
            "",
        )
    )
    knowledge = json.loads(
        _tool(server, "design_knowledge_retrieve")(
            "carrier", "basketball cradle", '{"material":"PLA"}', "[]", False
        )
    )
    recorded = json.loads(
        _tool(server, "design_record_result")(
            "carrier", "model.FCStd", "validation/report.json", '["validation/view.png"]'
        )
    )

    assert started["status"] == "approved"
    assert knowledge == {"blocking": False, "status": "completed_no_match"}
    assert recorded["status"] == "completed"
    assert [name for name, _arguments in calls] == ["start", "knowledge", "result"]


def test_lightweight_bootstrap_settings_do_not_require_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="agent", dry_run=False)
    executable = tmp_path / "FreeCADCmd"
    executable.write_bytes(b"reviewed executable")
    pinned = read_managed_file(executable)
    monkeypatch.setattr(
        "mechanical_design_agent.bootstrap_runtime.run_freecad_version",
        lambda path: __import__("subprocess").CompletedProcess(
            [str(path), "--version"], 0, "FreeCAD 1.1.3\n", ""
        ),
    )
    runtime = BootstrapRuntime.from_process(
        cwd=workspace,
        environ={},
        freecad_command=executable,
        freecad_sha256=pinned.sha256,
    )

    settings = runtime.lightweight_design_settings()

    assert settings.workspace == workspace
    assert settings.design_root == workspace / "designs"
    assert settings.freecadcmd == executable
    assert settings.freecadcmd_sha256 == pinned.sha256
    assert not hasattr(settings, "database_url")
