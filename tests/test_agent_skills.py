from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

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


def _frontmatter(skill_markdown: str) -> dict[str, object]:
    match = re.match(
        r"\A---\s*\n(?P<header>.*?)\n---\s*\n",
        skill_markdown,
        flags=re.DOTALL,
    )
    assert match is not None
    parsed = yaml.safe_load(match.group("header"))
    assert isinstance(parsed, dict)
    return parsed


def _markdown_table(markdown: str, heading: str) -> dict[str, dict[str, str]]:
    lines = markdown.splitlines()
    start = lines.index(heading)
    table_lines: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            table_lines.append(line)
    assert len(table_lines) >= 3
    headers = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    assert all(headers)
    assert all(
        set(cell.strip()) <= {"-", ":"}
        for cell in table_lines[1].strip("|").split("|")
    )
    rows: dict[str, dict[str, str]] = {}
    for line in table_lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == len(headers)
        row = dict(zip(headers, cells, strict=True))
        key = row[headers[0]]
        assert key not in rows
        rows[key] = row
    return rows


def test_project_owned_skills_are_complete_and_named_by_directory() -> None:
    skill_directories = {
        path.name for path in SKILLS_ROOT.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    assert skill_directories == set(EXPECTED_SKILLS)
    assert all(
        not path.is_dir() or (path / "SKILL.md").is_file()
        for path in SKILLS_ROOT.iterdir()
    )

    for skill_name, required_files in EXPECTED_SKILLS.items():
        skill_root = SKILLS_ROOT / skill_name
        markdown = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        assert _frontmatter(markdown)["name"] == skill_name
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
    metadata_text = (skill_root / "agents" / "openai.yaml").read_text(
        encoding="utf-8"
    )
    metadata = yaml.safe_load(metadata_text)
    assert isinstance(metadata, dict)

    frontmatter = _frontmatter(skill_text)
    assert frontmatter["name"] == "mechanical-design-job-workspace"
    description = frontmatter["description"]
    assert isinstance(description, str)
    for trigger in (
        "new designs",
        "existing models",
        "resumptions",
        "Product Family onboarding",
        "Design Lessons",
    ):
        assert trigger.casefold() in description.casefold()
    assert "do not use for software-only changes" in description.casefold()

    interface = metadata.get("interface")
    policy = metadata.get("policy")
    assert isinstance(interface, dict)
    assert isinstance(policy, dict)
    assert policy["allow_implicit_invocation"] is True
    default_prompt = interface["default_prompt"]
    assert isinstance(default_prompt, str)
    assert "$mechanical-design-job-workspace" in default_prompt
    assert re.search(r'(?m)^  display_name: ".+"$', metadata_text)
    assert re.search(r'(?m)^  short_description: ".+"$', metadata_text)
    assert re.search(r'(?m)^  default_prompt: ".+"$', metadata_text)

    assert "Do not create a Git branch or Git worktree" in skill_text
    routing = _markdown_table(skill_text, "## Routing decision matrix")
    assert routing == {
        "Explicit Job UUID or display ID": {
            "Incoming request": "Explicit Job UUID or display ID",
            "Required action": "Call `design_job_get` for authorized state.",
            "Stop rule": "Never pass the ID as a `design_job_resolve` query.",
        },
        "Explicitly independent demand": {
            "Incoming request": "Explicitly independent demand",
            "Required action": "Call `design_job_create` directly with an idempotency token.",
            "Stop rule": "Do not resolve merely similar Jobs first.",
        },
        "Resume without an explicit ID; one active/blocked candidate": {
            "Incoming request": "Resume without an explicit ID; one active/blocked candidate",
            "Required action": "Call `design_job_resolve` for `active` and `blocked`; reuse it.",
            "Stop rule": "Do not create a duplicate Job.",
        },
        "Resume without an explicit ID; multiple candidates": {
            "Incoming request": "Resume without an explicit ID; multiple candidates",
            "Required action": "Return candidates.",
            "Stop rule": "Stop; never select or create automatically.",
        },
        "Resume without an explicit ID; zero candidates": {
            "Incoming request": "Resume without an explicit ID; zero candidates",
            "Required action": "Clarify whether the demand is independent/new.",
            "Stop rule": "Create only after that explicit intent.",
        },
    }

    provenance = _markdown_table(skill_text, "## Provenance and Job type")
    assert provenance == {
        "New, existing, or resumed mechanical design": {
            "Product operation": "New, existing, or resumed mechanical design",
            "Job type": "`mechanical_design`",
            "Provenance rule": "Use the routed Job.",
        },
        "Product Family intake/onboarding": {
            "Product operation": "Product Family intake/onboarding",
            "Job type": "`product_family_onboarding`",
            "Provenance rule": "Create or reuse the onboarding Job through the routing matrix.",
        },
        "Product Family review, knowledge, or database publication": {
            "Product operation": "Product Family review, knowledge, or database publication",
            "Job type": "`product_family_onboarding`",
            "Provenance rule": "Reuse the original onboarding Job.",
        },
        "Design Lesson": {
            "Product operation": "Design Lesson",
            "Job type": "Originating `mechanical_design` only",
            "Provenance rule": "Stop if origin is missing or ambiguous; never create a replacement or onboarding Job.",
        },
    }

    job_types = _markdown_table(contract_text, "## Allowed Job types and phases")
    assert job_types == {
        "`mechanical_design`": {
            "Job type": "`mechanical_design`",
            "Allowed phases": "`requirements`, `design`, `validation`, `delivery`, `lesson_capture`, `completed`",
        },
        "`product_family_onboarding`": {
            "Job type": "`product_family_onboarding`",
            "Allowed phases": "`intake`, `analysis`, `knowledge_review`, `database_publication`, `completed`",
        },
    }

    lifecycle = _markdown_table(contract_text, "## Lifecycle calls and confirmations")
    assert lifecycle == {
        "`design_job_close`": {
            "Operation": "`design_job_close`",
            "Required values": "Current `expected_revision`, terminal `status` (`completed`, `cancelled`, or `archived`), valid phase, and reason.",
            "Canonical confirmation": "`关闭 <job-reference>`",
        },
        "`design_job_reopen`": {
            "Operation": "`design_job_reopen`",
            "Required values": "Current `expected_revision`, valid phase, and reason; only a terminal Job reopens to `active`.",
            "Canonical confirmation": "`重开 <job-reference>`",
        },
        "CLI/service doctor and repair": {
            "Operation": "CLI/service doctor and repair",
            "Required values": "`design_job_doctor` is read-only; `design_job_repair` also needs current `expected_revision`, doctor receipt SHA-256, and reason.",
            "Canonical confirmation": "`修复 <job-reference>` for repair",
        },
    }


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
