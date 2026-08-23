---
name: freecad-standard-parts
description: Select, create, insert, position, and verify reusable standard mechanical parts in FreeCAD through the configured FreeCAD MCP connection. Use for bolts, screws, nuts, washers, threaded hardware, gears, racks, worms, bearings or other catalog components; for BOM/source metadata; and whenever a CAD task should prefer a standards library over redrawing commodity parts.
---

# FreeCAD Standard Parts

Use library-provided, parametric objects before constructing commodity geometry manually.

## Workflow

1. Call `list_documents` to confirm the FreeCAD MCP bridge is connected. If connection is refused, launch FreeCAD, wait for its local RPC bridge, then retry. Keep `remote_enabled` false.
2. Read [references/providers.md](references/providers.md) before selecting a standard, provider, or Python API.
3. Select the provider in this order:
   - Use Fasteners Workbench for standardized screws, bolts, nuts, washers, pins, inserts, and related hardware.
   - Use `freecad.gears` for involute, internal, cycloid, bevel, worm, timing, lantern, crown, and rack geometry.
   - Use `$step-parts` for bearings, guide rails, rollers, named motors and connectors, catalog flanges, structural profiles, and other purchasable components not covered by the installed workbenches.
   - Use the verified local FCStd/STEP catalog only when none of the preceding providers covers the requested component. Check its source and license before reuse.
   - Do not construct or substitute standard-part geometry unless the user explicitly requests or approves it. When a search is ambiguous, present candidates. When it misses, record the attempted terms and stop for direction.
4. Resolve omitted engineering choices conservatively. Prefer ISO metric hardware, a 20-degree pressure angle for general-purpose involute gears, and simplified threads unless manufacturing-detail geometry is explicitly required. Report assumptions.
5. Use `execute_code` with [scripts/freecad_standard_parts.py](scripts/freecad_standard_parts.py) for deterministic creation or verified STEP import. Resolve the script relative to this skill directory, add its `scripts` directory to `sys.path`, and import `freecad_standard_parts`.
6. For STEP.parts downloads, use [scripts/cache_step_part.py](scripts/cache_step_part.py) outside FreeCAD. It stores `<part-id>/<sha256>/<filename>.step` plus `manifest.json` under the configured global cache. Verify the checksum before importing.
7. Preserve each standard part as a separate parametric document object. Position it with `Placement`; group it under an assembly or parts group. Do not fuse standard parts into the custom body unless the user explicitly requests a manufacturing solid. Design-required machining may create a derived component, but it must retain the base part's library metadata.
8. Add or update a BOM with quantity, category, standard, nominal size, provider, source commit or URL, part ID, and SHA-256 where applicable. Do not silently substitute a different standard or nominal size.
9. Save deliverables under the workspace `output/` folder. Verify object names, standard identifiers, nominal sizes, shape validity, expected solids, critical gear dimensions, and metadata. Use `get_view` for a visual check, then invoke `$freecad-model-validation` before reporting completion.

## Guardrails

- Keep real modeled threads off by default; they increase recompute time and file size.
- Treat generated gear geometry as nominal CAD. Confirm backlash, profile shift, material, tolerance, and strength requirements before manufacturing release.
- Use an assembly constraint workbench only when it is installed and the user requests persistent kinematic constraints; otherwise use explicit placements and an assembly group.
- Keep the pinned add-on sources unchanged during ordinary modeling work. Update them only as a separate reviewed maintenance task.
- Treat network or DNS failure as inconclusive, not as a catalog miss. Retry once with permission; never use the failure to justify an automatic placeholder.
