# Windows release acceptance

## Recorded boundary and v0.3 release gate

The earlier installed-wheel and GUI integration acceptance was exercised on
the exact Windows release boundary below:

- Windows 11 x64;
- CPython 3.12;
- FreeCAD 1.1.3 x64 and FreeCADCmd 1.1.3 x64; and
- external FreeCAD GUI MCP commit
  `7667e272e1db669ff61dd5411fb4f622691f2dbc`.

This is an exact evidence-backed historical boundary, not a claim for other
Windows, Python, FreeCAD, architecture, MCP commit, transport, or deployment
versions. Version 0.3.0 adds Design Job storage and lifecycle behavior. It is
not release-certified on Windows until the protected-host v0.3 Job acceptance
described below passes and its privacy-scanned evidence is recorded.

## Installation and workspace

Use a wheel-first installation in a clean CPython 3.12 environment. A portable
ZIP is a source-transfer convenience only: it does not bundle Python, FreeCAD,
database services, the external GUI MCP, or host configuration, and it does not
replace clean installed-wheel acceptance.

Runtime workspaces must be explicit, fixed local NTFS paths. Protected release
acceptance also requires a second fixed NTFS volume so file identity,
cross-volume behavior, atomic operations, locking, Unicode and space paths, and
reparse rejection are exercised on distinct volumes. Network, mapped, and
reparse-point roots are rejected. Public CI cannot substitute for this
dedicated protected host procedure.

Every mechanical-design and Product Family operation must create or reuse a
directory below the configured workspace `jobs` root. The protected run must
create one new-design Job and one existing-model Job, resume the first Job
without duplicating it, create another Job for an explicitly independent
requirement, and prove that none of those Job trees contains `.git`. FCStd
working files, validation and delivery evidence, onboarding knowledge, and
Design Lessons must remain in their correct originating Job.

FreeCADCmd is selected by explicit configuration or bounded local discovery,
and Job-CAD remains blocked until the selected official 1.1.3 x64 executable
matches the reviewed `MECH_DESIGN_FREECADCMD_SHA256`. The installed package
does not search arbitrary drives, accept ambiguous candidates, or trust version
output without the pinned file identity and digest. Native signature/digest
acceptance on the protected Windows host remains mandatory.

## External capabilities

PostgreSQL/pgvector and Neo4j are configurable external capabilities. Their
services are not bundled or provisioned, but their schemas are migrated by the
installed Mechanical Design Agent. Protected acceptance uses isolated,
disposable test targets and requires cleanup to succeed.

FreeCAD GUI MCP is external, required for the recommended interactive FreeCAD
workflow, not bundled, not project-owned, and not backend-probed by Mechanical
Design MCP. The validated integration uses the exact commit above over stdio
with its addon RPC restricted to loopback.

The local/evaluation Compose path is public but not production. D3 acceptance
used Windows 11 x64, CPython 3.12 x64, Docker Desktop 4.87.0, Docker Engine
29.7.2, Docker Compose 5.4.0, WSL 2.7.12, native Linux/amd64 containers, and
two distinct fixed NTFS volumes. It proved a clean installed wheel, exact image
digests, loopback-only ports, PostgreSQL and Neo4j first/second bootstrap,
Unicode and space paths, and exact cleanup. Other Docker, WSL, engine, Compose,
architecture, remote-access, and production configurations are not implied.

See [Database deployment](DATABASE_DEPLOYMENT.md) for the supported public
local/evaluation procedure. Compose provisions disposable or persistent local
services; the installed Mechanical Design Agent retains migration ownership.

## Public and protected checks

Public Windows CI runs non-interactive frozen dependency, offline test,
artifact, installed-wheel, and public-boundary checks on `windows-2025`. It
does not receive database credentials and does not connect pull-request code to
the interactive host.

W1 through W4 protected acceptance runs only on the dedicated protected host.
It validates two-volume Windows filesystem behavior, clean installed-wheel and
FreeCADCmd behavior, disposable PostgreSQL/pgvector and Neo4j integration, and
the exact external FreeCAD GUI MCP synthetic workflow. The procedure uses only
UUID-owned temporary resources. Any test, cleanup, source-integrity, or privacy
failure makes the overall result fail.

Raw logs remain local and sensitive. Only schema-limited, privacy-scanned
summaries may leave the host.

## Version 0.3.0 Design Job acceptance

Run `tests/test_design_job_freecad_live.py` from the clean installed-wheel
environment with its explicit opt-in variables and one non-sensitive FCStd or
STEP fixture. Then run the protected PostgreSQL/Neo4j and external FreeCAD GUI
MCP suites. Acceptance requires:

- exact migrations `001` through `015` from the installed package;
- same-Job resume and explicitly independent Job creation;
- new-design and existing-model FCStd working copies under the selected fixed
  NTFS workspace, with source hashes and no nested `.git`;
- same-revision FreeCAD validation and delivery evidence;
- Product Family onboarding publication in its onboarding Job;
- Design Lesson publication in its originating mechanical Job;
- PostgreSQL authority and a rebuildable Neo4j projection; and
- exact cleanup of UUID-owned processes, databases, directories, and temporary
  resources.

Until every item passes on the protected Windows boundary, documentation and
release notes must describe Windows support as an implementation target with
automated portability coverage, not as completed v0.3 live certification.
