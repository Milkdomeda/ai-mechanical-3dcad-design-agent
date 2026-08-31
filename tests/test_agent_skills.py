from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1] / ".agents" / "skills"
EXPECTED = {
    "mechanical-design",
    "freecad-model-validation",
    "freecad-standard-parts",
}


def _frontmatter(text: str) -> dict[str, object]:
    match = re.match(r"\A---\s*\n(?P<header>.*?)\n---\s*\n", text, re.DOTALL)
    assert match
    value = yaml.safe_load(match.group("header"))
    assert isinstance(value, dict)
    return value


def test_project_skills_are_complete_and_named_by_directory() -> None:
    directories = {
        path.name for path in ROOT.iterdir() if (path / "SKILL.md").is_file()
    }
    assert directories == EXPECTED
    for name in EXPECTED:
        text = (ROOT / name / "SKILL.md").read_text(encoding="utf-8")
        assert _frontmatter(text)["name"] == name
        assert (ROOT / name / "agents" / "openai.yaml").is_file()


def test_mechanical_design_skill_routes_the_complete_normal_process() -> None:
    root = ROOT / "mechanical-design"
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(
        (root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )

    for tool in (
        "design_start",
        "design_knowledge_retrieve",
        "design_record_result",
        "design_confirm",
        "design_lesson_decide",
    ):
        assert tool in text
    assert metadata["policy"]["allow_implicit_invocation"] is True
    assert "$mechanical-design" in metadata["interface"]["default_prompt"]


def test_skill_python_sources_are_syntax_valid_and_portable() -> None:
    for source in ROOT.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        ast.parse(text, filename=str(source))
        assert "/Users/" not in text
        assert not re.search(r"[A-Za-z]:\\\\Users\\\\", text)
