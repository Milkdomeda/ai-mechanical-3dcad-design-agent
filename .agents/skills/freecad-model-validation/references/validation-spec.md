# Validation specification

Pass a Python dictionary or a JSON file path to the validator. Omit sections that do not apply, but never omit tolerances from numeric checks.

```json
{
  "required_objects": ["Gear", "Bolt", "Bearing"],
  "required_links": [
    {"object": "AssemblyLink", "property": "LinkedObject"}
  ],
  "dimensions": [
    {"object": "Gear", "metric": "bbox_z", "expected": 15.0, "tolerance": 0.05},
    {"object": "Gear", "metric": "solid_count", "expected": 1, "tolerance": 0}
  ],
  "placements": [
    {"object": "Bearing", "axis": "z", "expected": 20.0, "tolerance": 0.05}
  ],
  "standard_parts": [
    {
      "object": "Bearing",
      "provider": "STEP.parts",
      "part_id": "catalog-id",
      "sha256": "hex-digest"
    }
  ],
  "interference_pairs": [
    {"a": "Bolt", "b": "Gear", "max_common_volume_mm3": 0.001}
  ],
  "compressible_overlap_pairs": [
    {"compressible": "Seal", "mate": "Flange", "max_common_volume_mm3": 25.0}
  ]
}
```

## Mandatory fastener installation gate

The validator automatically detects every object whose `LibraryCategory` is `fastener`, whose provider contains `Fasteners`, or which exposes `FastenerSetId`/`FastenerRole`. Detection is not controlled by the specification. Every detected object must declare these FreeCAD properties:

| Property | Required value |
|---|---|
| `FastenerSetId` | Shared non-empty ID for one installed hardware set |
| `FastenerRole` | `bolt`, `screw`, `stud`, `nut`, or `washer` |
| `FastenerConnectionType` | `through_bolt` or `tapped` |
| `FastenerNominalDiameterMM` | Positive nominal diameter |
| `FastenerAxisLocal` | `+X`, `-X`, `+Y`, `-Y`, `+Z`, or `-Z` |
| `FastenerHostObjects` | Semicolon-separated installed host object names |
| `FastenerContactObjects` | Semicolon-separated intended bearing-contact object names |
| `FastenerClearanceObjects` | Clearance-hole hosts; mandatory for a through-bolt driver |
| `FastenerThreadedObjects` | Threaded hosts; mandatory for a tapped driver |

These fixed mandatory CAD gates cannot be weakened by a model specification:

- one bolt/screw/stud driver per set;
- one or more nuts for `through_bolt`; a valid driver for `tapped`;
- consistent connection type and nominal diameter within the set;
- set-member axes parallel within 0.5 degrees and laterally coincident within 0.25 mm;
- bolt/screw/stud axis matched to a compatible cylindrical clearance or threaded hole within the same limits;
- positive axial bounding-box overlap with every declared hole host and with every nut/washer member;
- no driver/common solid volume in a clearance hole above 0.001 mm³;
- no rigid fastener/host or bearing-contact common volume above 0.001 mm³;
- bearing-contact gap no greater than 0.05 mm;
- threaded engagement overlap allowed only for a driver/host pair explicitly listed in `FastenerThreadedObjects`.

Missing metadata, missing objects, an invalid hardware combination, an off-axis member, a driver outside its hole, an unclassified overlap, or a contact gap is a mandatory failure. Manual `interference_pairs` never replace this gate.

## Compressible component overlap

Mark a seal or other deformable component with `IsCompressible=true`, `AllowedOverlapWith` as a semicolon-separated list, and a non-negative `MaxCompressionOverlapMM3`. Add exactly one `compressible_overlap_pairs` entry for every allowed mate. Each entry requires `compressible`, `mate`, and `max_common_volume_mm3`; its pair limit may not exceed the component property.

The numeric maximum is project-specific; the example value is illustrative and is not a default tolerance.

This is a controlled modeling exception for compressible parts. Rigid parts, undeclared mates, incomplete pair coverage, ordinary `interference_pairs` used in place of this section, and overlap above the declared maximum all fail. A declaration permits overlap but does not require it.

## Supported metrics

- `bbox_x`, `bbox_y`, `bbox_z`: object bounding-box lengths in millimeters.
- `volume`: shape volume in cubic millimeters.
- `solid_count`: number of solids in the shape.
- `placement_x`, `placement_y`, `placement_z`: object placement base coordinates in millimeters.
- `property:<FreeCADProperty>`: numeric value of an explicit FreeCAD property.

`placements` is a concise equivalent for the three placement metrics. Supported axes are `x`, `y`, and `z`.

## Standard-part manifest

`standard_parts` may be supplied directly or through `standard_parts_manifest`, a JSON path whose top-level `parts` array has the same entries. Each listed object is mandatory and must expose the shared `Library*` metadata properties. Native FreeCAD providers require provider, standard, nominal size, source URL, and pinned commit. STEP.parts objects additionally require part ID and SHA-256.

## STEP provenance manifests

`validate_step` accepts these provenance profiles:

| `provenance_profile` / `source_class` | Required contract |
|---|---|
| `step_parts` | Preserve the existing strict STEP.parts contract: exact `provider="STEP.parts"`, matching `part_id` and embedded record `id`, source URL, standard/name/nominal designation, declared SHA-256, and exact checksum equality. Existing STEP.parts manifests without the enum fields remain valid through exact-provider inference. |
| `manufacturer_official` | Require explicit profile, nonempty truthful `provider` and `manufacturer`, matching IDs, product `source_url`, source STEP `asset_url` or `step_url`, designation, declared SHA-256, and exact checksum equality. |
| `verified_local_catalog` | Require the manufacturer-official fields plus `source_identity` and `copy_identity` objects. Each identity requires a distinct nonempty `path` and the same SHA-256 as the manifest; the copy filename must match the STEP file being validated. |

Use `provenance_profile` as the primary enum. `source_class` may repeat it or act
as the enum when `provenance_profile` is absent; when both appear, they must match.
Unknown/conflicting profiles, empty provider/manufacturer, ID mismatch, missing
source/asset/designation, incomplete local identities, or checksum mismatch fail.

Example verified-local manifest excerpt:

```json
{
  "provenance_profile": "verified_local_catalog",
  "source_class": "verified_local_catalog",
  "provider": "Manufacturer official CAD portal",
  "manufacturer": "Manufacturer name",
  "part_id": "catalog-part-id",
  "source_url": "https://manufacturer.example/products/catalog-part-id",
  "asset_url": "https://manufacturer.example/cad/catalog-part-id.step",
  "sha256": "64-hex-digest",
  "source_identity": {
    "path": "prior-project/standard_parts/catalog-part-id.step",
    "sha256": "64-hex-digest"
  },
  "copy_identity": {
    "path": "standard_parts/catalog-part-id.step",
    "sha256": "64-hex-digest"
  },
  "part": {
    "id": "catalog-part-id",
    "standard": {"designation": "standard or nominal designation"}
  }
}
```

## Result contract

Every check contains `id`, `validator`, `status`, `message`, `mandatory`, and optional `actual`/`expected` fields. The `validator` value is `freecad-model-validation`. Fastener checks use `fastener.*` and `fastener-set.*`; controlled overlaps use `compressible.*`. Overall status is `passed` only when every mandatory check passes. Reports are written as `<model>_validation.json`, `<model>_validation.md`, and `<model>_validation.png`.

Every FCStd result contains the exact `working_sha256` and a `fastener_inventory` entry for every automatically detected fastener. Each entry names its set, role, connection type, nominal diameter, axis, hosts, contacts, clearance/threaded hosts, and mandatory contract check. `summary.fasteners_detected` equals the number of entries in this authoritative inventory.

Assembly-validation consumers must compare this inventory with assembly occurrence coverage. They must not reconstruct a smaller inventory from a hand-authored assembly manifest, accept a stale model hash, or treat an omitted fastener as outside the validation scope.
