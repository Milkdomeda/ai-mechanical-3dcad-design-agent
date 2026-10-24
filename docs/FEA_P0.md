# Static FEA P0 feasibility probes

This is test infrastructure, not an enabled text-to-FEA product feature.
Only synthetic axial, bending, and partitioned-face fixtures are accepted.
Existing design sessions and customer CAD are not opened or modified.

## Automated probes

Use Python 3.12+, the project test environment, official FreeCAD 1.1.3,
and explicitly reviewed Gmsh/CalculiX executables. Do not copy example
checksums or infer executable versions from filenames.

Copy `scripts/fea_live.json.example` outside the repository and replace each
path, SHA-256, and version with the reviewed local executable identity. For
a separately verified official Windows portable archive, the helper can
select the distribution-level `bin` executables and pin their real versions:

```text
python scripts/fea_p0_config.py --root <verified-extracted-root> --output <config.json>
python scripts/fea_acceptance.py --config <config.json> --workspace <new-isolated-root> --phase p0 --automated-only
```

The workspace must be new and outside the source checkout. Paths with spaces
and non-ASCII characters remain supported; the Windows console command uses
ASCII Python escapes rather than renaming the workspace. FreeCAD settings
are redirected to this isolated workspace.

The runner builds standard native FEM objects, generates Gmsh second-order
tetrahedra and the unmodified native CalculiX input, executes CalculiX,
reads native results and RF totals, and checks them against hand-derived
benchmark values. Force quantities carry explicit N units and directions
use a native direction reference, not a transient DirectionVector value.

Quality calibration samples the C3D10 map at 15 locations, checks positive
determinants/scaled Jacobians and counts intersected elements on three
declared thickness rays. Inverted and folded-midside fixtures exercise the
checks. These results are evidence for straight benchmark elements, not a
global positivity proof or a validated policy for arbitrary curved parts.

Gmsh and CalculiX startup cancellation runs on copies of generated inputs;
it never overwrites the validated solver output. Separate process tests
exercise three-generation trees, timeout, owner crash, PID/UUID mismatch,
UTF-8 logs, and Windows worker-owned kill-on-close Job Objects. This is not
a sandbox for executables that deliberately escape their owned POSIX group.

The native report contains `automated_status` separately from `status`.
An automated-only invocation can exit successfully while the full native
gate remains `blocked`. It must not be advertised as completed P0.

## Native GUI round-trip gate

The approved design separately requires an operator to open the known,
checksum-identified synthetic `analysis.FCStd` in standard FreeCAD 1.1.3,
without a product-specific FreeCAD addon, and regenerate the native input.
Record the exact source hash, FreeCAD identity, operator action, screenshot,
and exported input. Compare mesh/connectivity, material, node/element sets,
constraints and total load against the original native input. Do not edit
or resave the source model to clear this gate.

The automated harness never restores a persisted FEM/Proxy document. A
successful headless result, a matching filename, a copy of the original
input, or a hosted CI green check does not replace native GUI evidence.
The current P0 report deliberately retains `native_gui_roundtrip` as
`not_executed`; separate operator evidence and review are required before
declaring P0 complete. Do not change that flag by hand.

GitHub-hosted Windows runs cover the automated subset and the complete
supported offline suite. They do not supply the approved operator gate.
Until both platform evidence and the remaining native GUI gate are reviewed,
later implementation phases remain gated.

## Local evidence and distribution

Reports, FEM documents, meshes, input/results, settings and logs remain in
the explicit isolated output directory. They do not belong in Git or public
packages. The CI artifact contains only synthetic evidence and its JUnit
reports. No user configuration, private design model, credentials, or runtime
knowledge is uploaded.

A failed solver, unknown result field, wrong unit/direction, incomplete
result, invalid mesh, changed executable, or missing manual evidence must
remain failed/blocked. These probes do not certify printed-part strength.
