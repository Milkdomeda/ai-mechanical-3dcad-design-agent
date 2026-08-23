from __future__ import annotations

import ast
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / ".agents" / "skills"
EXPECTED_SKILLS = {
    "freecad-model-validation": {
        "agents/openai.yaml",
        "references/validation-spec.md",
        "scripts/freecad_model_validation.py",
    },
    "freecad-standard-parts": {
        "agents/openai.yaml",
        "references/providers.md",
        "scripts/cache_step_part.py",
        "scripts/freecad_standard_parts.py",
    },
}


def _frontmatter_name(skill_markdown: str) -> str:
    match = re.match(
        r"\A---\s*\n(?P<header>.*?)\n---\s*\n",
        skill_markdown,
        flags=re.DOTALL,
    )
    assert match is not None
    name = re.search(r"(?m)^name:\s*([^\n]+)$", match.group("header"))
    assert name is not None
    return name.group(1).strip().strip('"\'')


def test_project_owned_skills_are_complete_and_named_by_directory() -> None:
    skill_directories = {
        path.name for path in SKILLS_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    }
    assert skill_directories == set(EXPECTED_SKILLS)

    for skill_name, required_files in EXPECTED_SKILLS.items():
        skill_root = SKILLS_ROOT / skill_name
        markdown = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        assert _frontmatter_name(markdown) == skill_name
        for relative in required_files:
            assert (skill_root / relative).is_file(), (skill_name, relative)


def test_skill_python_sources_are_syntax_valid_and_portable() -> None:
    for source in sorted(SKILLS_ROOT.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        ast.parse(text, filename=str(source))
        assert "/Users/" not in text
        assert not re.search(r"[A-Za-z]:\\\\Users\\\\", text)


def test_superpowers_is_documented_as_optional_and_not_vendored() -> None:
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    optional_guide = (
        PROJECT_ROOT / "docs" / "OPTIONAL_AGENT_WORKFLOWS.md"
    ).read_text(encoding="utf-8")
    assert "optional external workflow" in agents
    assert "Never install or configure Superpowers automatically" in agents
    assert "must not block" in agents
    assert "does not vendor" in optional_guide
    assert not (SKILLS_ROOT / "brainstorming").exists()
    assert not (SKILLS_ROOT / "superpowers").exists()
