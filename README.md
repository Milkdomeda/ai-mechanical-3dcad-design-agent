# AI Mechanical 3DCAD Design Agent

![AI-generated mechanical CAD product showcase](docs/assets/ai-mechanical-design-showcase.gif)

AI Mechanical 3DCAD Design Agent helps coding agents turn mechanical
requirements into validated FreeCAD models. It combines requirement reasoning,
knowledge reuse, standard-part provenance, deterministic CAD state, automatic
validation, correction, final confirmation, and reusable Design Lessons.

The package provides the `mechanical-design` CLI and
`mechanical-design-mcp` server. A compatible coding agent performs the design
reasoning, while an external FreeCAD GUI MCP performs interactive CAD work. The
package does not embed a language model and does not replace engineering review.

## Design process

```text
User request
  → requirement clarification
  → short design proposal
  → one natural-language direction approval
  → knowledge retrieval
  → CAD modeling
  → automatic validation and correction
  → final result
  → natural-language final confirmation
  → automatic Design Lesson evaluation
  → finish, or one decision before durable lesson publication
```

## Core capabilities

- Create new designs or edit read-only snapshots of existing FCStd/STEP models.
- Retrieve matching Product Family Knowledge and Design Lessons when available.
- Continue CAD work when knowledge has no match or its backend is unavailable.
- Model interactively in FreeCAD and keep FCStd as the source of truth.
- Find purchasable standard parts through configured structured providers and,
  when they miss, extend the search to authoritative manufacturer, standards
  body, industry association, and attributable authorized-distributor sources.
- Register selected CAD components with provider, manufacturer, part identity,
  source, license, validation evidence, and SHA-256 provenance.
- Validate geometry, dimensions, placements, interfaces, assemblies,
  fasteners, BOM consistency, and visual evidence.
- Bind completion to the exact FCStd SHA-256 and passed JSON, Markdown, and PNG
  evidence.
- Evaluate reusable lessons automatically after the user confirms the final
  model.
- Store long-term Product Family profiles, Knowledge Assertions, and Design
  Lessons in PostgreSQL, with an optional rebuild-only Neo4j projection.

## MCP surfaces

The default `design` surface contains the complete design flow:

- `design_system_status`
- `design_start`
- `design_status`
- `design_knowledge_retrieve`
- `design_record_result`
- `design_confirm`
- `design_lesson_decide`
- `standard_part_providers_get`
- `standard_part_sources_status`
- `standard_part_download_register`

The separate `knowledge-admin` surface manages Product Family onboarding,
knowledge search, Design Lesson supersession or revocation, and explicit
Neo4j projection rebuilds.

## Architecture

Design sessions live under `designs/<design-id>/` as atomic JSON state, one
authoritative `model.FCStd`, optional source snapshots, validation evidence,
outputs, and an optional lesson review card. CAD creation and validation do not
depend on PostgreSQL.

PostgreSQL stores only durable Product Families, Knowledge Assertions, and
Design Lessons. Neo4j is optional, rebuildable, and never authoritative. See
[Architecture and trust boundaries](docs/ARCHITECTURE.md).

## Install and run

Python 3.12 or newer is required.

```bash
python -m pip install ai-mechanical-3dcad-design-agent
mechanical-design init \
  --workspace /path/to/mechanical-design-workspace \
  --actor engineer \
  --organization example-org \
  --design-group example-group
export MECH_DESIGN_WORKSPACE=/path/to/mechanical-design-workspace
mechanical-design-mcp
```

Windows PowerShell:

```powershell
mechanical-design init --workspace "D:\Mechanical Design Workspace" --actor engineer --organization example-org --design-group example-group
$env:MECH_DESIGN_WORKSPACE = "D:\Mechanical Design Workspace"
mechanical-design-mcp
```

Select knowledge administration only when needed:

```bash
MECH_DESIGN_MCP_TOOL_PROFILE=knowledge-admin mechanical-design-mcp
```

The current acceptance target is official FreeCAD 1.1.3. Configure the exact
`FreeCADCmd` path and SHA-256 in the workspace or environment. PostgreSQL is
needed only for durable knowledge operations. The baseline has no pgvector
requirement. Install `ai-mechanical-3dcad-design-agent[neo4j]` only when the
optional relationship projection is wanted.

## Project-owned Agent Skills

- [`mechanical-design`](.agents/skills/mechanical-design/SKILL.md)
- [`freecad-standard-parts`](.agents/skills/freecad-standard-parts/SKILL.md)
- [`freecad-model-validation`](.agents/skills/freecad-model-validation/SKILL.md)

## Operating boundaries

- Generated models, reports, screenshots, databases, credentials, and
  customer-specific evidence stay outside the public repository.
- Local MCP and database services remain bound to loopback interfaces.
- A passed validation report proves only the checks that ran against one exact
  model revision. It is not FEA, manufacturing release, safety certification,
  or legal standards certification.
- Final engineering responsibility remains with the user or an authorized
  engineer.

## Documentation

- [Architecture and trust boundaries](docs/ARCHITECTURE.md)
- [FreeCAD GUI MCP integration](docs/FREECAD_GUI_MCP_INTEGRATION.md)
- [Engineer learning playbook](docs/ENGINEER_LEARNING_PLAYBOOK.md)
- [Database deployment](docs/DATABASE_DEPLOYMENT.md)
- [Windows release acceptance](docs/WINDOWS_RELEASE_ACCEPTANCE.md)
- [Changelog](CHANGELOG.md)

## License

Project source is released under Apache-2.0. External dependencies,
integrations, and assets retain their own licenses; see
[Third-Party Notices](THIRD_PARTY_NOTICES.md).
