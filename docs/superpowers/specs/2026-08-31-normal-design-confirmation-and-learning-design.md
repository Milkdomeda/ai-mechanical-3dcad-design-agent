# Normal Design Confirmation and Learning Workflow

**Status:** Approved in conversation; written review pending  
**Target release:** AI Mechanical 3DCAD Design Agent v0.7.0  
**Date:** 2026-08-31

## Objective

Make one clear process the normal mechanical-design workflow:

1. clarify the requirement;
2. present a short design proposal;
3. obtain one natural-language approval of the design direction;
4. retrieve applicable knowledge;
5. create or modify CAD directly;
6. validate the exact model and correct safe failures automatically;
7. record the final result;
8. accept the user's natural-language confirmation of the completed design;
9. automatically evaluate and summarize reusable Design Lessons;
10. finish immediately when no material lesson exists, or show one review card
    when material lessons exist;
11. request one separate natural-language decision only before publishing that
    review card to the durable knowledge system.

The product must describe this as the normal design process. It must not use a
special adjective to distinguish it from an enterprise process that no longer
exists.

## Product principles

- Design quality, correctness, knowledge reuse, CAD reliability, validation,
  debugging, and low interaction cost have priority.
- One design-direction approval authorizes CAD work and validation-driven
  correction inside that direction.
- Final-model confirmation and knowledge publication are distinct decisions.
- Design Lessons are a normal post-confirmation learning step, not a CAD gate.
- A missing Product Family match, missing Design Lesson match, or unavailable
  knowledge backend does not block CAD work.
- PostgreSQL and Neo4j are knowledge infrastructure, not CAD mutation
  prerequisites.
- The final FCStd and its exact-hash validation evidence remain authoritative
  regardless of later knowledge-system availability.
- No fixed Chinese or English phrase is required for approval, rejection, or
  confirmation.

## Removed product surface

Version 0.7.0 is intentionally breaking. The complete enterprise CAD mutation,
approval, obligation, delivery, and audit workflow released before this version
is removed as one unit.

The removal includes its:

- public MCP tools and tool exposure modes;
- Python services, repositories, models, and error contracts;
- workspace formats and migration adapters;
- PostgreSQL tables and packaged migrations;
- tests, fixtures, skills, examples, and documentation;
- bootstrap capabilities and configuration options;
- compatibility wrappers and deprecated command surfaces.

The release does not translate old enterprise workspaces or databases. Existing
files and databases are not deleted automatically; they are simply unsupported
inputs to v0.7.0. Operators must initialize a new knowledge database.

Git history is not rewritten. The current checkout, built distributions, public
documentation, help text, schemas, and active tests must contain only the new
product model.

## Retained capabilities

The refactor preserves and verifies:

- FreeCAD GUI MCP and FreeCADCmd integration;
- FCStd source-of-truth handling and read-only source snapshots;
- secure cross-platform filesystem operations;
- exact-hash model validation, visual evidence, and automatic correction;
- standard-part selection, registration, provenance, and BOM validation;
- Product Family Knowledge creation, retrieval, review, and projection;
- Design Lesson review, publication, search, supersession, and revocation;
- PostgreSQL outbox authority and rebuildable Neo4j projection;
- macOS and Windows packaging, installation, diagnostics, and release tests.

Retained code may reuse proven validation and knowledge logic, but it must not
retain obsolete CAD workflow dependencies merely to avoid refactoring.

## Architecture

### Design session

Each design is stored under:

```text
designs/<design-id>/
├── design.json
├── model.FCStd
├── source/                 # optional read-only input snapshot
├── validation/
├── output/
└── lesson-review/          # created only after final confirmation
```

`design.json` uses `DesignSession/v1` and is the authority for the single
design run. The file is updated atomically under the existing crash-safe lock.

Its state is separated into independent sections:

```json
{
  "schema_version": "DesignSession/v1",
  "design_id": "example-design",
  "model_status": "completed",
  "direction_approval": {
    "state": "APPROVE",
    "text": "同意"
  },
  "model": {
    "relative_path": "model.FCStd",
    "sha256": "64-hex"
  },
  "validation": {
    "status": "passed",
    "report_relative_path": "validation/model_validation.json",
    "working_sha256": "64-hex"
  },
  "final_confirmation": {
    "state": "APPROVE",
    "text": "设计已确认",
    "model_sha256": "64-hex"
  },
  "lesson_review": {
    "status": "review_pending",
    "review_relative_path": "lesson-review/review.json",
    "review_sha256": "64-hex"
  }
}
```

Model completion is never revoked by a knowledge publication failure. A CAD
change after final confirmation invalidates that confirmation and any unissued
review card, then returns the design to validation.

### Core modules

- `design_session.py` owns session creation, resume, status, exact-hash result
  recording, final confirmation, state invalidation, and local review binding.
- `design_knowledge.py` performs best-effort Product Family and Design Lesson
  retrieval without opening a database when it is not needed.
- `design_lessons.py` validates candidate lessons, builds immutable review
  cards, records no-material outcomes, and coordinates durable publication.
- `knowledge_repository.py` contains only Product Family, Design Lesson,
  searchable assertion, review decision, and outbox persistence.
- `projection.py` remains a rebuildable Neo4j projection of PostgreSQL
  knowledge records.
- `server.py` exposes the normal design tools and a separate knowledge
  administration surface.

The previous monolithic service is split along these boundaries. Generic secure
filesystem, hashing, validation, FreeCAD runner, packaging, and standard-part
modules remain shared utilities.

## MCP tools

### Normal design surface

The default `design` surface exposes:

- `design_system_status`
- `design_start`
- `design_status`
- `design_knowledge_retrieve`
- `design_record_result`
- `design_confirm`
- `design_lesson_decide`
- `standard_part_providers_get`
- `standard_part_sources_status`
- `standard_part_download_register`

This is the complete ordinary workflow. No hidden lifecycle operation is
required between these calls.

### Knowledge administration surface

The `knowledge-admin` surface exposes Product Family onboarding, knowledge
review, Design Lesson search, supersession, revocation, and projection repair.
It does not own CAD mutation or model approval.

There are no catch-all, enterprise compatibility, or lifecycle tool surfaces.

## Approval semantics

All conversational decisions use one deterministic semantic classifier:

- `APPROVE`: clear agreement or instruction to proceed;
- `REJECT`: clear rejection or instruction not to proceed;
- `UNCLEAR`: conditional, contradictory, unknown, or ambiguous language.

Supported language includes ordinary Chinese and English expressions such as
`批准`, `同意`, `可以`, `继续`, `确认`, `设计已确认`, `approved`, `approve`,
`yes`, `proceed`, and `go ahead`. No operation requires a canonical sentence,
identifier embedded in the user's phrase, or exact punctuation.

The tool response always reports the interpreted state and original text. An
`UNCLEAR` result never mutates approval, confirmation, review, or publication
state.

## Final confirmation and lesson extraction

`design_confirm` accepts:

- `design_id`;
- the user's confirmation text;
- a structured list of candidate lessons prepared by the Agent from the design
  history, final FCStd, validation report, correction history, standard-part
  evidence, and manufacturing notes.

The package does not embed a language model. Automatic extraction means the
Agent performs the reasoning as part of the normal flow and submits structured
candidates to the deterministic service.

Before accepting confirmation, the service requires:

- `model_status=completed`;
- an existing FCStd file;
- a passed validation report;
- equality between the current FCStd SHA-256, recorded model SHA-256, and
  validation `working_sha256`;
- existing Markdown and PNG evidence referenced by the validation report.

Candidate lessons are material only when they contain a reusable engineering
problem, a decision or correction, supporting evidence, applicability limits,
and a future prevention or validation action. Project-only dimensions,
customer-specific facts, unsupported conclusions, generic advice, and claims
without evidence are rejected as non-material.

When no material candidate remains, the service records
`lesson_review.status=no_material_lessons` and completes without another user
interaction.

When one or more candidates remain, the service writes one immutable review
card and returns it for display. The review card contains:

- design and model identity;
- final-model and validation-report SHA-256 values;
- evidence paths and hashes;
- each lesson's problem, decision, evidence, applicability, prevention action,
  and proposed search terms;
- privacy and scope screening results;
- the canonical serialized review-card SHA-256.

## Publication decision

`design_lesson_decide` accepts `design_id` and natural-language decision text.

- `APPROVE` publishes the entire displayed review card as one immutable batch.
- `REJECT` records `lesson_review.status=declined` and publishes nothing.
- `UNCLEAR` returns a clarification response without mutation.

Publication is idempotent on the review-card SHA-256. PostgreSQL stores the
approved lessons and an outbox event in one transaction. Neo4j projection is
retried independently and never becomes authoritative.

If PostgreSQL is unavailable, the review card remains local with
`publish_retry_required`; model completion and final confirmation remain valid.
Retrying after recovery cannot create duplicates. A later CAD change creates a
new review identity and cannot publish the stale card.

No Product Family is required for a broadly applicable lesson. Every published
lesson still carries organization, design group, optional family, applicability,
authorization, and provenance scope.

## Knowledge database

Version 0.7.0 starts a new package-owned migration line for knowledge only:

- `001_knowledge_core.sql`: organizations, design groups, Product Families,
  assertions, Design Lessons, reviews, decisions, and outbox;
- `002_knowledge_search.sql`: vector/search support and indexes;
- `003_knowledge_projection.sql`: projection cursor and rebuild metadata.

No table stores CAD mutation approval, design lifecycle, or delivery state.
Design-session JSON is not mirrored into PostgreSQL.

Bootstrap detects incompatible prior schemas and returns a clear
`KNOWLEDGE_DATABASE_REINITIALIZATION_REQUIRED` diagnostic. It never drops or
rewrites an existing database automatically.

## Product Family Knowledge

Product Family onboarding becomes independent of a design run. It uses a
family workspace and the knowledge repository directly. A design may optionally
reference a family ID, but family selection is not required for session creation,
CAD mutation, validation, final confirmation, or general Design Lesson
publication.

Retrieval uses the configured organization and design group plus optional
family, design features, and query text. The result statuses are:

- `completed_matches`;
- `completed_no_match`;
- `unavailable`.

Only an explicitly required named knowledge source may make an unavailable or
no-match result blocking.

## Error handling and recovery

- Direction rejection or ambiguity creates no design session.
- Final confirmation before passed exact-hash validation is rejected without
  state mutation.
- A changed FCStd invalidates final confirmation and any unpublished review.
- Invalid candidate lessons return field-level errors while preserving model
  completion.
- Filesystem writes use atomic replacement and crash-safe locking.
- A partially written local review is never treated as displayable or
  publishable.
- Database failure retains the immutable local review for retry.
- Projection failure records durable outbox state and does not roll back an
  already committed PostgreSQL publication.
- Repeated start, result recording, confirmation, no-material recording,
  rejection, publication, and retry calls are idempotent.

## Documentation and terminology

README, architecture documentation, skills, examples, CLI help, environment
templates, schema names, module names, tool descriptions, and release notes
describe one normal design process.

An automated public-contract test scans active source, tests, documentation,
package resources, and built archives for retired terminology and removed API
names. Historical Git commits are the only retained record of the removed
architecture.

## Testing

### Deterministic unit tests

- Chinese and English `APPROVE`, `REJECT`, and `UNCLEAR` classification;
- design creation, resume, status, result binding, and invalidation;
- final confirmation with exact and stale hashes;
- candidate quality and privacy filtering;
- no-material completion;
- immutable review-card generation and hashing;
- approve, reject, ambiguous, retry, and duplicate publication behavior;
- Product Family optionality and retrieval result handling;
- crash-safe locking and atomic recovery;
- absence of removed tools, modules, schemas, and terminology.

### Integration tests

- PostgreSQL knowledge bootstrap from an empty database;
- incompatible-schema diagnostics without destructive mutation;
- Product Family and Design Lesson publication, retrieval, supersession, and
  revocation;
- transactional outbox and idempotent Neo4j rebuild;
- installed-wheel execution without repository access;
- exact-hash FreeCAD validation and result recording;
- standard-part provenance and BOM checks.

### Release tests

- full supported offline suite on macOS and Windows;
- wheel and source-distribution content allowlists;
- clean installation and MCP entry points;
- package resources and fresh knowledge migrations;
- public documentation links and terminology contract;
- applicable live FreeCAD, PostgreSQL, and Neo4j acceptance runs.

Tests tied only to the removed architecture are deleted, not weakened or
renamed. Tests protecting retained security, validation, standard parts,
knowledge correctness, and cross-platform behavior remain or are rewritten
against the new interfaces.

## Current basketball carrier transition

After the software refactor passes release validation:

1. convert the local basketball carrier `design.json` directly to
   `DesignSession/v1` as a task artifact operation, not a package compatibility
   feature;
2. preserve the existing FCStd and validation evidence bytes;
3. verify the recorded SHA-256 values again;
4. record the already supplied final-design confirmation;
5. automatically derive material candidate lessons;
6. record no-material completion or display the generated review card;
7. request one natural-language decision only if a review card is ready for
   durable publication.

The model is not redesigned, reapproved, or revalidated unless its bytes or
validation evidence have changed.

## Release boundary

The target is v0.7.0. Completion requires:

- removal of the retired architecture from the current product tree;
- the normal design and learning workflow implemented end to end;
- retained CAD, validation, standard-part, and knowledge capabilities passing;
- clean macOS and Windows release suites;
- verified wheel and source distribution contents;
- basketball carrier transition and Design Lesson review demonstrated locally.

A passed model validation report remains evidence of the checks performed, not
engineering certification, finite-element assessment, manufacturing release,
or legal standards compliance.
