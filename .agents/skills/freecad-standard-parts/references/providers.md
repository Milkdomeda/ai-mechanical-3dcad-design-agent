# Providers and standards

## Installed providers

| Provider | Best use | Pinned source | License |
|---|---|---|---|
| FreeCAD Fasteners Workbench 0.5.64 | Bolts, screws, nuts, washers, pins, inserts, threaded rods | `shaise/FreeCAD_FastenersWB` at `79a06dc067b57ebc89532be835704eb2af5da96c` | GPL-2.0-or-later |
| freecad.gears 1.3 | Parametric gears, racks, worms, timing gears | `looooo/freecad.gears` at `790e75c1dbc5c91b4abeee7ba7f972fa5dc7af57` | GPL-3.0-or-later |
| STEP.parts skill | Bearings, guide rails, rollers, catalog flanges, structural profiles, named motors/connectors, other purchasable components | `earthtojake/text-to-cad` `skills/step-parts` at `4fd71ea75fbb8a80b0d7c76862e0fd73c52a8989` | MIT |

Install FreeCAD workbenches separately through their supported FreeCAD installation paths. The Agent's package-owned provider configuration records supported provider identities, while each initialized workspace owns its explicit `config/standard_parts_sources.json` source configuration.

## Provider routing

1. Prefer Fasteners Workbench when it enumerates the exact hardware standard and size.
2. Prefer `freecad.gears` for supported gear and worm families.
3. Search STEP.parts through `https://api.step.parts` for catalog components outside those workbenches.
4. Search the verified local FCStd/STEP catalog.
5. Stop and ask for explicit approval before custom-building a standard part.

Store STEP.parts downloads under the platform cache selected by `scripts/cache_step_part.py`, or set `MECH_DESIGN_STANDARD_PART_CACHE`/`--cache-root` explicitly. The default roots are below `%LOCALAPPDATA%` on Windows, `~/Library/Caches` on macOS, and `$XDG_CACHE_HOME` or `~/.cache` on other systems. Each entry uses `<part-id>/<sha256>/`; keep the STEP file and `manifest.json` together. A cache entry is usable only when its computed SHA-256 matches the manifest.

## Preferred metric hardware mapping

Use these defaults only when the design does not specify another standard:

| Component | Preferred standard | Example |
|---|---|---|
| Fully threaded hex-head screw | ISO 4017 | `ISO4017`, `M8`, length `30` |
| Partially threaded hex-head screw | ISO 4014 | `ISO4014`, `M8`, length `40` |
| Hexagon nut, style 1 | ISO 4032 | `ISO4032`, `M8` |
| Plain washer, normal series | ISO 7089 | `ISO7089`, `M8` |
| Socket-head cap screw | ISO 4762 | `ISO4762`, `M8`, length `30` |
| Countersunk socket-head screw | ISO 10642 | `ISO10642`, `M8`, length `30` |

Diameter and length values must exist in the provider's enumeration. Never coerce an unavailable combination.

## Helper API

Load `scripts/freecad_standard_parts.py` inside FreeCAD and call:

```python
bolt = create_fastener(doc, "ISO4017", "M8", length="30", name="Bolt_M8x30")
nut = create_fastener(doc, "ISO4032", "M8", name="Nut_M8")
washer = create_fastener(doc, "ISO7089", "M8", name="Washer_M8")
gear = create_involute_gear(doc, module_mm=2, teeth=30, height_mm=15, bore_mm=8)
catalog_part = import_step_part(doc, step_path, manifest_path, name="Bearing_608ZZ")
```

`create_fastener` creates a parametric Fasteners Workbench object with simplified threads by default. `create_involute_gear` creates a parametric FCGear involute object and records source metadata.

## Gear defaults and checks

- Use a 20-degree pressure angle unless an existing mesh or a specified standard requires another value.
- Pitch diameter is `module × teeth` for a standard spur gear with zero profile shift.
- Enable the axle hole only when a bore is requested.
- Verify module, tooth count, height, pressure angle, pitch diameter, addendum diameter, root diameter, and bore before handoff.
- Treat backlash, quality grade, heat treatment, hub/keyway geometry, load rating, and lubrication as explicit engineering decisions rather than library defaults.

## Source links

- Fasteners Workbench: https://github.com/shaise/FreeCAD_FastenersWB
- freecad.gears: https://github.com/looooo/freecad.gears
- STEP.parts skill: https://github.com/earthtojake/text-to-cad/tree/main/skills/step-parts
- STEP.parts API: https://api.step.parts
