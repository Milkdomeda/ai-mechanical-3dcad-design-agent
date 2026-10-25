# Source-bound FreeCAD static FEA

The analysis service turns reviewed physical inputs into a deterministic native
FreeCAD/Gmsh/CalculiX run, complete result fields, native figures and a verified
report bundle. It shares the current filesystem design workspace. It does not
replace an engineer's physical approval or certify a product.

## Install and configure

Use Python 3.12 or newer and install the built wheel in an isolated environment.
For `uv pip install`, use `--link-mode copy` so installed execution files have
independent identities. The design MCP profile exposes the original ten tools
and eight additive `analysis_*` tools; the knowledge-admin profile is unchanged.
Existing CAD functionality does not require FEA configuration.

Select an initialized workspace and a fresh official FreeCAD 1.1.3 distribution
containing Gmsh 4.15.0 and CalculiX 2.22 or 2.23. Then run:

```text
mechanical-design analysis setup --workspace <workspace> --source-root <official-distribution-root> --destination <new-isolated-directory> --native-scratch-root <private-ASCII-directory>
mechanical-design analysis doctor --workspace <workspace> --qualify
mechanical-design analysis capabilities --workspace <workspace>
```

The source and destination must be separate. Setup validates exact reviewed
native source bytes, copies the distribution, checks its inventory, applies the
R1 consistent T6/global surface-force patch and the single-line R2 nodal RF
output patch, verifies actual loaded code and pins all executables. macOS uses
the application signature and a read-only copied envelope. Source installation
bytes are checked again after preparation. No source installation, global
FreeCAD preferences, vendor bridge or existing isolated runtime is edited.
An already used macOS application can contain unsigned startup caches; use a
fresh official distribution rather than repairing the daily installation.

The W1 native working directory must be private and ASCII. Input, output and
workspace paths may contain spaces and non-ASCII text; fixed native phases copy
bounded inputs into W1 and copy verified outputs back. W2 is not implemented.
Setup performs no implicit downloads or add-on installations. Only explicit
executable overrides inside the selected distribution are accepted. On Windows,
`--software-opengl` explicitly materializes the hash-pinned Mesa DLL already in
the official x64 distribution; it is not a hardware-GPU qualification.

`doctor --qualify` uses installed package resources, with no test directory or
source-checkout dependency. It generates synthetic geometry, checks both axial
force signs across three mesh levels against analytic displacement/stress,
checks a single total force over two curved native references, and exercises
real native result tasks, fields, images and standard input export. It records
software qualification, not customer material approval or assistant image
inspection. The default executor requires an intact qualification bound to the
exact package/interpreter/dependencies, configuration and runtime. After an
upgrade or changed native identity, run explicit qualification again. Read-only
`doctor` / `capabilities` do not start native programs.

Export the installed agent skill to a new project skill directory:

```text
mechanical-design analysis skill --destination <project>/.agents/skills/mechanical-fea
```

Agent clients which support repository skills can instead discover the
project-owned [mechanical-fea skill](../.agents/skills/mechanical-fea/SKILL.md).
`analysis schema --name prepare-request` and other registered schema names
return the closed installed JSON contracts. The agent writes requests and
reference-list files; users do not paste Python into FreeCAD or author receipts.

## Physical review and execution

Reuse the existing Design Job and a selected FCStd output. Prepare captures an
immutable source snapshot and validates one included connected closed solid,
explicit reference exclusions, native geometry and physical surface selectors.
The source and CAD design state are not changed by analysis.

The review must state E, nu and their evidence; a measured-mass or density basis
for self-weight; force/pressure direction and regions; support interpretation;
whether a quoted total includes self-weight; separate load factors; a fixed
mesh/ray policy; engineering ROIs and displacement references; applicability
bounds; and any criteria with evidence and units/tolerances. Infill is not an
elastic modulus or a mass measurement. Missing allowables leave engineering
criteria unevaluated. All synthetic qualification values are explicitly
artificial and are never defaults for a user's design.

`analysis_prepare` returns the physical summary, issues, native geometry
manifest and exact preview-image paths. Resolve `needs_input`, inspect the
region/load preview, then record the human's decision with `analysis_approve`
against the returned specification hash and state revision. Conditional or
ambiguous text does not approve. CAD approval and runtime-patch approval do not
approve physical FEA inputs.

`analysis_run` queues that exact approved hash with a stable idempotency key.
The detached supervisor owns its process trees independently of the MCP client.
Polling `analysis_status` is read-only. Explicit cancellation affects only the
selected run; a failed, cancelled, interrupted or timed-out solve needs an
explicit new retry. There are no hidden material/support/load changes or
optimization retries. Resource limits include bounded wall time, disk, logs,
counts and observed memory; observed memory is not a hard OS quota.

The native writer creates standard editable FEM objects and a read-only input
which is independently checked before solving. The adapter retains full nodal
displacement, stress, von Mises, principal stress and actual per-node reaction
fields. Reactions account for native loads on constrained nodes. Mesh Jacobian,
full-span ray coverage, force/moment equilibrium, applicability bounds, and the
last two adjacent displacement/stress refinement comparisons are separate
gates. Raw global peaks and locations remain visible alongside engineering ROI
observations.

## Results and interactive use

A successful numerical/GUI run pauses at `awaiting_visual_review`.
`analysis_report(action=get)` returns every required registered image. The agent
must actually view all case figures and meshes, checking framing, fields,
legends, units, step, recovery and deformation display. File existence or a
process exit cannot substitute for image inspection. Clients without image
viewing leave this gate pending.

After inspection, `analysis_report(action=finalize_visual)` receives the exact
reference list and state revision. It publishes JSON, Markdown, PNGs, standard
native FCStd and raw numerical evidence under an acyclic hash manifest, writing
completion last. Report-write failures retain evidence and do not rerun the
solver. Completed bundles can be verified/copied without starting FreeCAD.

For an interactive result task:

```text
mechanical-design analysis open --workspace <workspace> --design-id <job> --analysis-id <analysis> --run-id <run> --case-id <declared-case>
mechanical-design analysis open-status --workspace <workspace> --design-id <job> --analysis-id <analysis> --session-id <returned-session>
mechanical-design analysis close --workspace <workspace> --design-id <job> --analysis-id <analysis> --session-id <returned-session>
```

Open reconstructs safe BREP, UNV and already validated native results in a fresh
isolated GUI. It does not automatically restore a saved FEM FCStd. The native
Show Result task opens with von Mises selected, and readiness requires a
nonempty native cloud. The viewer uses its own private W1 child directory so it
does not hold the solve queue's scratch lock. Its working FCStd and diagnostics
are retained separately; they are not a revised validated delivery. Close
requests that owned session's shutdown without attaching to an arbitrary PID.
The interactive session has a four-hour wall limit and observed resource
bounds. Save intentional edits as a separate working model; changed physics
must return through a new reviewed analysis.

## Acceptance and scope

Numerical status, engineering-criteria status, applicability, currentness and
human acceptance are separate. `analysis_confirm` records only the human's
explicit decision against the exact current manifest and state revision. It
does not confirm CAD or publish lessons. Presentation revisions retain the
same solver artifacts but receive a new manifest and do not inherit prior
acceptance. Source/specification changes make previous results historical.

The first adapter covers a single bonded homogeneous isotropic solid under
linear static, infinitesimal-deformation assumptions, fixed global supports,
directed total surface force, planar pressure and optional self-weight. It does
not establish contact, buckling, plasticity, fatigue, creep, interlayer strength,
manufacturing release or standards compliance. Thickness checks cover the
explicit approved rays, not an automatically certified minimum wall everywhere.

Platform evidence distinguishes macOS arm64, Windows Server 2025 x64 and
Windows 11 ARM hosting x64 Python/FreeCAD through emulation. The latter does not
establish Windows 11 x64 desktop/hardware-GPU acceptance. Unsupported or failed
native graphics combinations remain blocked rather than falling back to a
headless pass. Qualification is local to the actual installed environment.
