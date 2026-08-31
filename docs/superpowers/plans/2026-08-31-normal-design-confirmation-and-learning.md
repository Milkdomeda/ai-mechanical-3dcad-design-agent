# Normal Design Confirmation and Learning Implementation Plan

> **Target:** AI Mechanical 3DCAD Design Agent v0.7.0
>
> **Design:** `docs/superpowers/specs/2026-08-31-normal-design-confirmation-and-learning-design.md`
>
> **Release policy:** Prepare and validate the release locally. Do not push,
> tag, publish, or create a GitHub release without separate user authorization.

## Goal

Replace the retired enterprise CAD workflow with one normal filesystem-backed
design process that continues through final confirmation and automatic Design
Lesson evaluation. Keep durable Product Family Knowledge and Design Lessons in
a knowledge-only PostgreSQL/Neo4j architecture.

## Working rules

- Work only in the nested public repository.
- Preserve unrelated user changes in the outer private repository.
- Use tests before or alongside each behavioral change.
- Remove tests tied only to deleted behavior; rewrite tests that protect a
  retained capability.
- Run focused tests after every task, then the complete offline and packaging
  suites.
- Do not weaken secure filesystem, FreeCAD, validation, standard-part,
  provenance, or knowledge authorization checks.
- Do not create compatibility shims for removed enterprise workspaces or
  databases.

## Task 1: Freeze the new public contract

**Modify:**

- `tests/test_tool_profiles.py`
- `tests/test_public_distribution.py`
- `tests/test_public_documentation.py`
- `tests/test_agent_skills.py`

**Add:**

- `tests/test_retired_surface_absence.py`

**Work:**

1. Define the exact normal design and knowledge-administration tool inventories.
2. Add failing tests for removed tool exposure modes, modules, schemas,
   configuration keys, docs, and package resources.
3. Add a current-tree and built-archive terminology scan.
4. Update distribution allowlists for the intended v0.7.0 surface.

**Verify:**

```text
pytest tests/test_tool_profiles.py tests/test_retired_surface_absence.py
```

## Task 2: Rename and complete the design-session core

**Rename:**

- the current filesystem design-session implementation module to
  `src/mechanical_design_agent/design_session.py`
- its unit-test module to `tests/test_design_session.py`

**Modify:**

- `src/mechanical_design_agent/config.py`
- `src/mechanical_design_agent/bootstrap_runtime.py`
- `src/mechanical_design_agent/bootstrap_diagnostics.py`
- relevant bootstrap tests

**Work:**

1. Replace the old session schema with `DesignSession/v1`.
2. Separate model, final-confirmation, and lesson-review state.
3. Add status reading and exact-hash invalidation behavior.
4. Preserve source snapshot, lock, atomic-write, and Windows path behavior.
5. Remove old identifiers and configuration names.

**Verify:**

```text
pytest tests/test_design_session.py tests/test_bootstrap_diagnostics.py
```

## Task 3: Rename knowledge retrieval and keep it nonblocking

**Rename:**

- the current design-session knowledge retrieval module to
  `src/mechanical_design_agent/design_knowledge.py`
- its unit-test module to `tests/test_design_knowledge.py`

**Work:**

1. Preserve match, no-match, and unavailable results.
2. Bind used knowledge IDs to the design session.
3. Remove Product Family prerequisites for general retrieval.
4. Keep explicitly required named knowledge blocking.

**Verify:**

```text
pytest tests/test_design_knowledge.py
```

## Task 4: Implement final confirmation and local lesson review

**Modify:**

- `src/mechanical_design_agent/design_session.py`
- `src/mechanical_design_agent/approval_semantics.py`

**Add:**

- `src/mechanical_design_agent/design_lesson_workflow.py`
- `tests/test_design_confirmation.py`
- `tests/test_design_lesson_workflow.py`

**Work:**

1. Add `design_confirm` with natural-language semantics.
2. Require completed exact-hash FCStd and validation evidence.
3. Validate structured candidate lessons.
4. Deterministically filter non-material or private candidates.
5. Record no-material completion without another interaction.
6. Create an immutable local review card when material lessons exist.
7. Invalidate unpublished review state after CAD byte changes.

**Verify:**

```text
pytest tests/test_design_confirmation.py tests/test_design_lesson_workflow.py
```

## Task 5: Replace the database with a knowledge-only schema

**Replace:**

- `src/mechanical_design_agent/resources/migrations/postgres/`
- top-level `migrations/postgres/`

**Add:**

- `001_knowledge_core.sql`
- `002_knowledge_search.sql`
- `003_knowledge_projection.sql`

**Modify:**

- `src/mechanical_design_agent/migrations.py`
- `src/mechanical_design_agent/repository.py` or replace it with
  `src/mechanical_design_agent/knowledge_repository.py`
- database bootstrap and deployment tests

**Work:**

1. Define organizations, design groups, Product Families, assertions, Design
   Lessons, review decisions, and outbox only.
2. Remove every CAD mutation, approval, obligation, delivery, and workspace
   table/query.
3. Add incompatible prior-schema diagnostics without destructive mutation.
4. Keep publication and outbox insertion transactional.
5. Preserve scoped retrieval, supersession, revocation, and projection reads.

**Verify:**

```text
pytest tests/test_migrations.py tests/test_database_deployment.py \
  tests/test_design_lesson_repository.py tests/test_design_lesson_projection.py
```

## Task 6: Publish or decline the immutable review card

**Modify:**

- `src/mechanical_design_agent/design_lesson_workflow.py`
- `src/mechanical_design_agent/knowledge_repository.py`
- `src/mechanical_design_agent/projection.py`

**Add or rewrite:**

- publication, retry, idempotency, supersession, and revocation tests

**Work:**

1. Add `design_lesson_decide` with natural-language semantics.
2. Publish the entire displayed card as one batch.
3. Keep rejection local and publish nothing.
4. Persist database-unavailable retry state without changing model completion.
5. Make publication idempotent by review-card SHA-256.
6. Preserve outbox-driven Neo4j recovery.

**Verify:**

```text
pytest tests/test_design_lesson_workflow.py \
  tests/test_design_lesson_repository.py tests/test_design_lesson_projection.py
```

## Task 7: Decouple Product Family Knowledge

**Modify:**

- Product Family onboarding, matching, library, learning, context, and
  repository modules
- corresponding tests

**Work:**

1. Replace enterprise workspace bindings with family workspace and knowledge
   repository identities.
2. Keep family selection optional for design sessions.
3. Preserve organization/design-group authorization and source provenance.
4. Preserve retrieval quality, onboarding review, publication, and projection.

**Verify:**

```text
pytest tests/test_product_family_onboarding.py \
  tests/test_product_family_matching.py tests/test_context.py tests/test_learning.py
```

## Task 8: Replace MCP registration and service composition

**Modify:**

- `src/mechanical_design_agent/server.py`
- `src/mechanical_design_agent/tool_profiles.py`
- service construction and bootstrap MCP tests

**Work:**

1. Register only `design` and `knowledge-admin` surfaces.
2. Expose the complete normal design flow in `design`.
3. Remove the old monolithic CAD service and its capability gates.
4. Keep database construction lazy until retrieval or publication needs it.
5. Keep CLI/MCP startup possible with no database.

**Verify:**

```text
pytest tests/test_tool_profiles.py tests/test_bootstrap_mcp.py \
  tests/test_service_rules.py
```

## Task 9: Delete the retired implementation

**Delete:**

- enterprise CAD workflow Python modules
- obsolete database migrations
- obsolete tests and fixtures
- obsolete workspace skill
- obsolete docs and examples

**Modify:**

- imports, package resources, build allowlists, third-party inventory, CLI help,
  environment template, README, AGENTS.md, architecture and database docs

**Work:**

1. Remove all unreachable compatibility code.
2. Rename the workspace guide to describe normal design sessions.
3. Rewrite product messaging around one process.
4. Run the absence contract against source and built archives.

**Verify:**

```text
pytest tests/test_retired_surface_absence.py tests/test_public_documentation.py \
  tests/test_public_distribution.py tests/test_agent_skills.py
```

## Task 10: Preserve CAD, validation, and standard-part capability

**Verify and modify only where imports require it:**

- secure filesystem suites
- FCStd security suites
- FreeCAD discovery and runner suites
- validation and assembly suites
- standard-part provider and packaging suites

**Verify:**

```text
pytest tests/test_fcstd_security.py tests/test_freecad_runner.py \
  tests/test_standard_parts.py tests/test_standard_part_packaging.py \
  tests/test_assembly.py tests/test_package_resources.py
```

## Task 11: Update version, changelog, packaging, and release docs

**Modify:**

- `pyproject.toml`
- `uv.lock`
- `src/mechanical_design_agent/__init__.py`
- `third-party-components.toml`
- `CHANGELOG.md`
- release-version tests and public docs

**Work:**

1. Set version `0.7.0` everywhere.
2. Describe the one normal process and breaking database reset.
3. Build and inspect wheel and source distribution.

## Task 12: Full verification

1. Run focused suites from Tasks 1 through 11.
2. Run the complete supported offline suite.
3. Build wheel and source distribution from a clean output directory.
4. Inspect metadata, allowlists, links, forbidden terms, and absolute paths.
5. Run applicable live PostgreSQL/Neo4j acceptance.
6. Run FreeCAD GUI/FreeCADCmd acceptance where the local environment permits.
7. Record precise platform skips; do not claim unexecuted certification.

## Task 13: Transition the basketball carrier and start learning

**Runtime artifact only; do not commit generated design data.**

1. Verify the current FCStd and validation SHA-256 values.
2. Convert its local state to `DesignSession/v1`.
3. Record the user's existing final confirmation.
4. Derive candidate Design Lessons from the model, validation, corrections, and
   manufacturing notes.
5. Create and display the review card or record no-material completion.
6. Stop for one publication decision if a review card exists.

## Completion criteria

- One normal design process works end to end.
- Final confirmation automatically performs Design Lesson evaluation.
- Durable publication asks for exactly one natural-language decision.
- CAD remains usable when knowledge services are unavailable.
- Retired enterprise code and public terminology are absent.
- Product Family Knowledge, Design Lessons, standard parts, validation, and
  FreeCAD remain functional.
- Complete supported tests and package checks pass.
- No external push, tag, or release occurs without separate authorization.
