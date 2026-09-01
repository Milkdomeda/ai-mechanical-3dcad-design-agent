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
5. If these structured sources return no result, apply the semantic authoritative-source expansion below when commercial availability is reasonably likely.
6. Stop and ask for explicit approval before custom-building a standard part.

Store STEP.parts downloads under the platform cache selected by `scripts/cache_step_part.py`, or set `MECH_DESIGN_STANDARD_PART_CACHE`/`--cache-root` explicitly. The default roots are below `%LOCALAPPDATA%` on Windows, `~/Library/Caches` on macOS, and `$XDG_CACHE_HOME` or `~/.cache` on other systems. Each entry uses `<part-id>/<sha256>/`; keep the STEP file and `manifest.json` together. A cache entry is usable only when its computed SHA-256 matches the manifest.

## Semantic authoritative-source expansion

A zero-result response from Fasteners Workbench, `freecad.gears`, STEP.parts,
or the verified local catalog proves only that the configured structured search
did not match. It does not prove that a standard or commercial component is
unavailable.

After a structured miss, judge the likelihood of an existing commercial part
from the requested mechanical function and interfaces. Consider the operating
principle, mounting and mating interfaces, dimensional envelope, motion or
travel, load-related attributes, material or environment constraints, and
industry vocabulary. Derive useful synonyms, dimensional series, interface
standards, manufacturer terminology, actuation methods, and mounting styles
from those semantics. Do not require the component to appear in a predefined
part-type routing table before expanding the search.

When a commercial off-the-shelf component is reasonably likely, search the
applicable authoritative public sources in this preference order:

1. The manufacturer's official catalog, product page, datasheet, selector,
   configurator, or CAD portal.
2. A standards body or industry association record that establishes the
   component identity, dimensional series, or interface standard.
3. An authorized distributor catalog that preserves the manufacturer, exact
   order code, and links to the manufacturer's primary documentation.

General marketplaces, forums, reposted files, aggregators without manufacturer
identity, and unattributed CAD mirrors may provide vocabulary for another
query, but they are not sufficient provenance and do not authorize CAD reuse.

Record each structured provider and extended authoritative source attempted,
the query terms used, and the material outcome. Preserve the manufacturer,
order code or part ID, standard, nominal size, source URL, source revision or
access identity, license or reuse terms, and SHA-256 for every downloaded file.
If candidates differ materially in interfaces or ratings, present them rather
than choosing silently.

Only after a reasonable semantic expansion across the authoritative sources
applicable to that component may the Agent report that no suitable component
was found. Network, DNS, authentication, or service failure is inconclusive and
must be reported as an incomplete search, not as a market miss. Neither a
structured miss nor a completed extended miss permits custom substitute
geometry without explicit user approval.

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
