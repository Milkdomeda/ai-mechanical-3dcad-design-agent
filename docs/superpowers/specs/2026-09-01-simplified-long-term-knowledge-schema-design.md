# Simplified Long-Term Knowledge Schema Design

Date: 2026-09-01
Status: approved in conversation; awaiting written-spec review

## Objective

Define the smallest durable knowledge architecture needed to demonstrate that AI Mechanical Design Agent can learn, match, retrieve, and use Product Family Knowledge and Design Lessons during mechanical design.

PostgreSQL remains the source of truth. The durable business model contains only Product Families, Knowledge Assertions, and Design Lessons. Search exists only to make those records retrievable. Neo4j remains an optional, rebuildable projection and is never required by normal Agent calls.

This is a pre-release schema finalization. The current `001`–`004` target schema has not been deployed successfully, so no compatibility tables, views, or forward migrations are required for it.

## Non-Goals

The long-term knowledge database does not model:

- Design Jobs, Working Copies, source-model revisions, Change Sets, approvals, validation, delivery, or audit events;
- Product Family onboarding progress, review queues, publication workflows, or approval envelopes;
- organization or design-group directories;
- import history, migration receipts, projection queues, or projection cursors;
- enterprise knowledge governance, document management, or generic event sourcing;
- mandatory Neo4j or pgvector runtime dependencies.

Approval and lesson-selection workflows remain in their originating Design Job Workspace. Only the resulting reusable knowledge is published to PostgreSQL.

## Design Principles

Every durable table, field, constraint, status, and index must directly support at least one of:

1. Product Family matching;
2. engineering knowledge retrieval;
3. Design Lessons retrieval;
4. `design_context_build` context construction;
5. basic data and deployment integrity.

If a value is needed only by the one-time migration, it belongs in the ignored migration artifacts under `output/`, not in the product schema.

## Considered Approaches

### Recommended: three typed business tables

Use one typed table for each public knowledge concept and one technical migration ledger. This keeps queries and Agent context explicit while removing workflow and projection layers.

### Rejected: retain review and projection infrastructure

Keeping review records, review decisions, outbox events, projection state, and import receipts would preserve the proposed current schema but would make a small knowledge experiment carry permanent governance and operational machinery.

### Rejected: one generic `knowledge_items` table

A single generic table would reduce the table count but weaken foreign keys, applicability semantics, family matching, Lesson-specific lifecycle rules, and the clarity of `design_context_build` output. Three typed tables are the smaller maintainable model.

## Durable Schema

The public schema contains four tables: three business tables and one technical deployment table.

### `knowledge_schema_migrations`

Fields:

- `version integer primary key`: deterministic migration order.
- `filename text not null unique`: installed resource identity.
- `sha256 char(64) not null`: migration drift detection.
- `applied_at timestamptz not null default now()`: deployment evidence.

This table serves only basic deployment integrity. The finalized pre-release schema begins from one `001_knowledge.sql` baseline migration rather than maintaining the undeployed `001`–`004` chain.

### `product_families`

Fields:

- `id text primary key`: stable Product Family identity.
- `organization_id text not null`: retrieval-scope boundary retained by the current API.
- `design_group_id text not null`: retrieval-scope boundary retained by the current API.
- `canonical_name text not null`: primary family-matching name.
- `aliases text[] not null default '{}'`: exact alternate names.
- `profile jsonb not null default '{}'`: approved family-level mechanisms, interfaces, structures, safeguards, limits, and evidence.
- `search_terms text[] not null default '{}'`: normalized exact terms used by matching and parity probes.
- `search_text text not null`: deterministic searchable text built by the application from the name, aliases, terms, and relevant profile text.
- `status text not null default 'active'`: retrieval lifecycle state.
- `created_at timestamptz not null default now()`: minimum provenance and deterministic export support.

Constraints:

- `status in ('active', 'superseded', 'revoked')`;
- canonical name and scope identifiers must be nonblank;
- `search_text` must be nonblank;
- unique `(organization_id, design_group_id, id)` for scoped foreign keys.

The database does not store organization or design-group names because they do not participate in knowledge matching or Agent context.

### `knowledge_assertions`

Fields:

- `id text primary key`: stable assertion identity exposed to the Agent.
- `organization_id text not null` and `design_group_id text not null`: scope isolation.
- `product_family_id text null`: family-specific assertion; null means design-group general knowledge.
- `subject text not null`, `predicate text not null`, and `object_value jsonb not null`: the engineering fact.
- `applicability jsonb not null default '{}'`: applicable and non-applicable conditions evaluated by the application.
- `evidence jsonb not null default '[]'`: engineering source and confidence evidence required by context consumers.
- `search_terms text[] not null default '{}'`: normalized exact retrieval terms.
- `search_text text not null`: deterministic full-text input.
- `status text not null default 'active'`: current retrieval state.
- `supersedes_id text null`: explicit replacement relation for stale-fact exclusion.
- `created_at timestamptz not null default now()`: minimum provenance.

Constraints:

- status values are `active`, `superseded`, or `revoked`;
- family foreign keys include organization and design-group scope;
- supersession foreign keys remain inside the same scope;
- an assertion cannot supersede itself;
- subject, predicate, and search text must be nonblank.

The proposed `authorization` document is removed. Source type, author identity, risk level, or confidence is retained only when it is useful engineering evidence, in `evidence`.

### `design_lessons`

Fields:

- `id text primary key`: stable Lesson identity exposed to the Agent.
- `organization_id text not null` and `design_group_id text not null`: scope isolation.
- `product_family_id text null`: family-specific Lesson; null means design-group general Lesson.
- `content jsonb not null`: title, problem, root causes, decision, prevention action, and non-applicable conditions.
- `applicability jsonb not null default '{}'`: directly filterable design conditions.
- `provenance jsonb not null default '{}'`: source lesson key, source review SHA-256, decision evidence, and original evidence manifest without a separate review entity.
- `search_terms text[] not null default '{}'`: normalized exact Lesson terms.
- `search_text text not null`: deterministic full-text input.
- `status text not null default 'active'`: current retrieval state.
- `supersedes_id text null`: Lesson replacement relation.
- `created_at timestamptz not null default now()`: minimum provenance.

Constraints mirror Knowledge Assertions: scoped family and supersession foreign keys, nonblank search text, valid lifecycle status, and no self-supersession.

The long-term database accepts only Lessons already approved by the originating workflow. Therefore `approved` is not a durable workflow status; approved source content is imported or published as `active`.

## Indexes and Search

Each business table keeps only indexes that serve a known query:

- Product Families: B-tree `(organization_id, design_group_id, status)`;
- Assertions and Lessons: B-tree `(organization_id, design_group_id, product_family_id, status)`;
- each table: GIN on `search_terms` for normalized exact-term containment;
- each table: expression GIN on `to_tsvector('simple', search_text)` for full-text fallback.

The primary key and composite scoped uniqueness constraints provide their required indexes. No speculative indexes are added.

Search order is deterministic:

1. explicit ID within scope;
2. exact normalized canonical name, alias, or `search_terms` match;
3. PostgreSQL full-text match against `search_text`;
4. stable ID tie-break.

Chinese terms and engineering phrases rely primarily on exact normalized terms. Full-text search is an exploratory fallback, not the parity authority.

`search_text` is populated and verified by application code instead of a stored generated column. This avoids the PostgreSQL 18 generated-expression problems encountered with array conversion while keeping the expression index portable.

No database functions or triggers are required. Search queries remain parameterized repository SQL.

## pgvector

The baseline schema does not create the `vector` extension and has no embedding columns. The current repository does not perform vector retrieval, and 2 families, 43 Assertions, 4 Lessons, and 605 deterministic probes do not justify a mandatory embedding runtime.

Semantic retrieval may be introduced later as a separately reviewed optional capability only after defining the embedding model, model version, dimensions, rebuild behavior, ranking contract, and fallback when embeddings are unavailable. PostgreSQL exact and full-text retrieval must remain functional without it.

## Agent Retrieval and Context Construction

`design_context_build` uses PostgreSQL directly:

1. Validate an explicitly requested family ID in the current organization/design-group scope, or match an active family using design features and the query.
2. Load the active family profile.
3. Retrieve active Assertions for the matched family plus applicable design-group-general Assertions.
4. Retrieve active Lessons for the matched family plus applicable design-group-general Lessons.
5. Evaluate applicability and non-applicable conditions in application code using the supplied design features.
6. Return:
   - `specialized_knowledge` from Product Family profiles;
   - `approved_facts` from Knowledge Assertions;
   - `approved_design_lessons` from Design Lessons.

The current behavior that ignores `design_features` and leaves `approved_facts` empty is corrected as part of the simplified implementation.

No normal matching, retrieval, Lesson search, or context-building request calls Neo4j.

## Optional Neo4j Projection

Neo4j contains only rebuildable representations of Product Families, Knowledge Assertions, and Design Lessons. Obsolete constraints for Product, ModelRevision, SourceNode, FamilyProfile, ProductSubfamily, and projection-state nodes are removed from the new projection baseline.

Projection rebuild reads the three PostgreSQL tables and replaces only nodes owned by this Agent. The PostgreSQL schema has no outbox or projection-state table. A Neo4j connection failure affects only an explicitly requested projection operation and never blocks PostgreSQL retrieval or `design_context_build`.

At the current scale, full rebuild is simpler and safer than incremental event processing. Incremental projection is outside this design.

## One-Time Migration Boundary

The existing read-only exporter and canonical `LongTermKnowledgeExport/v1` remain the migration input. Its organization and design-group collections validate source references but are not inserted into durable lookup tables.

The deterministic target transformation is:

- Product Family `knowledge.approved_profile` and profile evidence become `profile`;
- aliases and retrieval terms become typed arrays;
- approved Assertions become active Assertions;
- Assertion `authorization` is discarded except for engineering-relevant evidence explicitly moved into `evidence`;
- each Design Lesson review card, decision, source key, and evidence are folded into Lesson `provenance`;
- approved Lessons become active Lessons;
- search documents become row-local `search_terms` and `search_text`;
- source organization and design-group IDs remain scalar scope fields.

The importer:

1. requires a distinct empty target database on first import;
2. applies the new baseline schema;
3. transforms and inserts the canonical export in one transaction;
4. computes a deterministic target-payload SHA-256;
5. validates IDs, row counts, relationships, content hashes, and search parity;
6. writes source-export SHA-256, target-payload SHA-256, counts, and probe results to an ignored report under `output/`.

No import receipt is persisted in PostgreSQL. After a successful first import, a rerun against that nonempty target succeeds only when the recomputed canonical target payload exactly matches the expected payload; otherwise it fails closed. Temporary staging tables, if used, are transaction-local and are not part of the durable schema.

## Reusable Migration Assets

The following completed work remains valid:

- repeatable-read, read-only source access;
- explicit source-table allowlist;
- approved-only filtering and latest approved family-profile selection;
- canonical export containing 2 Product Families, 43 Assertions, and 4 Design Lessons;
- canonical JSON and export SHA-256;
- 605 Product Family, Assertion, and Lesson parity probes;
- source backup publication and credential redaction;
- stable identity, supersession, applicability, evidence, and search-term mapping.

Only the canonical-export-to-target transformation and target repository are revised.

## Implementation Transition

Task 3 remains paused. Its uncommitted `001` quoting change, proposed `004`, importer draft, and associated tests are not extended or committed as the target design.

After this specification is approved:

1. preserve the two completed exporter commits and all read-only artifacts;
2. replace the undeployed PostgreSQL migration inventory with the new single baseline;
3. implement the three-table importer and deterministic target validator;
4. update repository matching, Assertion retrieval, Lesson retrieval, applicability filtering, and `design_context_build`;
5. replace outbox synchronization with an explicit optional Neo4j rebuild;
6. run focused tests, complete offline tests, isolated PostgreSQL 18 tests, 605 parity probes, scope-isolation tests, and Neo4j-absent acceptance;
7. create a real target database and cut over `.env.local` only under a later explicit execution approval.

No migration implementation step modifies the old knowledge database.

## Acceptance Criteria

- durable public business schema contains exactly three knowledge tables;
- no Job, Working Copy, Change Set, approval, review, publication, event, summary, outbox, projection-state, or import-receipt table exists;
- Product Family matching works by ID, canonical name, aliases, and exact terms without Neo4j;
- all 43 Assertions are retrievable in the correct scope and can populate `approved_facts`;
- all 4 Lessons are retrievable in the correct scope and can populate `approved_design_lessons`;
- all 605 existing parity probes pass;
- applicability filtering is exercised by `design_context_build` tests;
- unrelated scopes cannot retrieve the records;
- PostgreSQL exact and full-text retrieval works without pgvector;
- Product Family matching, knowledge retrieval, Lesson retrieval, and context construction pass when Neo4j is unavailable;
- optional Neo4j projection can be rebuilt solely from the three PostgreSQL tables;
- the old database, completed canonical export, basketball model, and existing review cards remain unchanged;
- no target database is created and `.env.local` is not changed until the later migration execution gate.

## Rollback and Safety

Before cutover, failure leaves only the isolated new target database for inspection; cleanup of that target is a separate explicit action. The old database and environment configuration remain unchanged. After a separately approved cutover, rollback restores the previous database URL. The migration never drops or mutates the source database.
