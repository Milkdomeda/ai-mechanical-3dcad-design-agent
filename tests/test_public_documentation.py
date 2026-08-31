from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
ARCHITECTURE = PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
DATABASE = PROJECT_ROOT / "docs" / "DATABASE_DEPLOYMENT.md"
FREECAD = PROJECT_ROOT / "docs" / "FREECAD_GUI_MCP_INTEGRATION.md"
LEARNING = PROJECT_ROOT / "docs" / "ENGINEER_LEARNING_PLAYBOOK.md"
WINDOWS = PROJECT_ROOT / "docs" / "WINDOWS_RELEASE_ACCEPTANCE.md"
SKILL = PROJECT_ROOT / ".agents" / "skills" / "mechanical-design" / "SKILL.md"
LOCAL_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_public_identity_and_design_process() -> None:
    text = normalized(README)
    assert text.startswith("# AI Mechanical 3DCAD Design Agent")
    assert "coding agents" in text
    assert "does not embed a language model" in text
    assert "does not replace engineering review" in text
    for fragment in (
        "requirement clarification",
        "one natural-language direction approval",
        "knowledge retrieval",
        "CAD modeling",
        "automatic validation and correction",
        "natural-language final confirmation",
        "automatic Design Lesson evaluation",
    ):
        assert fragment in text


def test_normal_process_is_consistent_across_agent_guidance() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "AGENTS.md", README, LEARNING, SKILL)
    )
    for fragment in (
        "APPROVE",
        "REJECT",
        "UNCLEAR",
        "design_start",
        "design_knowledge_retrieve",
        "design_record_result",
        "design_confirm",
        "design_lesson_decide",
    ):
        assert fragment in combined
    assert "fixed phrase" in combined
    assert "must not invalidate a completed" in combined


def test_architecture_keeps_cad_independent_from_knowledge_services() -> None:
    text = normalized(ARCHITECTURE)
    assert "DesignSession/v1" in text
    assert "PostgreSQL" in text
    assert "Neo4j" in text
    assert "It stores no design-session or CAD-edit state" in text
    assert "Knowledge outages never invalidate" in text
    assert "DesignLessonReviewCard/v1" in text


def test_external_freecad_boundary_is_documented() -> None:
    text = normalized(FREECAD)
    for fragment in (
        "https://github.com/neka-nat/freecad-mcp",
        "MIT",
        "7667e272e1db669ff61dd5411fb4f622691f2dbc",
        "not bundled",
        "127.0.0.1:9875",
        "remote_enabled=false",
        "FreeCAD 1.1.3",
    ):
        assert fragment in text


def test_database_and_windows_guides_cover_supported_boundaries() -> None:
    database = normalized(DATABASE)
    for fragment in (
        "PostgreSQL",
        "pgvector",
        "Neo4j",
        "mechanical-design knowledge bootstrap",
        "loopback",
        "macOS",
        "PowerShell",
    ):
        assert fragment in database
    windows = normalized(WINDOWS)
    for fragment in (
        "Windows 11 x64",
        "Python 3.12",
        "FreeCAD 1.1.3",
        "Docker Desktop",
        "knowledge",
    ):
        assert fragment in windows


def test_public_readme_local_links_resolve_inside_project() -> None:
    for target in LOCAL_LINK.findall(README.read_text(encoding="utf-8")):
        if "://" in target or target.startswith("#"):
            continue
        resolved = (PROJECT_ROOT / target.split("#", 1)[0]).resolve()
        assert resolved.is_relative_to(PROJECT_ROOT.resolve())
        assert resolved.exists(), target
