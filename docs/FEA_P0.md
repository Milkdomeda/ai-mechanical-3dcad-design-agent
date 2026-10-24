# Static FEA P0 feasibility probes

This is test infrastructure, not an enabled text-to-FEA product feature.
Only synthetic axial, bending, partitioned-face and split-cylinder fixtures are accepted.
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

The workspace must be new and outside the source checkout. The probe retains
spaces and non-ASCII characters to test their compatibility; it never renames
the workspace to hide a failure. The Windows console command uses ASCII
Python escapes and the native Gmsh writer uses its relative-directory API.
FreeCAD settings are redirected to this isolated workspace.

The official Windows build's SMESH post-write check fails with a non-ASCII
native temporary directory. The separate Windows path matrix confirms that
an external Unicode FCStd path works when `FREECAD_USER_TEMP` is ASCII;
direct UNV export to a Unicode filename still fails. These are different
capabilities, and neither diagnostic result establishes a full P0 pass.

For controlled P0 runs, pass an explicit private root:

```text
python scripts/fea_acceptance.py --config <config.json> --workspace <new-Unicode-workspace> --native-scratch-root <private-ASCII-root> --phase p0 --automated-only
```

The root must have an existing parent, be outside the source checkout and
resolve to ASCII. The runner creates private directories, validates existing
ownership/access, rejects reparse/symlink paths through the secure filesystem
backend, locks the owned directory, and verifies FreeCAD's actual temporary
path. Only child-process `FREECAD_USER_TEMP` changes. Normal scope exit clears
cache contents while the owner lock and directory handle remain held, after
native processes exit. An empty directory and owner lock remain deliberately:
POSIX has no atomic compare-inode-and-unlink-directory operation. A fresh UUID
is used for every run; retained shells are never reused. Exceptions preserve
owned diagnostics. Root and owner lock identities are checked before cleanup;
an advisory lock alone does not pin a POSIX directory name.
Input, result, FCStd and report paths remain in the external workspace.
An unavailable or unsafe root fails explicitly; no global settings, 8.3 names
or directory aliases are silently substituted.

This adapter closes the tested headless serialization path only. Direct UNV
export and default GUI startup with a Unicode temporary directory remain
unsupported. The separate manual native GUI open/recompute/input-equivalence
gate still needs the supported startup configuration and evidence. Native
writer or SMESH runtime patches require separate review and authorization.

## Reviewed native R1 compatibility profile

The source-only R1 patch candidate is in
[`runtime-patches/freecad-1.1.3-faceforce-r1`](../runtime-patches/freecad-1.1.3-faceforce-r1/README.md).
It installs consistent T6 integration and one total force across unique
reference faces into native `femmesh/meshtools.py`. The standard writer still
generates input; the agent never changes Force or post-processes CLOAD records.
The payload/diff has its own LGPL license and provenance. It is not an enabled
product feature or an unchanged official FreeCAD runtime.

Prepare only a **new isolated copy**, using an explicitly reviewed v1 tool
configuration and official 1.1.3 runtime root:

```text
python scripts/fea_r1_prepare.py --source-root <official-runtime-root> --base-config <v1-config.json> --destination <new-isolated-runtime-directory>
python scripts/fea_acceptance.py --config <isolated-runtime-directory/config.json> --workspace <new-Unicode-P0-workspace> --native-scratch-root <private-ASCII-root> --phase p0 --automated-only
python scripts/fea_r1_acceptance.py --config <isolated-runtime-directory/config.json> --workspace <new-Unicode-R1-workspace> --native-scratch-root <private-ASCII-root>
```

Preparation refuses unknown module bytes, broad directories containing
unrelated siblings, existing destinations, aliases or source/destination
overlap. Source files are never patched. The macOS adapter preserves detached
signatures with the system copy tool, excludes only unsealed generated Python
caches, verifies the baseline resource signature, then locally signs the
isolated modified envelope and verifies it. This local ad-hoc build is not an
official Developer ID/notarized distribution. It does not disable OS policy,
remove quarantine metadata or alter the original installation. The copied
bundle is made read-only. Windows uses a separate official portable copy.

The generated `FEAP0Configuration/v2` requires the exact R1 profile and a
SHA-256-pinned `FEAR1Runtime/v1` manifest. Module, source patch, integration
scripts, complete runtime inventory, Python/Qt/OCCT and tool identities are
bound together. Before geometry and after execution the worker verifies the
actual imported module path, bytes and all changed function/helper code.
Structural code fingerprints avoid treating Python marshal interning details
as code changes while still rejecting stale bytecode. User home/data/config
are explicit isolated paths; bytecode writes are disabled. Windows extended
device paths are compared by canonical name and file identity. V1 continues
to require an exact approved official native module hash; it cannot silently select R1.
The official Windows archive carries CRLF source bytes. Its separately pinned
raw SHA and canonical LF SHA must both match before patching. Unknown bytes
are refused even if normalizing their line endings would match; both platforms
produce identical patched LF bytes. Original raw digests remain in receipts.
The identity-only startup directory is presented to FreeCAD without the
Windows device prefix after canonical validation. The native startup matrix
confirmed prefixed user/config directories fail before Python starts, while
both executable spellings succeed with the ordinary directory spelling.
Filesystem checks and same-file identity remain authoritative.

The R1 automated evidence adds independent analytic graph integration, signed
corner weights, legacy T3/Q4/Q8, invalid-map/reference rejection, order-20
Vandermonde/NumPy surface integration of original input, node-by-node forces,
moments and quadratic virtual work. NumPy is already bundled with native
FreeCAD; no new numerical dependency is installed. Three mesh levels and
reversed/permuted reference cases run the native solver, retaining result
inventories and response measurements. Same-mesh reference permutation must
preserve displacement and von Mises stress; reversal negates displacement and
preserves von Mises stress (rtol 1e-9). Each of the three circular cantilever
deflections is checked against the independent uniform-load beam formula
`F L^3 / (8 E I)`, `I = pi R^4 / 4`, using the existing P0 cantilever 5%
tolerance. Adjacent displacement changes must also be within 5% of that
reference. Actual displacement/stress changes are retained, with asymptotic
and maximum-stress convergence explicitly `not_established`. These fixture
checks do not establish a general curved-mesh or engineering-strength policy.
The five native unittests include extreme-area force tables using actual
FreeCAD Quantity objects. Unrepresentable integration weights and nonfinite
emitted loads fail explicitly.

For these additional static, globally clamped CLOAD fixtures, raw CalculiX RF
is retained. The support resultant is `raw RF - CLOAD on the printed constrained
nodes`, using constraints, node sets and loads read from the unchanged input
and independently checked by the host. RF includes loading as well as support
reaction at a node; see the official [CalculiX 2.23 manual, section 6.11.5](https://www.dhondt.de/ccx_2.23.pdf#page=397).
Transformed/MPC/distributed/nonzero-boundary/multi-step accounting is rejected.
This interpretation does not change existing P0 assertions, their tolerances,
the native input, or solver output. Both raw RF and derived support resultants
remain in evidence.

The dedicated Windows R1 workflow must demonstrate the original official
curved-force failure, then pass the unchanged P0 automated checks and the R1
evidence against a different copied runtime. A green R1 compatibility workflow
still records full P0 as `blocked` and the operator GUI gate as `not_executed`.
Rollback selects the original v1 configuration and discards only the owned
isolated copy after its processes stop. No system installation or user CAD is
part of the patch.

The runner builds standard native FEM objects, generates Gmsh second-order
tetrahedra and the unmodified native CalculiX input, executes CalculiX,
reads native results and RF totals, and checks them against hand-derived
benchmark values. Force quantities carry explicit N units and directions
use a native direction reference, not a transient DirectionVector value.
Before averaging, every consumed result field must cover the mesh's unique
node inventory with matching lengths and finite values. Multiface probes
check actual native nodal loads, load-region membership, and resultant
moment about the global origin, including two curved cylinder faces.

The curved fixture deliberately retains the strict declared-force check.
If the native writer's curved-surface quadrature produces a force deficit,
the report fails. A numerical tolerance policy needs separate evidence and
review; the harness must not normalize the loads or patch the native input.

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

GitHub-hosted `windows-2025` runs cover the automated subset and the complete
supported offline suite on Windows Server 2025. They do not establish a
Windows 11 desktop acceptance or supply the approved operator gate.
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
