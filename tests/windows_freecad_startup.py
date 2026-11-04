"""Installed-wheel native Windows MCP regression for Issue #3.

Run outside the checkout with the installed environment's Python -I -B.
Evidence contains only synthetic models and explicit test runtime identities.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import mechanical_design_agent
from mechanical_design_agent.fcstd_security import inspect_fcstd_bytes
from mechanical_design_agent.freecad_runner import run_freecad_script
from mechanical_design_agent.secure_fs import read_managed_file, validate_managed_path
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


async def exercise(workspace: Path, executable: Path, evidence: Path) -> dict:
    initialize_workspace(workspace=workspace, actor_id="synthetic-engineer", dry_run=False,
                         organization_id="synthetic-org", design_group_id="synthetic-group")
    pin = read_managed_file(executable)
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("MECH_DESIGN_") or key in {"PYTHONPATH", "PYTHONHOME"}:
            environment.pop(key)
    environment.update(MECH_DESIGN_WORKSPACE=str(workspace),
                       MECH_DESIGN_FREECADCMD=str(executable),
                       MECH_DESIGN_FREECADCMD_SHA256=pin.sha256)
    # No FreeCAD installation or current-directory entry is inherited in PATH.
    root = Path(environment["SYSTEMROOT"])
    environment["PATH"] = os.pathsep.join((str(root / "System32"), str(root)))
    parameters = StdioServerParameters(command=sys.executable,
        args=["-I", "-B", "-m", "mechanical_design_agent.server"],
        env=environment, cwd=str(evidence))
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=180)) as session:
            await session.initialize()

            async def call(name: str, arguments: dict) -> dict:
                result = await session.call_tool(name, arguments)
                assert not result.isError, result
                texts = [item.text for item in result.content if item.type == "text"]
                assert len(texts) == 1, texts
                value = json.loads(texts[0])
                assert "error" not in value, value
                return value

            status = await call("design_system_status", {})
            codes = {item["code"] for item in status["components"]}
            assert {"WORKSPACE_READY", "FREECADCMD_READY"} <= codes, status
            request = dict(design_id="startup-regression", title="Synthetic startup regression",
                model_classification="new_design", requirements_json='{"units":"mm"}',
                proposal_summary="Create an empty synthetic regression model", approval_text="approved")
            started = await call("design_start", request)
            assert started.get("model_path"), started
            model = Path(started["model_path"])
            data = model.read_bytes()
            assert len(data) > 0
            inspect_fcstd_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            resumed = await call("design_start", request)
            assert resumed["resumed"] is True
            assert resumed["model_path"] == started["model_path"]
            assert hashlib.sha256(model.read_bytes()).hexdigest() == digest

    # A real FreeCAD process reopens the result and exports STEP for exercising
    # the existing-model normalization path through the same installed MCP.
    probe = evidence / "reopen.py"
    step = evidence / "synthetic-source.step"
    reopened = evidence / "reopened.json"
    probe.write_text(
        "import json, sys\nimport FreeCAD as App\nimport Part\nimport Import\n"
        "doc=App.openDocument(sys.argv[1], hidden=True)\n"
        "assert doc.getObject('DesignAudit') is not None\n"
        "App.closeDocument(doc.Name)\n"
        "doc=App.newDocument('SyntheticSource')\n"
        "obj=doc.addObject('Part::Feature','Block')\n"
        "obj.Shape=Part.makeBox(10,20,30)\ndoc.recompute()\n"
        "Import.export([obj],sys.argv[2])\nApp.closeDocument(doc.Name)\n"
        "with open(sys.argv[3],'w',encoding='utf-8') as stream: json.dump({'reopened':True},stream)\n",
        encoding="utf-8")
    result = run_freecad_script(executable, probe, [model, step, reopened],
        timeout_seconds=120, expected_sha256=pin.sha256,
        expected_identity=pin.identity, controlled_directory=evidence)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(reopened.read_text(encoding="utf-8")) == {"reopened": True}
    before = hashlib.sha256(step.read_bytes()).hexdigest()
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=180)) as session:
            await session.initialize()
            response = await session.call_tool("design_start", dict(request,
                design_id="existing-step-regression", model_classification="existing_model", source_path=str(step)))
            assert not response.isError, response
            normalized = json.loads(response.content[0].text)
            assert normalized.get("model_path"), normalized
            normalized_data = Path(normalized["model_path"]).read_bytes()
            inspect_fcstd_bytes(normalized_data)
            assert hashlib.sha256(step.read_bytes()).hexdigest() == before
    # Confirm the normalized solid really loads with its expected volume.
    check = evidence / "check-solid.py"
    solid_check = evidence / "solid-check.json"
    check.write_text("import json, sys\nimport FreeCAD as App\ndoc=App.openDocument(sys.argv[1],hidden=True)\n"
        "assert any(hasattr(o,'Shape') and o.Shape.isValid() and abs(o.Shape.Volume-6000)<0.000001 for o in doc.Objects)\n"
        "App.closeDocument(doc.Name)\n"
        "with open(sys.argv[2],'w',encoding='utf-8') as stream: json.dump({'valid':True,'volume_mm3':6000},stream)\n", encoding="utf-8")
    result = run_freecad_script(executable, check, [Path(normalized["model_path"]), solid_check], timeout_seconds=120,
        expected_sha256=pin.sha256, expected_identity=pin.identity, controlled_directory=evidence)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(solid_check.read_text(encoding="utf-8")) == {"valid": True, "volume_mm3": 6000}
    return {"status": "passed", "model_sha256": digest, "model_bytes": len(data),
            "normalized_model_bytes": len(normalized_data), "source_unchanged": True,
            "reopened": True, "resumed": True, "freecad_sha256": pin.sha256}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--freecadcmd", type=Path, required=True)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    options = parser.parse_args()
    assert sys.platform == "win32", "native regression requires Windows"
    assert Path(mechanical_design_agent.__file__).is_relative_to(Path(sys.prefix)), "must use installed wheel"
    options.root.mkdir(parents=True, exist_ok=False)
    evidence = options.root / "evidence"
    evidence.mkdir()
    workspace = options.root / "Design workspace 中文"
    executable = options.freecadcmd
    if options.extended:
        executable = validate_managed_path(executable, allow_missing_leaf=False).path
        workspace = Path("\\\\?\\" + str(workspace.absolute()))
    record = {"package_version": mechanical_design_agent.__version__, "os": platform.platform(),
              "python": sys.version, "extended_input": options.extended, "baseline": options.baseline}
    try:
        record.update(asyncio.run(exercise(workspace, executable, evidence)))
    except Exception:
        record.update(status="failed", traceback=traceback.format_exc())
        if not options.baseline:
            raise
    finally:
        (evidence / "startup-result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(record, ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    main()
