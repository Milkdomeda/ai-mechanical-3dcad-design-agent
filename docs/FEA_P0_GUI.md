# P0 manual GUI evidence helper

This is test infrastructure for the reviewed FreeCAD 1.1.3 R1 profile.
It does not enable text-to-FEA, modify the native runtime, or change P0's
numerical thresholds. Existing automated reports remain immutable.

The legacy manual preparation and live launcher support macOS. Their Windows
manual mode is unavailable; the separate autonomous matrix is described below.
GitHub Server evidence is not Windows 11 desktop acceptance. The pure input
comparator is platform-neutral. The macOS source
preparation tests explicitly skip on other platforms rather than claiming
native Windows protection or launch coverage.

Use the reviewed, local preparation manifest and external workspace:

```text
python scripts/fea_gui_acceptance.py prepare --manifest <case-manifest.json> --workspace <new-external-workspace>
python scripts/fea_gui_acceptance.py launch --session <workspace/session.json> --config <pinned-r1-config.json> --native-scratch-root <private-ascii-root>
python scripts/fea_gui_acceptance.py console --session <workspace/session.json>
```

`launch` is a living owner process. It holds W1 and process ownership until
the GUI exits, with a default two-hour bound. Its blank GUI never receives a
CAD document argument. `console` prints the fixed identity-only collector
for the verified GUI Python console; it does not execute it or open a model.
The owner validates native identity and descendant PID before recording
`receipts/launch.json` as ready. Do not use the daily FreeCAD MCP instance.

The operator must open the specified read-only FCStd copy, inspect its native
objects/mesh/results, recompute, select the fresh output directory, and click
the standard `Write .inp File` button. After writing, while the solver panel
is still open, collect the case snapshot:

```text
python scripts/fea_gui_acceptance.py console --session <session.json> --case axial_bar
python scripts/fea_gui_acceptance.py check --session <session.json> --case axial_bar --actions <explicit-human-actions.json>
python scripts/fea_gui_acceptance.py summary --session <session.json> --case-actions <case-to-actions-paths.json>
```

Actions carry the session, actual GUI PID, exact FCStd SHA, explicit human
statement/operator, timezone-aware ordered times and hashed PNG evidence.
The helper checks the native snapshot and new input, never infers manual
work from a successful solver or a file timestamp, and offers no `--passed`
or automatic document restore option. These local receipts document trusted
operator attestations; they are not cryptographic proof of physical mouse
input or a hostile-user sandbox.

The comparator supports one static C3D10 step and the exact native cards
used by the P0 fixtures. Unknown cards/options, missing data, ambiguous
coincident-node renumbering and unreviewed Proxy state fail closed. It
compares full ordered connectivity, material/section/set/DOF/step/output
semantics and signed per-node CLOADs, as well as force, moment and quadratic
virtual work. Files are never rewritten. Source, prepared-session, helper
and evidence hashes are checked again at use time.

`check` writes JSON and Markdown; exit zero means this case passed. `summary`
revalidates every configured case and action file. Final macOS acceptance also
requires a session-bound normal GUI exit, successful final runtime verification,
and completed W1 cleanup; case checks before shutdown are provisional.
Repeated output cards are outside the supported native subset and are rejected.
Even after macOS passes,
its overall P0 status stays blocked and `eligible_for_p1` remains false while
Windows GUI is not executed. Missing evidence remains blocked; no manual
record changes the historical automatic report.

To request shutdown, use `stop --session <session.json>` or close only the
owned GUI normally. Only the living owner terminates its process tree; a
different session's stop request is ignored. W1 contents are cleared only
after owned process exit. Startup/identity failures retain scratch for
diagnosis. No existing installation or user CAD is part of cleanup.

## Autonomous native GUI regression

The separate P0 automation entry uses a fresh normal GUI process and fixed
synthetic specifications. It records `actor=assistant`; historical human
receipts remain unchanged. It is test infrastructure, not the deployable
analysis product interface.

```text
python scripts/fea_gui_automation.py --config <pinned-r1-config.json> --workspace <new-external-workspace> --native-scratch-root <private-ascii-root>
```

The first process generates complete native GUI fixtures through standard
`ObjectsFem` factories. A second independent process restores exactly those
hash-bound fixtures after auditing the archive and native JSON-null Proxy
metadata. The second process checks standard ViewProviders, result and solver
task panels, mesh/field inventory, displayed quantities, and new input written
by the native task-panel button. Input comparison uses the unchanged semantic,
force, moment and work checks. Each process retains W1 until exit and verifies
the runtime before and after. No command accepts an arbitrary source FEM file.
This acceptance-only restore path does not change the product CAD security
policy or authorize automated customer Proxy restoration.

A headless-generated FCStd can contain complete result fields while omitting
`GuiDocument.xml` and native Python ViewProviders. Such a file can open without
showing the standard result/solver panels. A deployable native delivery needs
GUI-context construction, the correct standard ViewProviders, and actual
save/reopen checks. Reassigning Proxy objects on a previously restored placeholder
can also leave native attachment state incomplete; that is not the production
reconstruction strategy.

The report separates native GUI regression, pending visual review, unexecuted
human review, and overall P0 status. A successful automation command establishes
only its recorded checks. It does not certify engineering strength or close an
unverified platform/visual gate.

The CI workflow records Windows Server x64 and Windows 11 ARM64 with the x64
FreeCAD archive as distinct compatibility runs. Windows 11 ARM64 execution is
not Windows 11 x64 evidence. OpenGL or desktop startup failures remain failures;
the workflow does not switch to an offscreen backend or relabel them as passes.

Automated figures retain the native viewport pixels and add field/unit,
raw nodal extrema, effective deformation scale and a matching legend with Qt
already bundled in FreeCAD. The classic native nodal palette includes zero;
positive-only data therefore starts at green rather than assigning blue to
the smallest observed value. The legend reproduces the pinned native scalar
anchors, with explicit provenance, and does not rescale or replace solver data.

The Windows GUI test environment explicitly materializes the official archive's
bundled `opengl32sw.dll` as a local `opengl32.dll` in the new isolated copy.
Both Qt and native Coin calls then use the same OpenGL implementation. It does
not replace a system DLL or edit an existing installation. Before the GUI run,
the bundled Python compiler creates missing checked-hash caches without
executing their source modules. Previously pinned cache bytes, all source files
and all original binaries must remain identical. Original numerical manifests
are retained; a separate GUI configuration and manifest pin the materialized
revision. Source, compiler and driver provenance accompany the CI artifacts.

[Run 40095737180](https://github.com/Milkdomeda/ai-mechanical-3dcad-design-agent/actions/runs/40095737180)
at `e2e44a6` passed the complete offline suite, eight repeated native CAS races,
unchanged numerical P0 checks, and all five native GUI cases with independent
process round trips on Windows Server 2025 x64 and Windows 11 ARM64 using x64
Python/FreeCAD compatibility. The Server viewport reported OpenGL 3.0 with the
software profile. This establishes that exact test environment; it does not
establish Windows 11 x64 desktop or hardware graphics compatibility. macOS uses
CalculiX 2.23, while the pinned Windows distribution supplies CalculiX 2.22;
both use Gmsh 4.15.0 and FreeCAD 1.1.3.

Where project acceptance authorizes an assistant operator, visual review is a
separate hash-bound assistant receipt. It never becomes a human attestation.
Historical manual gate fields stay unchanged. Capability activation still
requires all applicable numerical, GUI and visual checks for the declared
platform profile; a green CI job alone does not activate the product interface.
