# OpenCode integration

This guide connects OpenCode to AI Mechanical 3DCAD Design Agent and the
external FreeCAD GUI MCP server. OpenCode is the model-facing orchestrator;
Mechanical Design MCP remains responsible for controlled design state,
knowledge retrieval, Design Job storage, and deterministic lifecycle gates.
FreeCAD GUI MCP provides interactive inspection and CAD editing in a running
FreeCAD application.

An ordinary mechanical-design request belongs in a Design Job under the
configured workspace. It must not create a software Git worktree. Continue the
same design in the same Job and create a new Job only for an independent
requirement.

## Integration map

| Component | Role | Required |
| --- | --- | --- |
| OpenCode and an LLM provider | Conversation, reasoning, and tool selection | Yes |
| AI Mechanical 3DCAD Design Agent | Mechanical Design MCP, CLI, Design Jobs, validation, and knowledge workflows | Yes |
| FreeCAD 1.1.3 | FCStd source of truth and CAD runtime | Yes for CAD work |
| `neka-nat/freecad-mcp` | External stdio MCP server and local FreeCAD RPC addon | Yes for the recommended interactive workflow |
| PostgreSQL | Durable Product Families, Knowledge Assertions, and Design Lessons | Optional |
| Neo4j Community Edition | Rebuildable relationship projection | Optional |
| FreeCAD Fasteners and Gears workbenches | Parametric standard-part providers | Optional |
| STEP.parts | External standard-part discovery and download provider | Optional |
| Superpowers `brainstorming` | Optional requirement-discovery aid | Optional |

The project does not redistribute OpenCode, FreeCAD, FreeCAD GUI MCP, database
servers, workbenches, or downloaded catalog models. Their licenses and service
terms remain independent. See [Third-Party Notices](../THIRD_PARTY_NOTICES.md)
and [FreeCAD GUI MCP Integration Boundary](FREECAD_GUI_MCP_INTEGRATION.md).

## Supported boundary

- Python 3.12 or newer is required.
- macOS and native Windows are supported project platforms.
- The current CAD acceptance target is official FreeCAD 1.1.3.
- Both MCP servers use stdio. FreeCAD GUI MCP talks to the FreeCAD addon only
  through `127.0.0.1:9875` with remote access disabled.
- Run OpenCode, Mechanical Design MCP, and FreeCAD GUI MCP on the same host.
- OpenCode officially recommends WSL for its general Windows experience, but a
  WSL process and native Windows FreeCAD do not share the same loopback network
  boundary. Use native Windows OpenCode for this integration. A WSL-to-Windows
  or remote bridge requires a separate security and compatibility review.
- A passed validation report is evidence for the checks that ran against one
  exact model revision. It is not FEA, manufacturing release, certification, or
  approval by an engineer.

## 1. Install OpenCode and configure a model

Follow the current [OpenCode installation guide](https://opencode.ai/docs/).
Common installation choices are:

macOS:

```bash
brew install anomalyco/tap/opencode
```

Windows PowerShell, using one native installer:

```powershell
choco install opencode
# or
scoop install opencode
# or
npm install -g opencode-ai
```

Start OpenCode and run `/connect` to configure an LLM provider. Select a model
that can reliably call tools and reason over mechanical requirements. Provider
credentials belong in OpenCode's private credential storage or environment;
never add them to this repository or a Design Job.

## 2. Install the Mechanical Design Agent

Install the released package in a dedicated Python environment. Activating that
environment before starting OpenCode makes the two project commands available
on `PATH`.

macOS:

```bash
python3.12 -m venv /path/to/mechanical-design-venv
source /path/to/mechanical-design-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ai-mechanical-3dcad-design-agent
mechanical-design --version
python -c "import shutil; print(shutil.which('mechanical-design-mcp'))"
```

Windows PowerShell:

```powershell
py -3.12 -m venv "C:\path\to\mechanical-design-venv"
& "C:\path\to\mechanical-design-venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install ai-mechanical-3dcad-design-agent
mechanical-design --version
python -c "import shutil; print(shutil.which('mechanical-design-mcp'))"
```

For development against a source checkout, replace the final package install
with `python -m pip install -e .` from the repository root. Do not use an
editable source checkout as an end-user production deployment.

## 3. Create one stable Design Job Workspace

Choose a persistent directory outside the Git repository. All Design Jobs and
their governed FCStd files, validation evidence, outputs, and lesson review
cards remain grouped under this workspace.

macOS:

```bash
mechanical-design init \
  --workspace /path/to/mechanical-design-workspace \
  --actor engineer \
  --organization example-org \
  --design-group example-group
export MECH_DESIGN_WORKSPACE=/path/to/mechanical-design-workspace
export MECH_DESIGN_MCP_TOOL_PROFILE=design
mechanical-design status --workspace "$MECH_DESIGN_WORKSPACE"
```

Windows PowerShell:

```powershell
mechanical-design init `
  --workspace "D:\Mechanical Design Workspace" `
  --actor engineer `
  --organization example-org `
  --design-group example-group
$env:MECH_DESIGN_WORKSPACE = "D:\Mechanical Design Workspace"
$env:MECH_DESIGN_MCP_TOOL_PROFILE = "design"
mechanical-design status --workspace $env:MECH_DESIGN_WORKSPACE
```

Use the `design` MCP tool profile for ordinary design sessions. Start a separate
`knowledge-admin` MCP process only when an authorized operator is onboarding a
Product Family or administering durable knowledge. Keeping administrative tools
out of normal sessions reduces tool-selection noise and accidental operations.

Do not point `MECH_DESIGN_WORKSPACE` at the source repository, an OpenCode
configuration directory, or an OpenCode session directory.

## 4. Install FreeCAD and FreeCAD GUI MCP

Install official FreeCAD 1.1.3 for the host platform. If headless validation or
package diagnostics need `FreeCADCmd`, set `MECH_DESIGN_FREECADCMD` to the exact
executable in the operator's private environment.

The currently audited FreeCAD GUI MCP identity is:

- source: [`neka-nat/freecad-mcp`](https://github.com/neka-nat/freecad-mcp)
- commit: `7667e272e1db669ff61dd5411fb4f622691f2dbc`
- transport: stdio to OpenCode, local RPC to FreeCAD
- license: MIT
- distribution: external; not bundled with this project

The upstream project has inconsistent declared and lock-file versions. Use the
exact audited commit as the compatibility identity rather than the `0.1.19` or
`0.1.17` metadata value.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then clone
the external MCP into a separate directory:

```bash
git clone https://github.com/neka-nat/freecad-mcp.git /path/to/freecad-mcp
git -C /path/to/freecad-mcp checkout --detach 7667e272e1db669ff61dd5411fb4f622691f2dbc
git -C /path/to/freecad-mcp status --short
```

The last command must produce no output. Keep this checkout independent from the
Mechanical Design Agent repository and Python environment.

Copy `addon/FreeCADMCP` from that checkout into the FreeCAD addon directory:

- macOS FreeCAD 1.1:
  `~/Library/Application Support/FreeCAD/v1-1/Mod/FreeCADMCP`
- Windows:
  `%APPDATA%\FreeCAD\Mod\FreeCADMCP`

macOS example:

```bash
mkdir -p "$HOME/Library/Application Support/FreeCAD/v1-1/Mod"
cp -R /path/to/freecad-mcp/addon/FreeCADMCP \
  "$HOME/Library/Application Support/FreeCAD/v1-1/Mod/"
```

Windows PowerShell example:

```powershell
$freecadMod = Join-Path $env:APPDATA "FreeCAD\Mod"
New-Item -ItemType Directory -Force -Path $freecadMod | Out-Null
Copy-Item -Recurse -Force `
  "C:\path\to\freecad-mcp\addon\FreeCADMCP" `
  $freecadMod
```

Restart FreeCAD, select **MCP Addon**, and choose **Start RPC Server**. Do not
enable **Remote Connections**. Optional auto-start is acceptable only while the
server remains bound to loopback.

## 5. Configure the MCP servers in OpenCode

OpenCode merges global, custom, and project configuration. Machine paths and
workspace locations should stay in a private config outside the repository.
Set `OPENCODE_CONFIG` to that file before starting OpenCode, or merge the same
entries into the user's global OpenCode configuration.

The command paths below are placeholders. Replace them with absolute paths on
the target machine. Using an absolute Mechanical Design MCP executable avoids
dependence on shell activation when OpenCode restarts the server.

### Current stable OpenCode configuration

Save the following as a private `opencode-mechanical-design.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mechanical-design": {
      "type": "local",
      "command": [
        "/path/to/mechanical-design-venv/bin/mechanical-design-mcp"
      ],
      "enabled": true,
      "environment": {
        "MECH_DESIGN_WORKSPACE": "{env:MECH_DESIGN_WORKSPACE}",
        "MECH_DESIGN_MCP_TOOL_PROFILE": "design"
      },
      "timeout": 10000
    },
    "freecad": {
      "type": "local",
      "command": [
        "uv",
        "--directory",
        "/path/to/freecad-mcp",
        "run",
        "freecad-mcp"
      ],
      "enabled": true,
      "timeout": 10000
    }
  }
}
```

On Windows, use JSON-escaped paths:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mechanical-design": {
      "type": "local",
      "command": [
        "C:\\path\\to\\mechanical-design-venv\\Scripts\\mechanical-design-mcp.exe"
      ],
      "enabled": true,
      "environment": {
        "MECH_DESIGN_WORKSPACE": "{env:MECH_DESIGN_WORKSPACE}",
        "MECH_DESIGN_MCP_TOOL_PROFILE": "design"
      },
      "timeout": 10000
    },
    "freecad": {
      "type": "local",
      "command": [
        "uv",
        "--directory",
        "C:\\path\\to\\freecad-mcp",
        "run",
        "freecad-mcp"
      ],
      "enabled": true,
      "timeout": 10000
    }
  }
}
```

Select the private configuration when launching OpenCode:

macOS:

```bash
export OPENCODE_CONFIG=/path/to/opencode-mechanical-design.jsonc
cd /path/to/ai-mechanical-design-agent
opencode
```

Windows PowerShell:

```powershell
$env:OPENCODE_CONFIG = "C:\path\to\opencode-mechanical-design.jsonc"
Set-Location "C:\path\to\ai-mechanical-design-agent"
opencode
```

See the official [OpenCode configuration](https://opencode.ai/docs/config/)
and [MCP server](https://opencode.ai/docs/mcp-servers/) references for config
precedence and local-server fields.

### OpenCode V2 configuration difference

OpenCode V2 nests server names under `mcp.servers` and replaces `enabled` with
`disabled`. Do not mix the two schemas. The equivalent V2 shape is:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "mechanical-design": {
        "type": "local",
        "command": [
          "/path/to/mechanical-design-venv/bin/mechanical-design-mcp"
        ],
        "environment": {
          "MECH_DESIGN_WORKSPACE": "{env:MECH_DESIGN_WORKSPACE}",
          "MECH_DESIGN_MCP_TOOL_PROFILE": "design"
        },
        "disabled": false,
        "timeout": 10000
      },
      "freecad": {
        "type": "local",
        "command": [
          "uv",
          "--directory",
          "/path/to/freecad-mcp",
          "run",
          "freecad-mcp"
        ],
        "disabled": false,
        "timeout": 10000
      }
    }
  }
}
```

Check the [OpenCode V2 MCP documentation](https://opencode.ai/v2/docs/mcp-servers/)
before adopting V2 because it is a separate configuration contract.

## 6. Project instructions and Agent Skills

Start OpenCode from the AI Mechanical 3DCAD Design Agent repository root. It
will discover the existing [`AGENTS.md`](../AGENTS.md) and these project-owned
skills automatically:

- [`mechanical-design`](../.agents/skills/mechanical-design/SKILL.md)
- [`freecad-standard-parts`](../.agents/skills/freecad-standard-parts/SKILL.md)
- [`freecad-model-validation`](../.agents/skills/freecad-model-validation/SKILL.md)

OpenCode natively discovers `.agents/skills/<name>/SKILL.md`; do not duplicate
them under `.opencode/skills`. Do not run `/init` over this repository unless
you intentionally want to review changes to the existing `AGENTS.md`.

The instructions help the model choose the correct operation, while the
Mechanical Design MCP software enforces controlled state transitions and
evidence bindings. Agent instructions are not a substitute for deterministic
gates.

## 7. Optional third-party capabilities

### Durable engineering knowledge

CAD modeling and validation do not require a database. PostgreSQL is needed
only for durable Product Families, Knowledge Assertions, and Design Lessons.
Neo4j is an optional rebuildable relationship projection.

Use the repository's loopback-only Docker Compose deployment and private
environment file. Follow [Knowledge database deployment](DATABASE_DEPLOYMENT.md)
rather than copying credentials into OpenCode configuration. Install Neo4j
driver support only when the projection is wanted:

```bash
python -m pip install "ai-mechanical-3dcad-design-agent[neo4j]"
```

### Standard-part providers

The preferred provider order is:

1. FreeCAD Fasteners Workbench
2. FreeCAD Gears Workbench
3. [STEP.parts](https://www.step.parts/)
4. an explicitly configured verified FCStd/STEP catalog

Install FreeCAD workbenches through the FreeCAD Addon Manager. The release
inventory records Fasteners Workbench `0.5.64` and Gears Workbench `1.3` as the
audited provider identities. Provider availability is configuration, not a
guarantee that a requested part exists.

Inspect provider readiness with:

```bash
mechanical-design standard-parts providers
mechanical-design standard-parts status \
  --workspace "$MECH_DESIGN_WORKSPACE"
```

Downloaded catalog models retain the provider's and individual model's terms.
Preserve source URL, part identity, manufacturer, standard, license, and SHA-256
metadata. A network failure is not a valid catalog miss.

### Optional Superpowers brainstorming

The project does not bundle or require Superpowers. A user may install its
`brainstorming` skill as an optional aid for incomplete or exploratory design
requirements. Follow the upstream
[Superpowers for OpenCode](https://github.com/obra/superpowers/blob/main/docs/README.opencode.md)
instructions. Its current OpenCode plugin form is:

```jsonc
{
  "plugin": [
    "superpowers@git+https://github.com/obra/superpowers.git"
  ]
}
```

Merge that field into the private OpenCode configuration and restart OpenCode.
Installation of Superpowers is a user choice; its brainstorming output remains
subject to the project's requirement approval, provenance, and validation
rules.

## 8. Start and verify the integration

Use this startup order:

1. Export `MECH_DESIGN_WORKSPACE` and any private database settings.
2. Start optional PostgreSQL and Neo4j services if durable knowledge is needed.
3. Start FreeCAD 1.1.3.
4. Start the FreeCAD MCP addon RPC server and confirm remote access is disabled.
5. Start OpenCode from the project repository root using the private config.
6. Confirm both `mechanical-design` and `freecad` MCP servers are connected.

OpenCode can list configured MCP servers with:

```bash
opencode mcp list
```

Then ask OpenCode to perform a read-only readiness check:

> Read the project AGENTS.md and available Agent Skills. Use the
> mechanical-design system-status tool and the FreeCAD list-documents tool.
> Report the configured Design Job Workspace, MCP connection state, and any
> setup-required diagnostics. Do not create or modify a model.

For the first bounded modeling smoke test, use a disposable requirement and
confirm all of the following:

- the design is created under the configured workspace's `designs/<design-id>/`
- no Git branch or worktree is created for the mechanical design
- one authoritative `model.FCStd` remains inside the Design Job
- the FreeCAD document can be listed and inspected through FreeCAD MCP
- applicable knowledge retrieval records a completed, no-match, or unavailable
  outcome rather than being silently omitted
- validation evidence is bound to the final FCStd SHA-256
- OpenCode does not report completion before required validation passes

Delete or archive the disposable Design Job through an explicitly reviewed
workspace operation; never remove it with a broad recursive command.

## Troubleshooting

### `mechanical-design-mcp` is not found

Use the absolute executable path from the project virtual environment in the
OpenCode MCP command. Confirm that `shutil.which("mechanical-design-mcp")`
returns that path from the same account that starts OpenCode.

### Mechanical Design MCP starts but reports `setup_required`

Confirm `MECH_DESIGN_WORKSPACE` exists and was created by `mechanical-design
init`. Run:

```bash
mechanical-design status --workspace /path/to/mechanical-design-workspace
```

Resolve the reported component instead of bypassing the readiness result.

### OpenCode cannot connect to FreeCAD

Confirm all of these conditions:

- FreeCAD is running.
- **MCP Addon** is installed from the same audited checkout.
- **Start RPC Server** has been selected.
- the server is listening only on `127.0.0.1:9875`
- the `uv --directory ... run freecad-mcp` command works outside OpenCode
- OpenCode and FreeCAD are running on the same native host

Do not solve a connection error by enabling unaudited remote access.

### Skills are not visible

Start OpenCode inside the repository and confirm each skill has a valid
`.agents/skills/<name>/SKILL.md` path. OpenCode searches upward only to the Git
worktree boundary. The public repository, rather than its private parent
workspace, must be the active project.

### Too many tools or inconsistent tool choice

Keep `MECH_DESIGN_MCP_TOOL_PROFILE=design` for ordinary work. Disable unrelated
MCP servers for the mechanical-design session. OpenCode notes that every MCP
server adds tool descriptions to model context, so enabling fewer relevant
servers improves focus.

### Database is unavailable

The current design may continue with an explicit unavailable knowledge receipt
unless the user requires a named knowledge item. Database failure must not be
reported as a knowledge or Product Family miss. The completed CAD model remains
separate from an uncompleted Design Lesson publication attempt.

## Security and publication checklist

- Keep FreeCAD RPC, PostgreSQL, and Neo4j bound to loopback interfaces.
- Keep `remote_enabled=false` for FreeCAD GUI MCP.
- Do not commit `opencode-mechanical-design.jsonc`, environment files, API keys,
  credentials, usernames, absolute machine paths, or runtime data.
- Do not give OpenCode access to customer folders outside the selected Design
  Job Workspace and explicitly requested source models.
- Do not execute untrusted code embedded in CAD files.
- Keep generated FCStd/STEP files, screenshots, BOM exports, validation output,
  databases, and Design Lessons out of the public source repository.
- Revalidate the exact OpenCode release, FreeCAD release, FreeCAD MCP commit,
  and host platform before claiming a new compatibility combination.
