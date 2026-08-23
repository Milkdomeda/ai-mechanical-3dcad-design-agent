---
name: freecad-model-validation
description: Validate AI-created or modified FreeCAD FCStd parts and assemblies and STEP/STP standard components through the configured FreeCAD MCP connection. Use after CAD creation, visible geometry changes, STEP.parts downloads or imports, assembly placement changes, standard-part substitutions, or before reporting a FreeCAD model complete. Produces strict JSON, Markdown, and PNG evidence without build123d or CadQuery.
---

# FreeCAD Model Validation

Use FreeCAD and its bundled Part/OCCT geometry kernel as the only validation engine. Treat FCStd as the source of truth for designed parts and assemblies; use STEP validation primarily for downloaded standard components and deliberate exports.

## Required workflow

1. Call `list_documents` to confirm the FreeCAD MCP bridge. Keep `remote_enabled` false.
2. Read [references/validation-spec.md](references/validation-spec.md), then create an explicit specification. Every numeric requirement must include a tolerance; never invent manufacturing tolerances. Add the mandatory installation contract to every fastener object and controlled-overlap metadata to every compressible component.
3. Load [scripts/freecad_model_validation.py](scripts/freecad_model_validation.py) inside FreeCAD with `execute_code` and call one or both interfaces:

   ```python
   validate_fcstd(model_path, specification, report_dir)
   validate_step(step_path, manifest_path, specification, report_dir)
   ```

4. Inspect the returned report. For FCStd, the JSON must contain the exact
   `working_sha256`, a complete automatically detected `fastener_inventory`,
   and `summary.fasteners_detected` equal to that inventory length. Mandatory
   failures or a missing report-contract field block completion. Repair the
   model and rerun, or report it as failed/blocked.
5. Use `get_view` on the active model and inspect the saved `<stem>_validation.png`. Geometry checks passing never replaces visual review.
6. Report only checks that actually ran and link the generated JSON, Markdown, and PNG artifacts.

## Validation policy

- Check document recompute, object state, required objects and links, shape validity, solid count, positive volume, dimensions, placements, standard-part provenance, and BOM agreement.
- Automatically detect every fastener and fail if any fastener lacks its installation contract. Validate the complete set combination, coaxial placement, hole-axis match, axial position, clearance-hole containment, threaded-hole classification, bearing contact, and host interference. These checks are mandatory and do not depend on hand-written `interference_pairs`.
- Treat the model-detected `fastener_inventory` as authoritative for assembly
  validation. An assembly consumer must use one passed report with the exact
  same-revision FCStd SHA-256 and prove equality between that inventory and its
  exactly-once fastened-joint assignments. It must fail omitted, duplicate, or
  unknown occurrences and must not reconstruct a smaller inventory from a
  hand-authored manifest.
- Treat a seal or other compressible component as overlap-capable only when it is marked `IsCompressible` and every permitted mate appears in `compressible_overlap_pairs` with a numeric maximum common volume. Never use the compressible exception for rigid parts or undeclared mates.
- Outside the automatic fastener gate and controlled compressible pairs, check interference only for pairs declared in the specification. Each pair must provide `max_common_volume_mm3`; intentional contact without volume is acceptable.
- For STEP assets, require a matching SHA-256 manifest and one supported provenance profile: `step_parts`, `manufacturer_official`, or `verified_local_catalog`. Preserve truthful provider/manufacturer identity; never relabel a manufacturer or local-catalog file as STEP.parts. Import into a temporary FreeCAD document, validate, save evidence, and close the temporary document without changing the source file.
- Do not silently heal, replace, overwrite, or remodel invalid input.
- Do not use build123d, CadQuery, cadpy, or the browser CAD Viewer. FreeCAD MCP, FreeCAD Python, Part/OCCT, and `get_view` are authoritative for this workflow.
- A passed report is geometry and provenance evidence, not engineering certification, load validation, manufacturability approval, or legal standards certification.

## Strict gate

The model is complete only when the report status is `passed`, its FCStd hash
and fastener inventory contract are present, and the visual review finds no
unexplained geometry. A failed checksum, missing required metadata, invalid or
empty shape, failed dimension, missing required link, failed fastener
installation check, invalid compressible-overlap declaration, failed declared
interference check, stale hash, incomplete fastener inventory, or missing
validation artifact, unknown STEP provenance profile, incomplete source/copy
identity, or false provider attribution is blocking.
