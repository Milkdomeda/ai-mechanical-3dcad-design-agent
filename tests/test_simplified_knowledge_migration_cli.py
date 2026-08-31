from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import mechanical_design_agent.cli as cli
from mechanical_design_agent.long_term_knowledge_migration import build_long_term_export
from mechanical_design_agent.long_term_knowledge_target import MigrationImportResult
from test_long_term_knowledge_migration import _source


def _invoke(monkeypatch, capsys, arguments: list[str]) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["mechanical-design", *arguments])
    with pytest.raises(SystemExit) as captured:
        cli.main()
    output = capsys.readouterr().out
    return int(captured.value.code), json.loads(output) if output else {}


def test_analyze_only_never_connects_to_target_or_edits_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source_env = tmp_path / ".env.local"
    source_env.write_text(
        "MECH_DESIGN_DATABASE_URL=postgresql://source-only.invalid/knowledge\n",
        encoding="utf-8",
    )
    output = tmp_path / "output" / "analysis.json"
    monkeypatch.setattr(
        cli, "read_source_export", lambda _url: build_long_term_export(_source()), raising=False
    )
    target_calls: list[object] = []
    environment_writes: list[object] = []
    monkeypatch.setattr(
        cli,
        "create_target_database",
        lambda *args, **kwargs: target_calls.append((args, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "_update_environment_database_url",
        lambda *args, **kwargs: environment_writes.append((args, kwargs)),
        raising=False,
    )

    code, result = _invoke(
        monkeypatch,
        capsys,
        [
            "knowledge-migrate",
            "--analyze-only",
            "--source-env",
            str(source_env),
            "--output",
            str(output),
        ],
    )

    assert code == 0
    assert result["status"] == "passed"
    assert target_calls == []
    assert environment_writes == []
    assert output.is_file()
    report_text = output.read_text(encoding="utf-8")
    assert "postgresql://" not in report_text
    assert result["counts"] == {
        "product_families": 2,
        "knowledge_assertions": 43,
        "design_lessons": 4,
    }


def test_execute_requires_passed_analysis_and_target_name(
    monkeypatch, capsys
) -> None:
    code, result = _invoke(
        monkeypatch, capsys, ["knowledge-migrate", "--execute"]
    )

    assert code != 0
    assert "--analysis-report" in result["message"]


def _prepared_analysis(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    source_env = tmp_path / ".env.local"
    source_env.write_text(
        "MECH_DESIGN_DATABASE_URL=postgresql://old.invalid/knowledge\n",
        encoding="utf-8",
    )
    analysis = tmp_path / "output" / "analysis.json"
    export = build_long_term_export(_source())
    monkeypatch.setattr(cli, "read_source_export", lambda _url: export)
    cli._analyze_knowledge_migration(source_env, analysis)
    monkeypatch.setattr(
        cli,
        "create_target_database",
        lambda _source_url, _target_name: "postgresql://target.invalid/knowledge",
    )
    monkeypatch.setattr(
        cli,
        "import_simplified_payload",
        lambda _url, payload: MigrationImportResult(
            status="imported",
            source_export_sha256=payload.source_export_sha256,
            payload_sha256=payload.sha256,
            counts={
                "product_families": 2,
                "knowledge_assertions": 43,
                "design_lessons": 4,
            },
        ),
    )
    monkeypatch.setattr(
        cli,
        "validate_simplified_target",
        lambda _url, payload: {
            "status": "passed",
            "payload_sha256": payload.sha256,
        },
    )
    return source_env, analysis


def test_failed_parity_never_changes_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source_env, analysis = _prepared_analysis(tmp_path, monkeypatch)
    before = source_env.read_text(encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_run_target_parity",
        lambda _url, probes: {
            "status": "failed",
            "probe_count": len(probes),
            "passed": len(probes) - 1,
            "failed": 1,
            "failures": [],
            "negative_scope_failures": 0,
        },
    )

    code, result = _invoke(
        monkeypatch,
        capsys,
        [
            "knowledge-migrate",
            "--execute",
            "--analysis-report",
            str(analysis),
            "--target-name",
            "mechanical_design_knowledge",
            "--cutover-env",
            str(source_env),
        ],
    )

    assert code != 0
    assert "parity" in result["message"]
    assert source_env.read_text(encoding="utf-8") == before


def test_passed_execute_without_cutover_leaves_environment_unchanged(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source_env, analysis = _prepared_analysis(tmp_path, monkeypatch)
    before = source_env.read_text(encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_run_target_parity",
        lambda _url, probes: {
            "status": "passed",
            "probe_count": len(probes),
            "passed": len(probes),
            "failed": 0,
            "failures": [],
            "negative_scope_failures": 0,
        },
    )

    code, result = _invoke(
        monkeypatch,
        capsys,
        [
            "knowledge-migrate",
            "--execute",
            "--analysis-report",
            str(analysis),
            "--target-name",
            "mechanical_design_knowledge",
        ],
    )

    assert code == 0
    assert result["status"] == "passed"
    assert result["cutover"] is False
    assert source_env.read_text(encoding="utf-8") == before


def test_passed_execute_changes_only_explicit_cutover_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source_env, analysis = _prepared_analysis(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "_run_target_parity",
        lambda _url, probes: {
            "status": "passed",
            "probe_count": len(probes),
            "passed": len(probes),
            "failed": 0,
            "failures": [],
            "negative_scope_failures": 0,
        },
    )

    code, result = _invoke(
        monkeypatch,
        capsys,
        [
            "knowledge-migrate",
            "--execute",
            "--analysis-report",
            str(analysis),
            "--target-name",
            "mechanical_design_knowledge",
            "--cutover-env",
            str(source_env),
        ],
    )

    assert code == 0
    assert result["cutover"] is True
    assert source_env.read_text(encoding="utf-8") == (
        "MECH_DESIGN_DATABASE_URL=postgresql://target.invalid/knowledge\n"
    )
