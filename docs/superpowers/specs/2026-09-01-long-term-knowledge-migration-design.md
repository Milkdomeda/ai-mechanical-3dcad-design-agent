# Long-Term Knowledge Migration Design

Date: 2026-09-01
Status: approved in conversation; awaiting implementation-plan review

## Objective

Move the existing reusable engineering knowledge into the current knowledge-only PostgreSQL schema without carrying forward the retired design-governance architecture. Preserve Product Family Knowledge, approved Design Lessons, retrieval behavior, product-family matching, and Neo4j projection capability.

The source database remains unchanged and available for rollback. The migration writes to a newly created database and cuts runtime configuration over only after all acceptance checks pass.

## Scope

Migrate only:

- organization and design-group identities required to scope knowledge;
- Product Family identities, canonical names, aliases, and current engineering status;
- the newest approved Product Family profile for each family;
- approved Product Family knowledge assertions;
- retrieval terms and search text associated with migrated knowledge;
- approved Design Lesson content, applicability, exclusions, evidence, and search terms;
- the current review records required to provide immutable publication provenance;
- new projection outbox events generated from the migrated records.

Do not migrate:

- Design Jobs or job events;
- Working Copies or source-snapshot bindings;
- Change Sets or change audit events;
- approval envelopes, mutation authorizations, or obligation decisions;
- old interaction, question, answer, ingestion, review-event, artifact, validation, or delivery lifecycle state;
- rejected or merely proposed Product Family profiles;
- blocked or unpublished Design Lesson candidates;
- old Neo4j projection state;
- standard-part runtime records, which remain managed by the current standard-part configuration and provenance interfaces.

## Target Topology

Create a new PostgreSQL database in the existing local PostgreSQL service. Apply only the package-owned current migrations to that empty database. The old database is never modified by the importer and remains the rollback source.

After relational acceptance passes, switch `MECH_DESIGN_DATABASE_URL` in the selected environment file to the new database, initialize the current Neo4j constraints, and rebuild the Neo4j projection exclusively from the new PostgreSQL outbox.

No retired table or compatibility view is created in the target database.

## Product Family Mapping

For each source Product Family:

1. Preserve its stable family ID, organization ID, design-group ID, canonical name, and aliases.
2. Normalize source operational statuses into the current knowledge status. A family with approved reusable knowledge remains `active`; superseded or revoked knowledge is mapped only when the source explicitly records that meaning.
3. Build the target `knowledge` document from engineering-relevant source data:
   - the newest approved family profile;
   - approved descriptive facts and limits;
   - mechanisms, interfaces, structures, safeguards, and applicability information;
   - normalized retrieval terms and source search text.
4. Exclude source paths, actor workflow fields, approval policy, onboarding progress, working-copy policy, and other operational state.
5. Convert every approved source assertion to the current assertion model while preserving its stable identity, subject, predicate, object value, evidence, applicability, supersession relation, and authorization scope.

Proposed and rejected profiles are not included. Their absence cannot remove an approved fact because approved assertions and the latest approved profile are migrated independently.

## Design Lesson Mapping

Only source `design_lesson_events` whose status is `approved` are migrated. Each becomes one current Design Lesson with a deterministic ID and review SHA-256.

The mapping is:

- `problem` remains `problem`;
- source corrections become the current `decision`;
- source prevention becomes `prevention_action`;
- applicability and non-applicable conditions are retained;
- evidence manifest references remain evidence;
- search terms remain search terms;
- source family ID becomes `product_family_id` when the target family exists;
- source approval evidence is represented by the current immutable review record;
- supersession and revocation semantics are retained when present.

Old review queues, summaries, packages, working-copy references, confirmation modes, and publication blockers are not copied. They are source-process records, not reusable engineering knowledge.

## Migration Interface and Safety

Add an explicit knowledge migration command. It accepts separate source and target database URLs, refuses identical source and target identities, and supports a read-only analysis mode before writing.

The command performs these stages:

1. inventory and validate the source records;
2. produce a migration manifest containing counts and deterministic content hashes;
3. apply the current schema to an empty target database;
4. import records in one target transaction;
5. validate referential integrity and content hashes;
6. run retrieval and family-matching parity probes;
7. emit a machine-readable report;
8. allow configuration cutover only after a passed report.

The importer is idempotent: repeating it with the same manifest results in no duplicate records. Any changed source content or target conflict fails closed. Secrets and database passwords are never written to reports.

Before creating the target database, export the in-scope source tables to an ignored local backup artifact. The backup and migration report remain outside the public repository.

## Retrieval and Product-Family Matching Parity

The migration must not reduce the currently available retrieval surface. Acceptance therefore includes:

- every source family alias remains associated with the same family ID;
- every source approved assertion remains queryable in the same organization and design-group scope;
- every legacy `exact_terms` entry and meaningful `search_text` probe returns the same family ID after migration;
- every approved Design Lesson search term returns its migrated lesson;
- applicability and family filters return the same or a stricter authorized result set;
- unrelated organization or design-group scopes cannot retrieve the migrated records;
- PostgreSQL full-text indexes exist and are used by the current repository;
- Neo4j nodes and relationships can be rebuilt from PostgreSQL without consulting the old schema.

The current repository does not use legacy lifecycle tables for retrieval or family matching, so excluding those tables cannot remove an active retrieval input. Any source search term that cannot be represented by the current schema is added to the target family's knowledge document or lesson search-term array rather than dropped.

## Acceptance Criteria

For the currently observed source database, the migration must demonstrate:

- 2 Product Families migrated;
- 43 approved knowledge assertions migrated;
- the newest approved family profile retained;
- 4 approved Design Lessons migrated;
- no proposed or rejected profile migrated;
- no blocked or unpublished lesson candidate migrated;
- zero retired governance tables or compatibility views in the target database;
- deterministic rerun with no duplicate data;
- pre/post retrieval parity for the complete legacy term corpus;
- passed Product Family filtering and scope-isolation tests;
- successful Neo4j rebuild from the target outbox;
- successful publication and retrieval of basketball-carrier lesson 2;
- unchanged basketball model and original lesson review card.

## Testing

Automated tests cover source-schema recognition, field mapping, exclusion rules, deterministic identities and hashes, idempotency, conflict rejection, transaction rollback, retrieval parity, scope isolation, CLI redaction, and the absence of retired table names in the target migration inventory.

An isolated live PostgreSQL test creates source and target databases, imports a representative source fixture, applies the migration twice, validates parity, and drops only those test databases. The production migration runs only after the offline suite and isolated live test pass.

## Rollback

If any validation, retrieval probe, or projection rebuild fails, runtime configuration remains pointed at the source database and lesson publication remains retryable. If a post-cutover check fails, restore the previous database URL from the local configuration backup. The source database is not dropped or mutated as part of this work.
