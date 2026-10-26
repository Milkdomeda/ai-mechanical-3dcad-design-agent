---
name: mechanical-fea
description: Prepare, run, inspect and report source-bound FreeCAD linear static FEA through the Mechanical Design Agent. Use for mechanical text-to-FEA requests or reviewing existing static results; physical approval and report acceptance remain explicit human decisions.
---

# Mechanical Static FEA

Use the eight `analysis_*` tools on the design MCP surface. They share state
with `mechanical-design analysis`; never create a parallel result store. Read
[the input and operation guide](references/workflow.md) for protocol fields,
installation, native viewing and recovery commands.

## Establish the physical question

Reuse the current managed Design Job and its selected FCStd output. Preserve
its source and CAD lifecycle. FEA supports one connected bonded solid with
C3D10 elements, homogeneous isotropic linear elasticity, infinitesimal
kinematics, fixed supports, directed total surface force, planar pressure and
optional self-weight. Contact, buckling, plasticity, creep, fatigue and printed
interlayer failure need separate engineering methods.

Collect the included solid, explicit reference-object exclusions, global
coordinate system, support/load regions, E, nu and their evidence. For gravity,
collect measured mass or density and a mass-distribution basis. Clarify whether
a quoted total includes self-weight. Keep force factors separate. Infill is
not a Young's modulus or a mass measurement. Never fill missing physical data
with the software qualification benchmark values.

Distinguish supplied facts, derived values and proposed assumptions. Ask for
choices affecting physical meaning. Propose fixed engineering ROIs, relative
or absolute displacement metrics, reference regions, strain/displacement scope
bounds and any allowables with evidence. Missing allowables mean
`criteria_status=not_evaluated`; numerical convergence does not mean a part is
safe. State units and tolerances for numeric requirements.

## Prepare and approve

Call `analysis_capabilities`. If setup is required, use the explicit isolated
setup and qualification commands in the guide. Keep daily FreeCAD and vendor
bridges unchanged. Do not treat headless checks as GUI qualification.

The agent writes the structured request as data, using the installed schemas.
Do not ask the user to paste Python into FreeCAD or manually edit acceptance
JSON. Use native inspection or an initial `needs_input` preparation to discover
geometry; select physical regions by their surface type, area, centroid and
explicit tolerances, never by a guessed first face. Repeated preparation uses
the returned state revision and creates a new immutable review.

Present the returned physical summary, mesh/ray policy, engineering ROIs,
limitations, knowledge-retrieval state and native region/load preview. Actually
view the preview images. Resolve `needs_input` issues before asking for
approval. Call `analysis_approve` only with the human's applicable decision,
exact reviewed specification hash and revision. A prior CAD approval, general
request to automate FEA or approval of runtime patches is not physical approval.

## Execute and inspect

Call `analysis_run` with the approved hash, revision and a stable idempotency
key. Poll `analysis_status` with event cursors; polling is read-only. Use
`analysis_cancel` for a requested cancellation. Technical retries use an
explicit new run and retain failed evidence; never silently change supports,
material, loads, ROIs, tolerances or mesh policy to obtain a pass.

When the run reaches `awaiting_visual_review`, call `analysis_report` with
`action=get`. Inspect every exact returned required image with an image-capable
tool: each case's displacement components/magnitude, stress figure and mesh.
Check framing, nonempty viewport/mesh, actual field, units, legible legend,
step, stress recovery and effective deformation display. Treat unexpected
uniformity or magnitudes as findings, not as proof of success. Do not declare
an image inspected from its filename, metadata, existence or process exit.

Only after actual inspection, call `analysis_report` with
`action=finalize_visual`, the complete exact reference list and current state
revision. This records the assistant's visual review, not human mouse actions.
If the client cannot view images, leave the visual gate pending and explain
the missing capability. Never upload replacement numerical results or set a
pass flag yourself.

For an interactive FreeCAD result panel, use `analysis open` for an explicit
run and case. It safely rebuilds the native FEM objects and validated results
in a separate viewer process. It never restores a saved FEM FCStd. Poll
`analysis open-status`; close only the owned session with `analysis close`.
Keep the original completion immutable. Working files in the private W1 viewer
directory are retained; they are not newly validated delivery evidence.

## Deliver and accept separately

Report numerical, engineering-criteria, applicability and currentness statuses
separately. Link JSON, Markdown, PNGs and native FCStd from the verified
manifest. Preserve raw global peaks and locations alongside engineering ROI
values, equilibrium checks and the last two adjacent refinement comparisons.
Changed source/specification makes an old result historical.

`analysis_confirm` requires a separate human decision on the exact current
manifest. Do not call it for the user, or infer it from an instruction to run or
publish software. A presentation revision gets a new manifest and does not
inherit acceptance. Changed physics requires a newly prepared and approved
run. Analysis acceptance does not call CAD confirmation or publish lessons.
