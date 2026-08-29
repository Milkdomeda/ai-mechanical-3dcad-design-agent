# Design Lessons Single-Confirmation Publishing Implementation Plan

> **Status:** Implementation plan awaiting user approval
> **Date:** 2026-08-29
> **Target release:** AI Mechanical 3DCAD Design Agent v0.5.0
> **Approved design:** `docs/superpowers/specs/2026-08-29-design-lessons-single-confirmation-publishing-design.md`

## Objective

Make the ordinary Design Lessons workflow require one engineer decision after
the complete immutable Review Card has been displayed:

- `确认发布设计经验` publishes the unchanged Review Card;
- `确认无可发布设计经验` records an immutable reviewed-no-publication outcome;
- projection or retrieval recovery never asks for another confirmation; and
- all existing expert, audit, supersession, rejection, revocation, and legacy
  approval tools remain compatible.

The implementation must preserve PostgreSQL as the authority, bind every
decision to its originating Design Job and exact delivered FCStd revision, and
report publication complete only after projection witnesses and retrieval
verification succeed.

## Implementation constraints

- Work only in the nested public Source of Truth repository.
- Keep the existing Review Card and Design Lesson package contracts readable.
- Add behavior test-first and run focused tests before the complete suite.
- Do not make `模型设计确认` publish Design Lessons.
- Do not add a background worker or a PostgreSQL/Neo4j distributed transaction.
- Do not update the pinned FreeCAD MCP integration.
- Do not push, tag, release, or merge without a later explicit authorization.

## Contract details used by the implementation

### New screening package

Add `DesignLessonScreeningPackage/v1` for the no-publication path. It is a
strict JSON object containing:

- `schema_version`;
- `screening_id` and `codex_session_id`;
- `source` with organization, design group, optional family, working copy,
  before/after FCStd SHA-256 values, and applied change-set IDs;
- `summary` explaining the completed screening;
- non-empty `excluded_candidates`, each with a short candidate description,
  a controlled reason code, rationale, and evidence references;
- `evidence_manifest`, including same-revision baseline geometry validation.

Controlled exclusion reasons are `product_specific`, `insufficient_evidence`,
`duplicate`, `no_material_learning`, and `uncertain_applicability`. The Python
validator remains authoritative at runtime; a packaged JSON Schema documents
and tests the installed contract.

### Review and decision receipts

`DesignLessonReview/v2` adds `review_outcome` (`publish` or `no_publish`) while
the reader continues to accept v1 records as `publish`. A decision receipt is
canonical UTF-8 JSON stored beside the Review Card in the originating Job. Its
stable fields are:

- schema version and outcome;
- Review ID, Job ID and reviewed Job revision;
- working-copy ID;
- package, Review Card, final-model, and approved-artifact SHA-256 bindings;
- configured actor ID; and
- confirmation mode (`single_confirmation`).

The receipt SHA-256 is computed from canonical JSON. PostgreSQL stores the
receipt hash and Job-relative path in the same authoritative decision
transaction. The filesystem receipt is written once before that transaction
and reverified by the repository pre-commit verifier; an interrupted attempt
can therefore be retried idempotently without changing the receipt bytes.
Legacy rows use confirmation mode `legacy_review_id` and may have no decision
receipt.

### Public result mapping

The two new tools return `DesignLessonPublication/v1`:

- `stored-and-retrievable` -> `published`;
- `approved-retrieval-pending` -> `publishing`;
- expected pre-approval binding or authorization failures -> `blocked` with a
  stable reason code and no private path or incident text;
- `reviewed-no-publishable-lesson` remains the public terminal state of the
  no-publication path.

Repeated calls return the same receipt and terminal result. Unexpected
programming or infrastructure errors remain errors; they are not mislabeled as
valid engineering decisions.

## Task 1: Add the additive PostgreSQL migration and installed-resource contract

**Files:**

- Create: `src/mechanical_design_agent/resources/migrations/postgres/017_design_lesson_single_confirmation.sql`
- Modify: `src/mechanical_design_agent/bootstrap_runtime.py`
- Modify: `src/mechanical_design_agent/database_bootstrap.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/test_database_deployment_live.py`
- Modify: `tests/test_windows_database_live.py`
- Modify: `tests/windows_release_helpers.py`
- Modify: `tests/test_packaging.py`

1. Add failing migration-contract tests for migration 017 and extend every
   exact ordered migration manifest from 001-016 to 001-017. Include the
   currently stale `database_bootstrap.py` list in the correction so clean
   installed bootstrap verifies the complete package-owned sequence.
2. In the SQL contract tests require:
   - `review_outcome` backfilled/defaulted to `publish`;
   - nullable `lesson_id` only for `no_publish`;
   - the new terminal state;
   - `confirmation_mode`, `decision_receipt_sha256`, and
     `decision_receipt_path`;
   - publish/no-publish state and published-Lesson consistency checks; and
   - preservation of existing SHA-256 and foreign-key bindings.
3. Run the failing focused tests:

   ```bash
   uv run --frozen --group test pytest -q \
     tests/test_migrations.py \
     tests/test_packaging.py
   ```

4. Implement migration 017 by dropping and recreating only the affected named
   constraints, backfilling existing rows without reapproval, and adding the
   new consistency constraints as `NOT VALID` before validation where needed
   for safe upgrade behavior.
5. Add migration 017 to the runtime, database bootstrap, Windows live bundle,
   packaging, and installed-wheel manifests.
6. Re-run the focused tests and commit the migration/resource slice.

## Task 2: Define and validate the no-publication screening package

**Files:**

- Create: `src/mechanical_design_agent/resources/schemas/design-lesson-screening-package-v1.schema.json`
- Modify: `src/mechanical_design_agent/design_lessons.py`
- Modify: `src/mechanical_design_agent/bootstrap_runtime.py`
- Modify: `tests/test_design_lessons.py`
- Modify: `tests/test_workspace_bootstrap.py`
- Modify: `tests/test_public_distribution.py`

1. Add failing tests for valid and invalid `DesignLessonScreeningPackage/v1`
   values: strict top-level fields, safe IDs, exact source bindings, finite JSON
   values, controlled reason codes, nonblank rationales, evidence references,
   unique evidence IDs, required geometry validation, and SHA-256 validation.
2. Add `validate_design_lesson_screening_package` and a small dispatcher that
   accepts exactly the existing Lesson package or the new screening package.
   Reuse the existing source/evidence helpers instead of creating divergent
   validation rules.
3. Package the JSON Schema through the runtime resource manifest and verify
   wheel/sdist ownership and schema/code agreement.
4. Run:

   ```bash
   uv run --frozen --group test pytest -q \
     tests/test_design_lessons.py \
     tests/test_workspace_bootstrap.py \
     tests/test_public_distribution.py
   ```

5. Commit the package-contract slice.

## Task 3: Extend immutable staging, Review Cards, and Job receipts

**Files:**

- Modify: `src/mechanical_design_agent/design_lessons.py`
- Modify: `src/mechanical_design_agent/lesson_reviews.py`
- Modify: `tests/test_design_lesson_reviews.py`

1. Add failing filesystem tests covering:
   - publish and no-publish staging;
   - `DesignLessonReview/v2` and v1 read compatibility;
   - outcome-specific Review Card rendering;
   - screening rationale and exclusions shown in full;
   - SHA drift, symlink, noncanonical JSON, and cross-Job rejection;
   - deterministic single-write decision receipts; and
   - idempotent inspection of an existing identical receipt.
2. Generalize review staging to dispatch by package schema while keeping all
   existing Lesson staging paths and return values compatible.
3. Extend `DesignLessonReviewStore.prepare`, `verify`, `_review_card`, and
   `_render_markdown` so v2 records include `review_outcome`; v1 records are
   interpreted as publish records.
4. Add a receipt writer/verifier under the existing Job review directory. It
   must use canonical JSON, managed-path checks, atomic publication, no
   overwrite, and redacted public output.
5. Run:

   ```bash
   uv run --frozen --group test pytest -q tests/test_design_lesson_reviews.py
   ```

6. Commit the immutable-artifact slice.

## Task 4: Persist outcome-aware reviews and immutable decisions

**Files:**

- Modify: `src/mechanical_design_agent/repository.py`
- Modify: `tests/test_design_lesson_repository.py`
- Modify: `tests/test_design_lesson_outbox.py`

1. Add failing repository tests for creating publish/no-publish reviews,
   reading migrated publish rows, superseding a pending card for the same Job
   and final model even when the replacement outcome changes, and rejecting
   cross-scope or stale replacements.
2. Extend `create_design_lesson_review` to persist `review_outcome`, nullable
   Lesson identity for screening packages, and the exact Job/final-artifact
   bindings.
3. Extend `approve_design_lesson` with optional single-confirmation receipt
   inputs. Keep legacy callers unchanged; when supplied, store confirmation
   mode and receipt bindings in the same transaction as the Lesson,
   assertions, review transition, and outbox events.
4. Add `record_design_lesson_review_no_publish` that:
   - locks and authorizes the actor and Review row;
   - accepts only an awaiting `no_publish` Review;
   - rechecks the Job/working-copy/final-artifact bindings through the service
     pre-commit verifier;
   - stores reviewer, confirmation mode, receipt hash/path, and the terminal
     state atomically;
   - emits an auditable `design_lesson_review.no_publish` lifecycle event; and
   - creates no Design Lesson event, knowledge assertion, search document, or
     Lesson projection event.
5. Add idempotent reads for the same stored receipt and fail closed on receipt
   or outcome divergence.
6. Run:

   ```bash
   uv run --frozen --group test pytest -q \
     tests/test_design_lesson_repository.py \
     tests/test_design_lesson_outbox.py
   ```

7. Commit the PostgreSQL-authority slice.

## Task 5: Add the one-confirmation service orchestration

**Files:**

- Modify: `src/mechanical_design_agent/service.py`
- Modify: `tests/test_design_lesson_reviews.py`

1. Add failing service tests for `design_lesson_review_publish`:
   - surrounding whitespace is trimmed but internal/extra text is rejected;
   - the phrase contains no Review ID or digest;
   - only a publish Review is accepted;
   - Job, revision, organization, design-group, working-copy, final FCStd,
     approved artifact, evidence, package, and card drift fail before approval;
   - a superseded/rejected/invalid card cannot be used;
   - the family-owner authority remains required;
   - the current PostgreSQL approval transaction is reused;
   - successful completion returns `published` and a stable receipt;
   - a durable pending approval returns `publishing` and retries without a new
     confirmation; and
   - repeated calls are idempotent and create no duplicate events.
2. Refactor the existing locked approval path only enough to accept optional
   decision-receipt metadata. Keep `design_lesson_review_approve` byte-for-byte
   compatible at its public boundary.
3. Add helpers to create/verify the receipt, map internal review states to
   `DesignLessonPublication/v1`, and redact expected blocked failures into
   stable reason codes.
4. Make one bounded call to the existing `_complete_design_lesson_review` path
   after durable approval. Never map `approved-retrieval-pending` to complete.
5. Run the focused review tests and commit the publish-orchestration slice.

## Task 6: Add the reviewed-no-publication service path

**Files:**

- Modify: `src/mechanical_design_agent/service.py`
- Modify: `tests/test_design_lesson_reviews.py`
- Modify: `tests/test_design_lesson_projection.py`

1. Add failing tests for `design_lesson_review_no_publish` with the exact
   `确认无可发布设计经验` phrase, outcome mismatch, stale bindings, authorization,
   receipt idempotency, and replacement Review Cards.
2. Reuse the same Job lock and immutable binding verification used by publish,
   but call the repository no-publication transaction instead of Lesson
   approval.
3. Return `reviewed-no-publishable-lesson` only after the decision row and
   receipt are durable.
4. Assert that the path produces no Lesson, assertion, search-document, or
   Design Lesson Neo4j node/relation. If the review lifecycle event is
   projected, it may update only the Review audit node and must not create a
   `PUBLISHED_AS` relationship.
5. Run:

   ```bash
   uv run --frozen --group test pytest -q \
     tests/test_design_lesson_reviews.py \
     tests/test_design_lesson_projection.py
   ```

6. Commit the no-publication slice.

## Task 7: Expose the two additive MCP tools and preserve old clients

**Files:**

- Modify: `src/mechanical_design_agent/server.py`
- Modify: `tests/test_service_rules.py`
- Modify: `tests/test_bootstrap_mcp.py`
- Modify: `tests/test_public_distribution.py`

1. Add failing MCP tests for tool discovery, capability gating, argument
   validation, unsafe IDs, exact confirmation text, optional internal Job
   binding, and JSON result schemas.
2. Register:
   - `design_lesson_review_publish(review_id, confirmation, job_id="",
     expected_job_revision=-1)`; and
   - `design_lesson_review_no_publish(review_id, confirmation, job_id="",
     expected_job_revision=-1)`.
3. Keep the old prepare/approve/reject/status/stage/audit/supersede/revoke tool
   signatures and capability assignments unchanged.
4. Extend the installed MCP tool inventory and Windows helper fixture.
5. Run:

   ```bash
   uv run --frozen --group test pytest -q \
     tests/test_service_rules.py \
     tests/test_bootstrap_mcp.py \
     tests/test_public_distribution.py
   ```

6. Commit the MCP-boundary slice.

## Task 8: Prove end-to-end recovery and backward compatibility

**Files:**

- Modify: `tests/test_design_lesson_reviews.py`
- Modify: `tests/test_design_lesson_projection.py`
- Modify: `tests/test_design_lesson_outbox.py`
- Modify: `tests/test_database_deployment_live.py`
- Modify: `tests/test_windows_database_live.py`

1. Add synthetic end-to-end coverage for one-confirmation publish from Review
   Card through PostgreSQL, outbox, projection witnesses, retrieval, and
   terminal receipt.
2. Inject transient Neo4j and retrieval failures, prove the first call returns
   `publishing`, and prove status/repeated publish completes with the same
   approval and receipt.
3. Prove a clean database applies 001-017, a v0.4-shaped review row upgrades as
   `publish`, and preexisting pending/terminal reviews remain resumable and
   idempotent.
4. Prove projection rebuild from PostgreSQL reproduces publish Reviews and
   Lessons while no-publication Reviews create no Lesson relationship.
5. Preserve all legacy approval/rejection/status tests and add explicit
   compatibility assertions where the new fields appear in repository rows.
6. Run the focused offline suite, then the opt-in live PostgreSQL/Neo4j nodes
   when the local services are configured. Record environmental skips rather
   than weakening them.
7. Commit the recovery/compatibility slice.

## Task 9: Make the simplified flow the documented Agent default

**Files:**

- Modify: `AGENTS.md`
- Modify: `.agents/skills/mechanical-design-job-workspace/SKILL.md`
- Modify: relevant files under `.agents/skills/mechanical-design-job-workspace/references/`
- Modify: `README.md`
- Modify: `docs/ENGINEER_LEARNING_PLAYBOOK.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DATABASE_DEPLOYMENT.md`
- Modify: `tests/test_public_documentation.py`
- Modify: `tests/test_public_release_contract.py`

1. Add failing documentation-contract tests that require the two exact user
   phrases, complete-card-before-confirmation rule, silent retry behavior,
   truthful public states, separate model confirmation, and reviewed
   no-publication outcome.
2. Update Agent instructions and the Design Job Skill so IDs, digests, status
   polling, and infrastructure retries remain internal. Do not make
   Superpowers brainstorming mandatory; retain it as the recommended optional
   discovery workflow already established by the project.
3. Update architecture and database documentation with the new state,
   migration 017, authoritative receipt, and no-publication semantics.
4. Keep old MCP tools documented as compatibility/expert surfaces, not the
   default engineer interaction.
5. Run documentation and public-release-contract tests and commit the docs
   slice.

## Task 10: Bump the single product version to 0.5.0 and run release gates

**Files:**

- Modify: `pyproject.toml`
- Modify: `src/mechanical_design_agent/__init__.py`
- Modify: `uv.lock`
- Modify: `third-party-components.toml`
- Modify: `tests/third_party_licensing_helpers.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_public_distribution.py`
- Modify: `tests/test_windows_packaging.py`
- Modify: any exact version fixtures located by `rg '0\.4\.0|v0\.4\.0'`

1. Update the project version to `0.5.0` in the package source of truth and
   synchronize every tested metadata copy, lockfile entry, release fixture,
   README statement, and changelog entry. Do not change third-party dependency
   versions as part of this feature.
2. Run focused feature and migration tests:

   ```bash
   uv run --frozen --group test pytest -q \
     tests/test_design_lessons.py \
     tests/test_design_lesson_reviews.py \
     tests/test_design_lesson_repository.py \
     tests/test_design_lesson_outbox.py \
     tests/test_design_lesson_projection.py \
     tests/test_migrations.py \
     tests/test_service_rules.py \
     tests/test_bootstrap_mcp.py
   ```

3. Run the complete supported offline suite:

   ```bash
   uv run --frozen --group test pytest -q
   ```

4. Run formatting/diff and public-boundary checks:

   ```bash
   git diff --check
   uv run --frozen --group test pytest -q \
     tests/test_public_release_contract.py \
     tests/test_public_distribution.py \
     tests/test_public_documentation.py \
     tests/test_third_party_licensing.py \
     tests/test_windows_release_evidence.py \
     tests/test_windows_packaging.py
   ```

5. Build wheel and sdist, then run the existing clean-install tests that verify
   package resources, CLI/MCP entry points, version, and installed tool
   inventory:

   ```bash
   uv build --frozen --wheel --sdist --out-dir dist
   uv run --frozen --group test pytest -q \
     tests/test_public_distribution.py \
     tests/test_packaging.py
   ```

6. Run the public-repository scans for absolute paths, usernames, secrets,
   generated Job artifacts, FCStd/STEP files, runtime knowledge, and private
   evidence. Verify `git status`, inspect the complete diff, and conduct one
   overall code review against the approved design and this plan.
7. Commit the version/release-readiness slice. Stop before merge, push, tag, or
   release and report any live macOS/Windows gates that still require protected
   infrastructure.

## Completion checklist

- [ ] One displayed immutable publish Review Card needs one simple confirmation.
- [ ] One displayed immutable screening card needs one no-publication confirmation.
- [ ] User confirmation text contains no internal identifier or digest.
- [ ] PostgreSQL decision, Lesson, assertions, and outbox writes remain atomic.
- [ ] Pending projection/retrieval never requests reapproval.
- [ ] Public completion is truthful and receipt-stable.
- [ ] No-publication creates no Lesson or Lesson projection.
- [ ] Old MCP contracts and migrated reviews remain compatible.
- [ ] Migration 017 is included in source, wheel, sdist, bootstrap, and Windows manifests.
- [ ] Agent instructions display the full card before asking for confirmation.
- [ ] Version metadata is consistently 0.5.0.
- [ ] Focused, complete offline, packaging, and public-boundary tests pass.
- [ ] Applicable live database and protected-platform results are recorded.
- [ ] No merge, push, tag, or release occurs without explicit authorization.
