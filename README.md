# AI Mechanical 3DCAD Design Agent

![AI-generated mechanical CAD product showcase](docs/assets/ai-mechanical-design-showcase.gif)

AI Mechanical 3DCAD Design Agent is an open-source framework for using coding
agents and large language models to design mechanical parts and assemblies in
FreeCAD. It combines AI reasoning with deterministic workspace management,
standard-part selection, engineering knowledge, model validation, and
traceable design results.

The project provides the `mechanical-design` CLI and `mechanical-design-mcp`
server. It works with a compatible coding agent and an external FreeCAD GUI MCP
connection. The core package does not include an embedded language-model
client, and it does not replace engineering review.

## What it can do

- Turn natural-language mechanical requirements into reviewable design intent
  and controlled FreeCAD work.
- Create a new design or resume an existing design in a stable workspace
  directory, including read-only snapshots of supplied source CAD.
- Use Product Family knowledge, prior Design Lessons, and approved engineering
  context when they are available.
- Find and register bearings, fasteners, gears, motors, profiles, guide rails,
  and other standard components with source and SHA-256 provenance.
- Drive interactive FreeCAD modeling through an external GUI MCP and use
  FreeCADCmd for applicable extraction and verification tasks.
- Validate geometry, dimensions, placements, interfaces, assemblies,
  fasteners, BOM consistency, and exact-revision evidence.
- Correct validation failures within the approved design intent and record the
  final FCStd model with its validation report and visual evidence.
- Review completed work and publish reusable Design Lessons into the governed
  knowledge system.

## Featured tools and capabilities

| Capability | Purpose |
| --- | --- |
| `design_start` | Creates or safely resumes one design workspace after the user approves the design intent. |
| `design_knowledge_retrieve` | Retrieves applicable Product Family knowledge and Design Lessons without blocking ordinary design when no match exists. |
| Standard-part tools | Inspect configured providers and register downloaded components with catalog provenance. |
| `design_record_result` | Accepts a result only when the FCStd hash matches the validation report and Markdown/PNG evidence. |
| Product Family workflow | Learns a product group, compares models, and publishes reviewed engineering knowledge. |
| Design Lesson workflow | Filters, reviews, and publishes reusable lessons from completed design work. |
| Governed workflow | Adds Design Jobs, approval envelopes, scope-bound obligations, lifecycle evidence, and delivery approval when stronger audit control is required. |

The default `design` MCP profile exposes seven tools for ordinary design work.
Additional `governed`, `family-knowledge`, `maintenance`, and `all` profiles
keep specialized or administrative tools out of the model's normal tool list.

Three project-owned Agent Skills are included:

- [`freecad-standard-parts`](.agents/skills/freecad-standard-parts/SKILL.md)
- [`freecad-model-validation`](.agents/skills/freecad-model-validation/SKILL.md)
- [`mechanical-design-job-workspace`](.agents/skills/mechanical-design-job-workspace/SKILL.md), for the optional governed workflow

The external
[`superpowers:brainstorming`](https://github.com/obra/superpowers) skill is an
optional recommendation for requirement discovery. It is not bundled, not
installed automatically, and not required.

## Architecture

![AI Mechanical 3DCAD Design Agent architecture](docs/assets/ai-mechanical-design-agent-architecture-v2.png)

- The coding agent or LLM interprets requirements, makes engineering
  decisions, and selects the appropriate tools.
- The Mechanical Design MCP manages design state, knowledge, standard-part
  provenance, validation bindings, and result records.
- The external FreeCAD GUI MCP performs interactive inspection and CAD edits in
  the user's running FreeCAD application.
- FCStd files and evidence remain in the selected workspace. PostgreSQL is the
  authoritative store for optional governed knowledge and lifecycle data;
  Neo4j is a rebuildable relationship projection.

See [Architecture and trust boundaries](docs/ARCHITECTURE.md) for the complete
component and authority model.

## Design process

```text
Mechanical requirement
        ↓
Clarify function, interfaces, dimensions, loads, materials, and constraints
        ↓
Present the design intent and obtain user approval
        ↓
Create or resume the design workspace
        ↓
Retrieve applicable knowledge and evaluate standard components
        ↓
Model and inspect the part or assembly in FreeCAD
        ↓
Validate the exact FCStd revision and correct detected failures
        ↓
Record the final model, report, and visual evidence
        ↓
Optionally review and publish reusable Design Lessons
```

The depth of each activity follows the engineering problem. A simple mounting
plate and a multi-component mechanism use the same design principles without
being forced through identical analysis work.

## Install and run

Python 3.12 or newer is required.

```bash
python -m pip install ai-mechanical-3dcad-design-agent
```

Initialize a workspace on macOS or Linux:

```bash
mechanical-design init --workspace /path/to/mechanical-design-workspace
export MECH_DESIGN_WORKSPACE=/path/to/mechanical-design-workspace
mechanical-design-mcp
```

On Windows PowerShell:

```powershell
mechanical-design init --workspace "D:\Mechanical Design Workspace"
$env:MECH_DESIGN_WORKSPACE = "D:\Mechanical Design Workspace"
mechanical-design-mcp
```

The default MCP profile is `design`. Select another profile only for the
corresponding workflow:

```bash
MECH_DESIGN_MCP_TOOL_PROFILE=family-knowledge mechanical-design-mcp
```

A compatible external FreeCAD GUI MCP is required for the recommended
interactive FreeCAD workflow, including viewing, selection, measurement,
modeling, and modification. It is not bundled with the core Python
distribution. PostgreSQL/pgvector and Neo4j are optional for the default design
workflow and are required for the applicable knowledge, Product Family, and
governed capabilities.

The databases are configurable runtime capabilities. The public `compose.yaml`
supports a loopback-only `docker compose` deployment for local and evaluation
use; it is not a production deployment.

Use the public [environment template](.env.example) and follow:

- [FreeCAD GUI MCP integration](docs/FREECAD_GUI_MCP_INTEGRATION.md)
- [Database deployment](docs/DATABASE_DEPLOYMENT.md)
- [Windows release acceptance](docs/WINDOWS_RELEASE_ACCEPTANCE.md)

## Operating boundaries

- Supports macOS and Windows with Python 3.12 or newer. The current release
  acceptance target is official FreeCAD 1.1.3.
- The FreeCAD GUI MCP, language model, database services, CAD catalogs, and
  generated design artifacts are not bundled with the Python package.
- Local MCP and database services should remain bound to loopback interfaces;
  remote access requires a separate security review.
- Product Family association and durable knowledge services are optional for
  ordinary design work.
- Generated FCStd/STEP models, reports, screenshots, databases, credentials,
  and customer-specific evidence remain outside the public source repository.
- Validation proves the checks that ran against one exact model revision. It is
  not finite-element analysis, manufacturing release, safety certification, or
  legal confirmation of standards compliance.
- Final engineering approval remains the responsibility of the user or an
  authorized engineer.

## Documentation

- [Architecture and trust boundaries](docs/ARCHITECTURE.md)
- [FreeCAD GUI MCP integration](docs/FREECAD_GUI_MCP_INTEGRATION.md)
- [Design Job workspaces](docs/DESIGN_JOB_WORKSPACES.md)
- [Engineer learning playbook](docs/ENGINEER_LEARNING_PLAYBOOK.md)
- [Database deployment](docs/DATABASE_DEPLOYMENT.md)
- [Optional agent workflows](docs/OPTIONAL_AGENT_WORKFLOWS.md)
- [Windows release acceptance](docs/WINDOWS_RELEASE_ACCEPTANCE.md)
- [Changelog](CHANGELOG.md)

## License

Project-owned source code is released under the Apache License 2.0. External
dependencies, integrations, and assets retain their own licenses; see
[Third-Party Notices](THIRD_PARTY_NOTICES.md).
