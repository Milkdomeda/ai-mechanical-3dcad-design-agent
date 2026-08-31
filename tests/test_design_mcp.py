from __future__ import annotations

import json
from pathlib import Path

import pytest

from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
from mechanical_design_agent.secure_fs import (
    read_managed_file,
    relative_managed_path,
    same_managed_path,
)
from mechanical_design_agent.server import create_mcp
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


def _tool(server: object, name: str):
    return server._tool_manager._tools[name].fn


def _fake_x64_freecadcmd_bytes() -> bytes:
    payload = bytearray(70)
    payload[:2] = b"MZ"
    payload[0x3C:0x40] = (64).to_bytes(4, "little")
    payload[64:68] = b"PE\0\0"
    payload[68:70] = (0x8664).to_bytes(2, "little")
    return bytes(payload)


def test_design_tools_use_injected_services_without_database_startup() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Design:
        def start(self, **kwargs: object) -> dict[str, object]:
            calls.append(("start", kwargs))
            return {"status": "approved", "design_id": kwargs["design_id"]}

        def get(self, design_id: str) -> dict[str, object]:
            calls.append(("status", {"design_id": design_id}))
            return {"model_status": "completed", "design_id": design_id}

        def record_result(self, **kwargs: object) -> dict[str, object]:
            calls.append(("result", kwargs))
            return {"status": "completed", "design_id": kwargs["design_id"]}

    class Knowledge:
        def retrieve(self, **kwargs: object) -> dict[str, object]:
            calls.append(("knowledge", kwargs))
            return {"status": "completed_no_match", "blocking": False}

    class Lessons:
        def confirm(self, **kwargs: object) -> dict[str, object]:
            calls.append(("confirm", kwargs))
            return {
                "confirmation_state": "APPROVE",
                "lesson_review_status": "no_material_lessons",
            }

    server = create_mcp(
        design_service=Design(),
        design_knowledge_service=Knowledge(),
        lesson_workflow=Lessons(),
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
    status = json.loads(_tool(server, "design_status")("carrier"))
    knowledge = json.loads(
        _tool(server, "design_knowledge_retrieve")(
            "carrier", "basketball cradle", '{"material":"PLA"}', "[]", False
        )
    )
    recorded = json.loads(
        _tool(server, "design_record_result")(
            "carrier",
            "model.FCStd",
            "validation/report.json",
            '["validation/view.png"]',
        )
    )
    confirmed = json.loads(
        _tool(server, "design_confirm")("carrier", "设计已确认", "[]")
    )

    assert started["status"] == "approved"
    assert status["model_status"] == "completed"
    assert knowledge == {"blocking": False, "status": "completed_no_match"}
    assert recorded["status"] == "completed"
    assert confirmed["lesson_review_status"] == "no_material_lessons"
    assert [name for name, _ in calls] == [
        "start",
        "status",
        "knowledge",
        "result",
        "confirm",
    ]


def test_design_settings_do_not_require_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="agent", dry_run=False)
    executable = tmp_path / "FreeCADCmd"
    executable.write_bytes(_fake_x64_freecadcmd_bytes())
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

    settings = runtime.design_settings()

    assert same_managed_path(settings.workspace, workspace)
    assert relative_managed_path(
        settings.design_root,
        settings.workspace,
        allow_missing_leaf=True,
    ) == Path("designs")
    assert same_managed_path(settings.freecadcmd, executable)
    assert settings.freecadcmd_sha256 == pinned.sha256
    assert not hasattr(settings, "database_url")
