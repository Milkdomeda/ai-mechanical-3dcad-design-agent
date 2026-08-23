# AI Mechanical 3DCAD Design Agent

![AI-generated mechanical CAD product showcase](docs/assets/ai-mechanical-design-showcase.gif)

AI Mechanical 3DCAD Design Agent provides deterministic mechanical 3D CAD
workflows, engineering knowledge, validation, and MCP tools for a coding agent
or another compatible MCP client. The core package does not include an embedded
language-model client. Standalone LLM orchestration is not included in version
0.2.0.

The public Python distribution is `ai-mechanical-3dcad-design-agent`. Existing
compatibility surfaces remain stable: the Python package is
`mechanical_design_agent`, the CLI is `mechanical-design`, and the MCP server is
`mechanical-design-mcp`.

## Release boundary

Version 0.2.0 is a coding-agent/MCP-server release. It includes deterministic
workspace bootstrap, product-family configuration, model analysis, engineering
knowledge workflows, standard-part provenance, validation resources, and
package-owned database migrations. It also publishes project-owned agent
instructions and skills for Design Job routing, standard-part selection, and
FreeCAD model validation. It does not bundle a language model, a CAD model
library, generated output, engineering reports, database services, or a FreeCAD
GUI MCP integration.

See [Architecture](docs/ARCHITECTURE.md) for trust boundaries and the
[Engineer learning playbook](docs/ENGINEER_LEARNING_PLAYBOOK.md) for the
operational knowledge workflow. A supported local and evaluation database path
is documented in [Database deployment](docs/DATABASE_DEPLOYMENT.md). The public
[environment template](.env.example) and
[synthetic product-family example](examples/product_families/example-family.json)
contain no production credentials or real project data.

## Capabilities

- Explicit, idempotent workspace initialization with structured diagnostics.
- Empty-workspace first use and explicit product-family creation/selection.
- FreeCADCmd extraction and package-owned FreeCAD scripts for applicable
  headless workflows.
- PostgreSQL/pgvector authoritative storage and a rebuildable Neo4j projection.
- Scoped `DesignContext/v2` retrieval, auditable learning, and design lessons.
- Provider-aware standard-part lookup and provenance.
- FreeCAD model, mechanical-interface, and `AssemblyCompleteness/v2` validation.
- Stable CLI and MCP tool schemas for coding agents and compatible MCP clients.

## Agent instructions and skills

The repository root [`AGENTS.md`](AGENTS.md) defines the recommended operating
boundary for coding agents. Three project-owned skills are included:

- [`mechanical-design-job-workspace`](.agents/skills/mechanical-design-job-workspace/SKILL.md)
  routes every product operation through a controlled Design Job before work on
  a new design, existing model, resumed job, Product Family, or Design Lesson.

- [`freecad-standard-parts`](.agents/skills/freecad-standard-parts/SKILL.md)
  selects and imports reusable standard mechanical components while preserving
  catalog provenance and BOM metadata.
- [`freecad-model-validation`](.agents/skills/freecad-model-validation/SKILL.md)
  validates FCStd and STEP geometry, same-revision evidence, standard-part
  provenance, and mandatory fastener installation contracts.

These skills are source-controlled project capabilities. They do not install
themselves into a user's global agent environment.

### Optional: Superpowers brainstorming

The external [`superpowers:brainstorming`](https://github.com/obra/superpowers)
skill is recommended for turning an incomplete mechanical-design request into
reviewable requirements before modeling. It is optional, not bundled, not
installed, and not required by this project.

- **Codex App:** open **Plugins**, find **Superpowers** in the Coding category,
  and choose **Install**.
- **Codex CLI:** run `/plugins`, search for `superpowers`, and select
  **Install Plugin**.

See [Optional agent workflows](docs/OPTIONAL_AGENT_WORKFLOWS.md) for the scope,
installation boundary, and fallback behavior.

## Architecture

![AI Mechanical 3DCAD Design Agent architecture](docs/assets/ai-mechanical-design-agent-architecture-v2.png)

## FreeCAD integration boundary

A compatible external FreeCAD GUI MCP integration is required for the
recommended interactive FreeCAD workflow. It is not bundled with the core
Python distribution. Interactive viewing, selection, measurement, modeling,
and modification inside the FreeCAD GUI depend on that external integration.

Bootstrap, configuration, knowledge, database, standard-part, and applicable
headless/FreeCADCmd capabilities can operate without the GUI MCP. This project
does not vendor or relicense an external FreeCAD GUI MCP.

See the [FreeCAD GUI MCP integration boundary](docs/FREECAD_GUI_MCP_INTEGRATION.md)
for the exact validated upstream identity, installation boundary, localhost
security contract, and release acceptance matrix.

The certified Windows boundary is Windows 11 x64, CPython 3.12, FreeCAD 1.1.3
x64, and the exact external MCP commit recorded above. See the
[Windows release acceptance](docs/WINDOWS_RELEASE_ACCEPTANCE.md) guide for the
fixed-NTFS, second-volume, wheel-first, protected-host, and portable ZIP
limitations. No other Windows, Python, FreeCAD, architecture, or MCP version is
implied.

## Install

Python 3.12 or newer is required.

```bash
python -m pip install ai-mechanical-3dcad-design-agent
```

## Initialize a workspace

The CLI uses an explicit workspace. Initialization creates only managed
configuration and artifact directories; a new workspace contains zero product
families.

```bash
mechanical-design init --workspace /path/to/mechanical-design-workspace
```

PowerShell uses the same CLI contract and native path syntax:

```powershell
mechanical-design init --workspace C:\path\to\mechanical-design-workspace
$env:MECH_DESIGN_WORKSPACE = "C:\path\to\mechanical-design-workspace"
```

These path examples describe the portable configuration contract. Windows
certification is limited to the exact boundary in the Windows release guide.

## Create and select the first product family

```bash
mechanical-design family create \
  --workspace /path/to/mechanical-design-workspace \
  --organization-id example-org \
  --organization-name "Example Organization" \
  --design-group-id example-design-group \
  --design-group-name "Example Design Group" \
  --family-id example-family \
  --family-name "Example Product Family" \
  --set-default
```

The checked-in synthetic JSON is documentation only. It is never copied,
auto-discovered, selected, or loaded as a runtime default.

## Status, diagnostics, and smoke validation

```bash
mechanical-design status --workspace /path/to/mechanical-design-workspace
mechanical-design doctor --workspace /path/to/mechanical-design-workspace
mechanical-design smoke-fixture --workspace /path/to/mechanical-design-workspace --source /path/to/model.FCStd
```

`status` and `doctor` report the four-state diagnostic model: `ok`, `warning`,
`setup_required`, and `blocked`. Operational commands fail closed with a
structured setup/configuration response when their required capability is not
ready.

## Start the MCP server

Select the workspace explicitly or with the modern environment setting:

```bash
MECH_DESIGN_WORKSPACE=/path/to/mechanical-design-workspace mechanical-design-mcp
```

The MCP server exposes deterministic tools; semantic reasoning remains the
responsibility of the connected coding agent or MCP client.

## Configurable runtime capabilities

PostgreSQL/pgvector and Neo4j are supported configurable runtime capabilities.
The installed package owns and loads their schema migrations. The public
`compose.yaml` can provision loopback-only services for local and evaluation
use; run it with an explicit protected env file, then run
`mechanical-design database bootstrap` from the installed wheel. See the
[database deployment guide](docs/DATABASE_DEPLOYMENT.md) for the exact
`docker compose` flow, image digests, persistence, cleanup, and platform
boundaries.

This Compose path is not a production deployment. It does not define remote
access, production secrets, backups, high availability, monitoring, or a
managed database service. FreeCADCmd may be configured explicitly or discovered
within its documented safe boundary.

## Design and knowledge lifecycle

Before proposing or applying a CAD change, retrieve scoped design knowledge for
the working copy. Existing-model working copies must bind one unique source
model revision; new designs may be source-less. `design_knowledge_retrieve`
builds `DesignContext/v2` and records its receipt. Specialized family knowledge
is available only after explicit family authority; similarity never grants
scope.

The default post-delivery lesson workflow is:

```text
final-model delivery approval -> design_lesson_review_context
-> material/generalizable summary when warranted
-> design_lesson_review_prepare -> one immutable review card
-> engineer approval -> stored-and-retrievable
```

`design_lesson_review_status(retry=True)` may make one bounded retry after an
approved projection/retrieval delay. The hash-bound `design_lesson_stage`,
`design_lesson_staged_get`, `design_lesson_approve`, and
`design_lesson_supersede` surfaces remain an expert/audit compatibility path.

Delivery uses mandatory same-revision gates. FreeCAD model validation emits the
model-detected fastener inventory bound to the same-revision FCStd SHA-256.
`AssemblyCompleteness/v2` requires exactly-once fastened-joint assignment for
every occurrence. Missing, duplicate, unknown, failed, or stale evidence is a
mandatory model and assembly failure. `AssemblyCompleteness/v1` is rejected.

## License boundary

Project-owned source code is released under Apache License 2.0. Dependencies,
external integrations, vendored components, submodules, and assets retain their
own licenses and are not relicensed by this project. The machine-readable
inventory is rendered into the public [Third-Party Notices](THIRD_PARTY_NOTICES.md);
an entry in that index does not relicense a third-party component or CAD asset.
