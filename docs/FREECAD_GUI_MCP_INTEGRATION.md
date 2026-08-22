# FreeCAD GUI MCP Integration Boundary

## Position in the system

FreeCAD GUI MCP is a required external integration for the recommended
interactive FreeCAD workflow. It is not bundled with AI Mechanical 3DCAD Design
Agent, is not a backend dependency of Mechanical Design MCP, and is not project-
owned code.

The external integration supplies interactive viewing, selection, measurement,
modeling, and modification inside the running FreeCAD GUI. Workspace bootstrap,
configuration, knowledge, database, standard-part, and applicable headless
FreeCADCmd capabilities do not require this second MCP server. Mechanical Design
MCP does not discover, launch, stop, probe, or validate it at runtime.

## Audited upstream identity

The release boundary records these upstream facts without normalization:

| Fact | Audited value |
| --- | --- |
| Official source | [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) |
| Commit | `7667e272e1db669ff61dd5411fb4f622691f2dbc` |
| Tag | no approved tag |
| Declared `pyproject.toml` version | `0.1.19` |
| Committed `uv.lock` project version | `0.1.17` |
| License | MIT |
| Copyright | Copyright 2025 Shirokuma (k tanaka) |
| Distribution relationship | external integration; not distributed by this project |

The declared and committed-lock versions are an upstream metadata inconsistency.
They are retained as two independent audit facts. This project does not call the
approved commit version `0.1.19` or `0.1.17` as a normalized compatibility
identity.

The upstream [MIT license](https://github.com/neka-nat/freecad-mcp/blob/7667e272e1db669ff61dd5411fb4f622691f2dbc/LICENSE)
and copyright ownership remain with the upstream project. The project Apache-2.0
license does not apply to FreeCAD GUI MCP. See the
[Third-Party Notices](../THIRD_PARTY_NOTICES.md) for the release inventory.

## Installation and configuration boundary

Install from a clean checkout of the exact audited commit in a separate
environment outside the Mechanical Design Agent checkout. Follow the upstream
installation instructions for that checkout; do not copy its package, addon, or
source into this repository or the Mechanical Design Agent environment.

Use generic external locations such as `/path/to/freecad-mcp-checkout` and a
separate environment selected by the operator. Before release validation, the
checkout must have the approved commit as `HEAD` and an empty Git status. The
release harness receives the checkout, executable, FreeCAD addon, and settings
locations explicitly. It never falls back to a private vendor checkout.

Upstream installation documentation may describe other platforms, including
Windows. Those instructions are a reference to the upstream project only; they
are not a Mechanical Design Agent compatibility claim.

## Validated security boundary

The intended MCP transport is stdio. Communication with the FreeCAD addon is
restricted to `127.0.0.1:9875`, with `remote_enabled=false`. The release
acceptance harness rejects non-loopback hosts, forwarded endpoints, and remote
access settings. Mechanical Design MCP does not create a network dependency on
the external MCP server.

## Release compatibility acceptance target

| Platform | FreeCAD | FreeCAD GUI MCP identity | RPC boundary | Status |
| --- | --- | --- | --- | --- |
| macOS | FreeCAD 1.1.1 | commit `7667e272e1db669ff61dd5411fb4f622691f2dbc` | loopback-only | historical evidence only; not release-approved |
| macOS | FreeCAD 1.1.3 arm64 | commit `7667e272e1db669ff61dd5411fb4f622691f2dbc` | loopback-only | passed; current release-approved baseline |
| Windows 11 x64 | FreeCAD 1.1.3 x64 | commit `7667e272e1db669ff61dd5411fb4f622691f2dbc` | loopback-only | passed |

The historical macOS acceptance exercised MCP startup, localhost-only FreeCAD
RPC, document access, synthetic temporary geometry creation, readback,
measurement, modification, and the applicable Mechanical Design validation
path. It did not open or modify real CAD data. FreeCAD's
[1.1.3 security release](https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.3)
states that earlier releases are affected by one or more file-handling security
issues. Therefore the 1.1.1 result is retained as evidence only and does not
satisfy the public version 0.1.0 macOS interactive-release gate. That gate
requires the same clean-host workflow to pass with the exact current
release-approved version, FreeCAD 1.1.3. FreeCAD 1.1.1 remains a known
historically compatible version, but it is not recommended or accepted as the
current release baseline. No later version is implicitly approved.

The Windows acceptance exercised CPython 3.12, exact upstream/addon/settings
provenance, localhost-only RPC, synthetic UUID-owned document creation,
readback, measurement, modification, applicable Mechanical Design validation,
and cleanup. It did not open or modify real CAD data. See
[Windows release acceptance](WINDOWS_RELEASE_ACCEPTANCE.md) for the protected
host and installation boundary. Upstream Windows instructions do not
substitute for this project's acceptance.

No compatibility is guaranteed for any other commit, version, tag, host,
transport, or platform.
