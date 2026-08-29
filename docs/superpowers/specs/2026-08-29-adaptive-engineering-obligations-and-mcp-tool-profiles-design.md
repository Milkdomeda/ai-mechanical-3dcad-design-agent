# Adaptive Engineering Obligations and MCP Tool Profiles

**Target release:** AI Mechanical 3DCAD Design Agent v0.5.0

## Purpose

Reduce the Mechanical Design MCP surface presented to a coding agent without
removing engineering capability, weakening deterministic gates, or turning
mechanical design into a fixed pipeline.

The governing principle is:

> Engineering obligations must reach an explicit conclusion, but their depth
> and order remain adaptive to the design.

The system must never impose a universal `A -> B -> C` sequence. A simple
single-part design may resolve Product Family, standard-parts, and assembly
questions immediately as `no_match` or `not_applicable`. A mechanism or
assembly expands only the obligations triggered by its approved scope.

## Current problem

The MCP currently exposes 85 tools in one flat list. Ordinary design, Product
Family knowledge construction, workspace administration, legacy compatibility,
and owner-only audit operations all compete in the same model-visible schema
set. Several pairs are especially easy to confuse:

- `design_job_get` and the ingestion-only `job_get`;
- `design_context_build` and receipt-producing `design_knowledge_retrieve`;
- two legacy working-copy creators and two Job-aware creators;
- the default Design Lesson flow and its legacy staging/audit surfaces;
- workspace Product Family configuration and PostgreSQL-authoritative family
  discovery;
- ordinary design operations and projection/catalog maintenance.

Instructions alone cannot make this reliable. The backend must describe open
engineering questions and reject unsafe omissions, while the MCP exposure layer
must show only the canonical tools for the selected task profile.

## Non-goals

- Removing Service or repository capabilities in v0.5.0.
- Replacing typed tools with a generic `action` or router tool.
- Requiring every obligation to call a network service or run an expensive
  analysis.
- Automatically binding a Product Family from semantic similarity.
- Treating `not_applicable` as permission to skip ordinary geometry validation.
- Dynamically changing the MCP tool list in the middle of a session.
- Adding telemetry or a language-model dependency.

## Adaptive obligation model

### Obligation kinds

Version 0.5.0 governs four cross-cutting questions:

1. `product_family_resolution`
2. `knowledge_retrieval`
3. `standard_parts_assessment`
4. `assembly_assessment`

Geometry/model validation, approval, delivery, and Design Lessons remain their
existing lifecycle gates. They are reported beside obligations but are not
reimplemented as obligation records.

### No global sequence

An obligation read model exposes:

- `open_obligations`;
- `resolved_obligations`;
- `recommended_actions`;
- `allowed_actions`;
- `blocked_actions` with exact reasons.

More than one action may be allowed. The backend blocks only an operation whose
own prerequisites remain unresolved. It does not require unrelated obligations
to be resolved in a fixed order.

### Outcomes

Product Family resolution uses existing append-only match decisions:

- `matched`
- `no_match`
- `not_configured`
- unresolved `confirmation_required` or `conflict`

Knowledge retrieval uses the existing receipt:

- `completed_matches`
- `completed_no_matches`

Standard-parts assessment decisions use:

- `not_applicable`
- `no_candidates`
- `candidates_resolved`
- `approved_custom_exception`

Assembly assessment decisions use:

- `not_applicable`
- `required_pending`
- `required_passed`

`required_pending` is a valid screening conclusion that permits approved
modeling to begin; it keeps the existing assembly-validation gate open and
therefore cannot satisfy delivery. This avoids the circular requirement to
validate an assembly before it has been modeled.

Every explicit screening decision records `resolution_level=screening` or
`expanded`, a nonblank rationale, the exact scope SHA-256, evidence references,
the actor, and time.

### Scope snapshot

The exact same structured scope is supplied with the initial Design Intent and
with screening decisions:

```json
{
  "schema_version": "EngineeringScope/v1",
  "deliverable_kind": "single_part",
  "component_count": 1,
  "motion_present": false,
  "assembly_interfaces": [],
  "component_plan": [
    {
      "component_id": "mounting-plate",
      "category": "machined_plate",
      "sourcing_class": "custom",
      "included_in_delivery": true
    }
  ]
}
```

The canonical JSON SHA-256 binds every decision. A changed component plan,
deliverable kind, motion declaration, component count, or interface list
invalidates prior standard-parts and assembly screening decisions rather than
mutating them.

### Deterministic expansion rules

`standard_parts_assessment=not_applicable` is rejected when the scope contains:

- `standard_candidate`, `standard_selected`, or `unresolved` sourcing;
- a category in the project standard-part category vocabulary;
- a standard component explicitly included in delivery.

`assembly_assessment=not_applicable` is rejected unless all are true:

- `deliverable_kind=single_part`;
- `component_count=1`;
- `motion_present=false`;
- `assembly_interfaces` is empty.

These rules deliberately cover obvious contradictions. They do not pretend to
replace semantic engineering judgement. The complete scope and conclusions are
shown in the Design Intent review so that an engineer can reject a false
classification.

### Persistence

Migration 018 creates an append-only
`design_job_obligation_decisions` table scoped to the Job and optional working
copy. PostgreSQL remains authoritative. Decisions are immutable; a new scope or
new conclusion appends a successor decision. The table records:

- scope identities and optional working-copy binding;
- obligation kind and outcome;
- resolution level, rationale, scope JSON, and scope SHA-256;
- evidence references and optional predecessor decision;
- actor and timestamp.

Product Family and knowledge outcomes are derived from their existing
authoritative receipts rather than duplicated into this table.

### Public operations

One new canonical tool is added:

`design_job_obligations_resolve(...)`

It records a batch containing only `standard_parts_assessment` and
`assembly_assessment` conclusions for one exact scope. It cannot publish family
or retrieval conclusions, perform a catalog search, approve a custom exception
on the user's behalf, or mark an assembly passed without matching validation
evidence.

`design_job_get` gains an additive `engineering_obligations` read model.

The first Design Intent proposal must include `engineering_scope` inside
`approval_envelope_draft.design_intent`. Legacy/expert change calls remain
available in the `all` profile, but the canonical `design` profile requires
the v0.5 scope contract.

Before mutation authorization, the Service verifies that:

- Product Family resolution has a terminal match/no-match/not-configured
  conclusion;
- knowledge retrieval has a completed receipt;
- standard-parts and assembly conclusions match the exact Design Intent scope;
- any expanded standard-part candidates have evidence;
- an expanded assembly is at least `required_pending`, allowing modeling to
  proceed. It must later reach `required_passed` through same-revision assembly
  completeness before delivery. Ordinary geometry validation remains mandatory
  for all models.

## MCP exposure profiles

### Profiles

The MCP registers tools once at process startup using
`MECH_DESIGN_MCP_TOOL_PROFILE` or an explicit `create_mcp(tool_profile=...)`
argument.

- `design`: canonical ordinary mechanical-design workflow; recommended for new
  project configuration.
- `family-knowledge`: Product Family source ingestion, onboarding, learning,
  assertion review, and family profile operations.
- `maintenance`: bootstrap diagnostics, workspace configuration, catalog
  binding, audit, and projection maintenance.
- `all`: exact compatibility/expert inventory, including legacy tools.

An unset profile remains `all` during the v0.5.0 compatibility window. The
project's documented and generated recommended configuration selects `design`.
Unknown profile values fail closed before the MCP starts.

### Registration mechanism

Each tool receives centrally tested exposure metadata. Registration filters the
tool at server construction; hidden tools are not returned by `tools/list` and
their schemas do not consume model context. Service methods and database gates
remain intact.

The implementation must not rely on unsupported mutation of FastMCP internals.
A project-owned registration decorator decides whether to invoke `mcp.tool()`
for a named function.

### Design profile

The target is no more than 32 visible tools. It keeps canonical tools for:

- system readiness;
- Design Job routing;
- optional Product Family discovery/matching;
- Job-aware working-copy creation;
- obligation resolution and knowledge retrieval;
- Design Intent, mutation authorization, change recording/closure;
- validation, assembly completeness, delivery;
- standard-part providers, source status, and provenance registration;
- the five default Design Lesson operations.

Legacy working-copy creators, raw lesson staging/audit tools, ingestion jobs,
family administration, catalog mutation, and Neo4j maintenance are absent.

### Compatibility

`all` must expose every previously released tool plus the new obligation tool.
No existing tool parameter or result contract is removed in v0.5.0. Profile
selection is additive. A later release may change the unset default only after
one documented transition window and protected-host acceptance.

## Agent instructions

Project instructions must state:

- obligations are a set, not a pipeline;
- a quick explicit conclusion is valid when supported;
- `not_applicable` never means “not considered”;
- scope changes invalidate prior screening conclusions;
- the Agent follows `open_obligations` and `blocked_actions` without assuming a
  single mandatory next tool;
- standard parts expand only when the component plan triggers them;
- ordinary part validation is never skipped because assembly is not applicable.

## Testing and acceptance

### Obligation behavior

Tests cover:

1. a 100 x 80 x 5 mm single mounting plate with four M5 interfaces resolving
   family `no_match`, standard parts `not_applicable` when fasteners are outside
   delivery scope, and assembly `not_applicable`;
2. adding delivered M5 fasteners invalidating the standard-parts conclusion;
3. a linear actuator expanding family, knowledge, standard-part, and assembly
   obligations;
4. rejecting `not_applicable` when deterministic triggers contradict it;
5. multiple allowed actions with no global ordering;
6. scope-hash drift reopening only affected obligations;
7. mutation/delivery failing closed on unresolved applicable obligations;
8. no Product Family configuration resolving quickly without blocking generic
   design.

### Tool profiles

- `design` exposes at most 32 tools and every core design workflow remains
  reachable.
- `family-knowledge` exposes its complete onboarding workflow without ordinary
  maintenance tools.
- `maintenance` exposes diagnostics and repair surfaces.
- `all` preserves the compatibility inventory.
- hidden tool names and schemas are absent from `tools/list`.
- unknown profiles fail before server startup.
- wheel/sdist, macOS, Windows, clean-install, and MCP inventory tests pass.

### Release

The version is 0.5.0. The final release occurs only after merging with current
`main`, running the complete offline suite, available live PostgreSQL/Neo4j and
FreeCAD boundaries, clean installed-wheel tests, public artifact scans, and one
overall code review. The release is tagged and published once; there is no
separate v0.4.1 tag.
