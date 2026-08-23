from __future__ import annotations

import ast
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / ".agents" / "skills"
EXPECTED_SKILLS = {
    "mechanical-design-job-workspace": {
        "agents/openai.yaml",
        "references/job-contract.md",
    },
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


def test_design_job_skill_routes_product_operations_through_jobs() -> None:
    skill_root = SKILLS_ROOT / "mechanical-design-job-workspace"
    skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    contract_text = (skill_root / "references" / "job-contract.md").read_text(
        encoding="utf-8"
    )
    metadata = (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")

    for trigger in (
        "new design",
        "existing model",
        "resume",
        "Product Family onboarding",
        "Design Lessons",
    ):
        assert trigger.casefold() in skill_text.casefold()
    assert "Do not create a Git branch or Git worktree" in skill_text
    assert "design_job_resolve" in skill_text
    assert "design_job_create" in skill_text
    assert "design_job_get" in contract_text
    assert "design_job_list" in contract_text
    assert "design_job_close" in contract_text
    assert "design_job_reopen" in contract_text
    assert "same design" in skill_text.casefold()
    assert "independent demand" in skill_text.casefold()
    assert "ambigu" in skill_text.casefold()
    assert "working-copy" in skill_text.casefold()
    assert "evidence" in skill_text.casefold()
    assert "lesson" in skill_text.casefold()
    assert "Task 6" in contract_text
    assert "not an arbitrary filesystem path" in skill_text.casefold()
    assert "software development" in skill_text.casefold()
    assert "macOS" in skill_text
    assert "Windows" in skill_text
    assert "Blender" in skill_text
    assert "render" in skill_text.casefold()
    assert "video" in skill_text.casefold()

    assert 'display_name: "Mechanical Design Job Workspace"' in metadata
    assert 'short_description: "Route FreeCAD product work through Jobs"' in metadata
    assert 'default_prompt: "Use $mechanical-design-job-workspace' in metadata
    assert "allow_implicit_invocation: true" in metadata


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
