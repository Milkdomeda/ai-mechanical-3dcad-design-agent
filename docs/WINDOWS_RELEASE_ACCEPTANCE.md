# Windows release acceptance

## FreeCAD startup regression

The `Windows FreeCAD startup regression` workflow installs the candidate wheel
outside the checkout and runs real MCP stdio requests on `windows-2025` x64 and
Windows 11 ARM with x64 Python and FreeCAD under Windows compatibility. It uses
the SHA-256-pinned official FreeCAD 1.1.3 x64 archive. These are distinct platform
scopes; the ARM runner does not constitute native ARM FreeCAD certification.

The required candidate matrix covers two fixed NTFS volumes, spaces, Unicode,
ordinary and extended local path inputs, and a parent PATH without FreeCAD.
Each cell requires readiness, `design_start`, a nonempty inspectable FCStd,
same-Job resume, reopening in FreeCAD, and STEP normalization with preserved
source bytes and a valid 6000 mm³ test solid. Synthetic output and diagnostics
are uploaded as Actions artifacts. The separate v0.8.0 baseline is diagnostic
only; no candidate failure is allowed or converted into a skip.

FreeCAD version probes and CAD execution share the selected installation's
bounded environment. Filesystem canonicalization and identity checks retain
extended paths internally; only the external FreeCAD boundary translates safe
local drive spellings. A readiness result is still a version/identity probe;
the native regression verifies actual document creation and reopening.

## Full platform acceptance

Windows is a first-class runtime target. Release acceptance uses Windows 11
x64, Python 3.12 or newer, official FreeCAD 1.1.3 x64, Git, and Docker Desktop
with Linux containers when knowledge integration is tested.

## Required checks

1. Build the wheel and source distribution from a clean checkout.
2. Install the wheel in a new virtual environment on a fixed NTFS volume whose
   path contains spaces and non-ASCII characters.
3. Run the supported offline test suite and verify CLI and MCP startup.
4. Initialize a workspace, start a new design, record exact-hash validation,
   confirm the final model with ordinary natural language, and exercise both
   Design Lesson outcomes: no useful lesson and a review card.
5. Verify that database unavailability does not block CAD creation, validation,
   correction, completion, or final confirmation.
6. When Docker Desktop is available, bootstrap a fresh knowledge database,
   publish Product Family Knowledge and one approved Design Lesson, synchronize
   Neo4j, and repeat bootstrap to prove idempotency.
7. Run the installed FreeCAD validation resources against official FreeCAD
   1.1.3 and inspect the generated visual evidence.
8. Verify path containment, reparse-point rejection, exclusive locking,
   cleanup, UTF-8, and long-path behavior.

The repository provides:

- `scripts/windows_release_acceptance.ps1`
- `scripts/windows_database_deployment_acceptance.ps1`
- `.github/workflows/windows.yml`

## Knowledge service boundary

The accepted local/evaluation setup uses Docker Desktop, PostgreSQL with
pgvector, and Neo4j on loopback ports. Live database checks require explicit
test credentials and isolated databases. They must never target production
data.

Earlier Windows evidence used Docker Desktop 4.87.0, Docker Engine 29.7.2,
Docker Compose 5.4.0, and WSL 2.7.12 with Linux/amd64 containers. Those versions
record evidence for that run and are not a promise about every future Docker
release.

## Release result

Record:

- operating system build, Python, FreeCAD, Docker, and package versions;
- hashes of the installed package, FreeCAD executable, model, and validation
  evidence;
- passed, skipped, and failed checks with precise reasons;
- any remaining platform limitation as a release blocker.

A successful run verifies the software checks executed on that environment. It
does not certify the mechanical design or production deployment.
