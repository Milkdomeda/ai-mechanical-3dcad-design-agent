# AI Mechanical Design Experimentation Workflow

**Date:** 2026-08-30

**Status:** User-approved architecture; pending written-spec review and implementation

**Repository:** `ai-mechanical-design-agent`

## 1. Purpose

Refocus the ordinary mechanical-design workflow on evaluating and improving an AI Agent's mechanical design, knowledge reuse, CAD modeling, validation, debugging, and automatic correction capabilities.

The default workflow shall be simple, stable, low-interaction, and independent of enterprise-style lifecycle infrastructure. PostgreSQL-backed Job, Working Copy, Change Set, Approval Envelope, mutation authorization, engineering-obligation, and delivery-approval mechanisms shall no longer be prerequisites for ordinary CAD mutation.

The target ordinary flow is:

```text
user request
-> requirement clarification
-> short design proposal
-> one natural-language user approval
-> knowledge retrieval
-> CAD modeling
-> automatic validation
-> automatic correction when needed
-> final result
```

The existing governed workflow remains temporarily available as an optional compatibility profile. It is not the default and cannot block lightweight design work.

## 2. Goals

- Make an ordinary design executable without PostgreSQL or Neo4j.
- Require only one user approval of the design direction before CAD mutation.
- Accept clear Chinese and English approval or rejection language without a canonical phrase.
- Keep Product Family Knowledge and Design Lessons useful to the Agent without turning retrieval into a mutation gate.
- Preserve standard-part provenance and strict model validation.
- Keep FCStd as the source of truth for designed geometry.
- Use inspectable filesystem state and atomic JSON rather than transactional lifecycle records for one-off design sessions.
- Reduce MCP tool-schema exposure and repeated lifecycle calls in the default profile.
- Preserve enough local metadata to resume, debug, validate, and deliver a design reliably.
- Use the approved four-basketball carrier as the first real end-to-end acceptance case.

## 3. Non-goals

- Do not delete or migrate existing PostgreSQL rows or `jobs/` directories.
- Do not rewrite Product Family onboarding, Design Lesson publication, knowledge indexing, standard-part acquisition, or model-validation algorithms in this change.
- Do not introduce SQLite.
- Do not preserve enterprise governance semantics in the default design path.
- Do not make a database-backed audit trail, multi-user concurrency, optimistic revisioning, or canonical confirmation a lightweight-session requirement.
- Do not weaken exact-model validation, SHA-256 binding, standard-part provenance, or FreeCAD safety boundaries.
- Do not push, publish, tag, or release as part of implementation unless separately requested.

## 4. Current-state findings

The current version 0.5.0 implementation couples ordinary design to governance across MCP exposure, service construction, PostgreSQL schema, repository operations, agent instructions, and tests.

The dependency inventory found:

- `working_copy_id` references across 43 source, test, and documentation files, with approximately 907 matches;
- `design_job_*` references across 41 files;
- `design_change_*` references across 21 files;
- `approval_envelope` references across 20 files;
- default MCP startup currently exposes `all` tools unless an environment profile overrides it;
- the `design` profile includes Job, obligation, approval, mutation, applied-change, confirmation, validation-record, and delivery-approval tools;
- ordinary mutation authorization currently reconstructs PostgreSQL obligation rows and can fail before FreeCAD is touched, as demonstrated by UUID serialization in the basketball-carrier task.

Knowledge, standard-parts, and validation capabilities are valuable. The governance gate that surrounds them does not directly improve ordinary geometry quality and shall be removed from the default path.

## 5. Selected migration approach

Use a progressive split:

1. create an independent lightweight design core;
2. make that core the default `design` MCP profile;
3. move the existing Job/change/approval surface to an optional `governed` profile;
4. keep family knowledge and maintenance profiles separate;
5. leave existing governed data and migrations unchanged;
6. evaluate physical deletion of the governed implementation in a later, separately reviewed change.

This approach avoids a high-risk simultaneous rewrite of CAD flow, knowledge publication, migrations, and provenance while immediately removing governance from ordinary design.

Adding a bypass flag to the old lifecycle is rejected because it would preserve parameter burden, service coupling, tool-schema cost, and failure modes. Immediate deletion of all governance code is rejected because Product Family onboarding and Design Lesson provenance still share parts of the database and repository layer.

## 6. Lightweight session directory

Every ordinary design uses one filesystem directory:

```text
designs/<design-id>/
├── design.json
├── source/                 # present only for an existing-model snapshot
├── model.FCStd
├── validation/
└── output/
```

Rules:

- `<design-id>` is a caller-supplied safe identifier so retries and resume operations do not create accidental duplicates.
- Every session declares `new_design` or `existing_model`.
- An existing-model source is copied once into `source/`, inspected through the existing FCStd/STEP safety boundary, and never edited in place.
- The directory must remain inside the selected workspace.
- Creation is atomic and fails on an incompatible existing directory.
- An existing matching `design.json` returns the same session rather than creating a duplicate.
- `model.FCStd` is the authoritative CAD file.
- Validation JSON, Markdown, and PNG artifacts remain below `validation/`.
- Deliberate derivative exports remain below `output/`.
- No `.git` directory, database file, secrets, or executable CAD script is written inside a session.
- Existing `jobs/` directories remain untouched.

## 7. Lightweight state contract

`design.json` uses `LightweightDesignSession/v1` and stores one current snapshot rather than an event log.

Minimum contract:

```json
{
  "schema_version": "LightweightDesignSession/v1",
  "design_id": "four-basketball-carrier-20260830",
  "title": "Four-Basketball Hand Carrier",
  "model_classification": "new_design",
  "status": "approved",
  "requirements": {},
  "proposal_summary": "One-piece PLA lattice carrier",
  "approval": {
    "state": "APPROVE",
    "text": "approved"
  },
  "knowledge": {
    "status": "not_executed",
    "used_ids": [],
    "warning": null
  },
  "model": {
    "relative_path": "model.FCStd",
    "sha256": null,
    "seed_sha256": null,
    "source_relative_path": null,
    "source_sha256": null
  },
  "validation": {
    "status": "not_executed",
    "working_sha256": null,
    "report_relative_path": null,
    "evidence_relative_paths": []
  },
  "created_at": "RFC-3339 timestamp",
  "updated_at": "RFC-3339 timestamp"
}
```

Allowed statuses are `approved`, `modeling`, `needs_attention`, and `completed`.

Updates use a workspace-owned lock, a temporary file in the same directory, flush and file sync, then atomic replacement. A malformed, path-escaping, symlink-substituted, or incompatible session fails without rewriting evidence.

## 8. Approval semantics

Approval is a conversation-level design-direction decision, not a database lifecycle event.

The classifier returns exactly one of:

- `APPROVE`;
- `REJECT`;
- `UNCLEAR`.

It normalizes Unicode, case, outer whitespace, and ordinary surrounding punctuation. It uses phrase and token boundaries rather than substring matching.

The initial vocabulary shall include at least:

| State | Chinese examples | English examples |
|---|---|---|
| `APPROVE` | 批准、同意、可以、继续、确认 | approve, approved, yes, proceed, go ahead |
| `REJECT` | 拒绝、不同意、不批准、停止 | reject, rejected, no, stop, not approved |
| `UNCLEAR` | empty, contradictory, conditional, or unknown text | empty, contradictory, conditional, or unknown text |

Negative or contradictory meaning takes precedence over a positive substring. For example, `not approved`, `不批准`, and `不同意` are never classified as `APPROVE`. Text containing both clear approval and rejection cues is `UNCLEAR`.

Only `APPROVE` creates a lightweight session. `REJECT` and `UNCLEAR` return a non-mutating result that tells the Agent to revise or clarify the proposal.

After session creation, no Change Set, Approval Envelope, mutation authorization, canonical confirmation, or second approval is required for implementation details within the approved direction.

A materially different function, mechanism, key interface, specified material, manufacturing process, or explicit user constraint still requires conversational clarification and a new design-direction approval. That decision updates the lightweight session snapshot; it does not create an enterprise lifecycle.

## 9. Default MCP tools

### 9.1 `design_start`

Inputs:

- `design_id`;
- `title`;
- `model_classification`, exactly `new_design` or `existing_model`;
- `requirements_json`;
- `proposal_summary`;
- `approval_text`.
- optional `source_path`; required for `existing_model`. For `new_design`, it may identify a verified empty FCStd seed to reuse; otherwise the packaged neutral seed is used.

Behavior:

1. classify `approval_text`;
2. return without mutation for `REJECT` or `UNCLEAR`;
3. validate the requirements object and safe identifier;
4. create or idempotently resume `designs/<design-id>/`;
5. for `new_design`, create `model.FCStd` from the packaged neutral FCStd seed, or inspect and copy an explicitly supplied shape-free FCStd seed, using the reviewed FreeCADCmd boundary;
6. for `existing_model`, inspect and snapshot the read-only FCStd or STEP source, then create an independent normalized `model.FCStd`;
7. write `design.json` atomically;
8. return the design ID, absolute model path, source identity when applicable, approval state, and next recommended action.

The operation requires workspace and FreeCADCmd readiness but not PostgreSQL, Neo4j, a Product Family, a Job, or a Working Copy.

### 9.2 `design_knowledge_retrieve`

Inputs:

- `design_id`;
- `query`;
- `features_json`;
- optional `required`, default `false`.

Behavior:

- With an available knowledge backend, build the existing scoped design context and return applicable approved knowledge.
- With matches, record `completed_matches` and the IDs actually selected by the Agent.
- With no matches, record `completed_no_match` and continue.
- With an unavailable database or projection, record `unavailable`, include a concise warning, and continue when `required=false`.
- When the user explicitly requires a named Product Family or Design Lesson, the Agent sets `required=true`; unavailable or unresolved required knowledge then returns a blocking result.

No retrieval receipt, Job revision, Working Copy, obligation row, or mutation gate is required for the default flow. Knowledge scope and redaction rules remain unchanged.

### 9.3 `design_record_result`

Inputs:

- `design_id`;
- `model_path`;
- `validation_report_path`;
- `evidence_paths_json`.

Behavior:

1. require every path to resolve inside the session;
2. calculate the current FCStd SHA-256;
3. parse the validator JSON;
4. require `status=passed`;
5. require report `working_sha256` to equal the current FCStd hash;
6. require mandatory report-contract fields and referenced Markdown/PNG evidence;
7. write the model hash and validation summary into `design.json`;
8. set `completed` only when every check passes, otherwise set `needs_attention`.

This operation records evidence; it does not ask for delivery approval.

`design_start` is also the resume operation: an existing matching design ID returns the current snapshot and model path. No separate session-lifecycle tool is added.

## 10. Runtime and profile architecture

Profiles become:

- `design`: default lightweight design tools, system status, applicable standard-part tools, and no governance tools;
- `governed`: existing Job, Working Copy, obligation, Change Set, Approval Envelope, mutation, validation-record, confirmation, and delivery tools;
- `family-knowledge`: existing Product Family onboarding and knowledge-management tools;
- `maintenance`: existing setup, audit, and projection-maintenance tools;
- `all`: backward-compatible union for explicit use only.

The default profile changes from `all` to `design`.

The lightweight service receives `LightweightDesignSettings` containing only:

- workspace path;
- package-resource root;
- local design root;
- configured FreeCADCmd path, version, SHA-256, and file identity.

It does not receive a database URL. The knowledge tool constructs the database-backed knowledge service lazily only when retrieval is invoked and converts an unavailable optional backend into a recorded warning.

Default lightweight tool calls must not instantiate `MechanicalDesignService`, `PostgresRepository`, `DesignWorkspace`, `JobWorkspaceManager`, or Neo4j drivers.

## 11. Code boundaries

Add:

- `src/mechanical_design_agent/approval_semantics.py` for deterministic tri-state parsing;
- `src/mechanical_design_agent/lightweight_design.py` for session schema, storage, seed creation, hashing, and result recording;
- `src/mechanical_design_agent/lightweight_knowledge.py` for optional knowledge retrieval and fallback behavior;
- `LightweightDesignSettings` and a bootstrap-runtime resolver that does not inspect or require database secrets.

Modify:

- `server.py` to construct the lightweight service independently and register the new tools;
- `tool_profiles.py` to add `governed`, redefine `design`, and make `design` the default;
- package resources and public-distribution allowlists when required by new modules;
- Agent Skill instructions so ordinary design uses one approval and the lightweight tools;
- architecture, setup, workflow, README, and optional-workflow documentation;
- tests that currently assert governed tools are present in the default design profile.

Retain for optional governed compatibility:

- `jobs.py`;
- `design.py`;
- `approval_envelope.py`;
- `engineering_obligations.py`;
- existing Job and governance PostgreSQL migrations;
- existing governed repository and Service operations.

The current governed UUID-serialization defect is not a default-flow prerequisite. A narrow compatibility fix may be made if required to keep the governed regression suite truthful, but no lightweight code may depend on that fix.

## 12. Knowledge, Design Lessons, and Product Families

- PostgreSQL/pgvector remains authoritative for long-term approved knowledge, assertions, Design Lessons, family profiles, review state, and searchable vectors.
- Neo4j remains a rebuildable relationship projection.
- Existing authorization and redaction rules for specialized family knowledge remain active.
- A no-match outcome never blocks ordinary CAD.
- Database unavailability never blocks ordinary CAD unless the user explicitly required named knowledge.
- Lightweight session JSON stores only retrieval status, selected public IDs, and a concise warning or summary. It does not duplicate private knowledge contents.
- Product Family onboarding and Design Lesson publication continue through their dedicated profiles and existing durable storage.
- Post-design lesson capture is optional and separate from delivery. It cannot introduce a second model-approval gate.

## 13. Standard parts and validation

Standard-part provider ordering, provenance, catalog identity, license, source URL, part number, designation, nominal size, version or commit, and SHA-256 requirements remain unchanged.

The default profile retains read-only provider/source inspection and the standard-part registration operation needed when a design actually contains standard components. Database-backed provenance may be required for that optional operation, but it is not a prerequisite for custom-part CAD mutation.

Model validation remains mandatory before completion:

- FCStd is validated directly from its session path;
- numeric requirements retain explicit units and validation tolerances;
- evidence remains bound to the exact FCStd SHA-256;
- any model change invalidates prior evidence;
- the authoritative fastener inventory and controlled-overlap rules remain unchanged;
- top, front, and isometric visual review remains required;
- a failed mandatory check or stale hash sets the session to `needs_attention`;
- the Agent may repair and rerun validation automatically without another approval when it remains inside the approved design direction.

`design_validation_record` and `design_delivery_approve` remain governed-profile compatibility tools and are not called by the default flow.

## 14. Failure behavior

| Failure | Lightweight behavior |
|---|---|
| Approval is `REJECT` | Do not create or modify a session; return revision guidance |
| Approval is `UNCLEAR` | Do not create or modify a session; ask for clarification |
| PostgreSQL/Neo4j unavailable | Record knowledge `unavailable`; continue unless retrieval was explicitly required |
| No knowledge match | Record `completed_no_match`; continue |
| FreeCAD seed creation fails | Return setup/blocked diagnostic; do not create a false ready session |
| FreeCAD modeling or save fails | Preserve debuggable files; set `needs_attention` |
| Validation fails | Set `needs_attention`; permit automatic correction and rerun |
| FCStd/report hash mismatch | Reject completion as stale evidence |
| Missing JSON/Markdown/PNG evidence | Reject completion as incomplete evidence |
| Material design direction changes | Clarify and obtain one new conversational design approval |

Errors remain concise and action-oriented. They must not ask for lifecycle IDs or canonical confirmation phrases in the default flow.

## 15. Compatibility and data retention

- Existing PostgreSQL rows, migrations, Job directories, source snapshots, Working Copies, Change Sets, envelopes, validation records, and Design Lessons remain unchanged.
- No automatic conversion from a Job to a lightweight session is provided.
- Existing governed tools remain callable only through explicit `governed` or `all` profiles.
- Existing documented governed API contracts are deprecated for ordinary design but are not silently changed in this implementation.
- New documentation labels governed mode as optional compatibility for audit-heavy or multi-user scenarios.
- Physical deletion of governed code or database schema requires a later dependency review and separate breaking-change specification.

## 16. Testing strategy

### 16.1 Approval semantics

- all required Chinese approval and rejection examples;
- all required English approval and rejection examples;
- case, Unicode, whitespace, and surrounding punctuation normalization;
- negative phrases such as `not approved` and `不批准`;
- contradictory and unknown text returning `UNCLEAR`;
- no substring false positives.

### 16.2 Session storage

- new session creation and idempotent resume;
- both `new_design` neutral-seed creation and `existing_model` read-only source snapshotting;
- incompatible collision rejection;
- atomic JSON replacement and crash-safe temporary-file cleanup;
- path containment, symlink substitution, and unsafe identifier rejection;
- spaces, non-ASCII paths, Windows separators, and UTF-8 behavior;
- packaged neutral FCStd creation without PostgreSQL.

### 16.3 Knowledge fallback

- approved matches returned and locally summarized;
- no-match continuation;
- unavailable PostgreSQL continuation when optional;
- unavailable or unresolved explicitly required knowledge blocking;
- specialized knowledge remains redacted without family authorization.

### 16.4 Result and validation recording

- passed same-hash report completes the session;
- failed report sets `needs_attention`;
- stale hash, missing artifact, malformed report, or missing mandatory field cannot complete;
- a correction changes the model hash and requires a new validation report;
- no database service is constructed during start or result recording.

### 16.5 MCP profiles and compatibility

- default profile is `design`;
- default tools exclude Job, Change Set, Approval Envelope, mutation authorization, canonical confirmation, validation-record, and delivery-approval tools;
- `governed` exposes the legacy lifecycle tools;
- `family-knowledge`, `maintenance`, and explicit `all` remain internally complete;
- existing Product Family, Design Lesson, standard-part, packaging, and validation tests continue to pass;
- the supported complete offline suite passes on macOS and Windows-relevant path tests.

### 16.6 Live acceptance

After implementation, resume the already-approved basketball-carrier requirement using the lightweight flow:

1. create `four-basketball-carrier-20260830` without a database lifecycle, reusing the existing governed task's empty FCStd only as an inspected neutral seed while leaving the old Job and database rows unchanged;
2. record optional knowledge retrieval as completed-no-match or unavailable;
3. build the one-piece PLA carrier in FreeCAD;
4. validate the exact FCStd revision;
5. automatically repair and rerun if needed;
6. record the passed report and evidence;
7. present the final FCStd and validation artifacts without another lifecycle approval.

## 17. Documentation and instruction changes

Update public documentation and project-owned Agent Skills so they consistently state:

- design capability is the primary product goal;
- structured requirement clarification and one user approval precede modeling;
- clear natural Chinese or English approval is sufficient;
- ordinary design uses lightweight local sessions;
- knowledge retrieval improves design but is not a default mutation gate;
- validation remains mandatory for completion;
- governed mode is optional and explicitly selected;
- PostgreSQL is required for durable knowledge workflows, not ordinary CAD mutation;
- generated sessions and CAD evidence remain ignored local artifacts.

Remove instructions that force ordinary designs through Jobs, obligations, envelopes, mutation authorization, applied-change recording, canonical `批准`, delivery approval, or mandatory Design Lesson review.

## 18. Completion criteria

The refactor is complete when:

1. the default MCP profile is lightweight and does not expose governance tools;
2. `design_start` accepts approved Chinese and English phrases without canonical wording;
3. an ordinary session can start, model, validate, correct, and complete without PostgreSQL;
4. optional knowledge retrieval handles match, no-match, and unavailable states as specified;
5. exact-hash validation remains mandatory for completion;
6. Product Family Knowledge, Design Lessons, standard parts, and validation capabilities retain their dedicated tests;
7. governed data and tools remain available only through explicit compatibility profiles;
8. documentation and Agent Skills describe the same simplified workflow;
9. focused tests and the complete supported offline suite pass; and
10. the approved basketball-carrier design completes through the lightweight flow with final FCStd and validation evidence.
