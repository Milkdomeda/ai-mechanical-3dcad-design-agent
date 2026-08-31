from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from mechanical_design_agent import cli
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["mechanical-design", *arguments])
    with pytest.raises(SystemExit) as captured:
        cli.main()
    return int(captured.value.code), json.loads(capsys.readouterr().out)


def test_provider_list_needs_no_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    code, result = _run(
        monkeypatch, capsys, "standard-parts", "providers", "--category", "fastener"
    )

    assert code == 0
    assert result["schema_version"] == "StandardPartProviders/v1"
    assert result["providers"]
    assert list(tmp_path.iterdir()) == []


def test_catalog_enable_and_disable_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    initialize_workspace(workspace=workspace, actor_id="agent", dry_run=False)

    enabled_code, enabled = _run(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "enable",
        "--root",
        str(catalog),
        "--workspace",
        str(workspace),
    )
    repeated_code, repeated = _run(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "enable",
        "--root",
        str(catalog),
        "--workspace",
        str(workspace),
    )
    disabled_code, disabled = _run(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "disable",
        "--workspace",
        str(workspace),
    )

    assert (enabled_code, repeated_code, disabled_code) == (0, 0, 0)
    assert enabled["code"] == "STANDARD_PART_CATALOG_CONFIGURED"
    assert repeated["code"] == "STANDARD_PART_CATALOG_ALREADY_CONFIGURED"
    assert disabled["code"] == "STANDARD_PART_CATALOG_DISABLED"
