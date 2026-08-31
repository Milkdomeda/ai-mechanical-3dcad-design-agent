# Long-Term Knowledge Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate only reusable Product Family Knowledge and approved Design Lessons from the existing PostgreSQL database into the current knowledge-only schema, prove retrieval parity, switch configuration safely, and publish basketball-carrier lesson 2.

**Architecture:** A source reader recognizes only the long-term-knowledge tables and converts their approved records into a deterministic canonical export. A separate target importer applies the existing current migrations to a new database, imports the canonical records transactionally, and validates content and retrieval parity before an atomic environment-file cutover. The source database is read-only throughout and no retired governance table, compatibility view, or runtime dependency is created in the target.

**Tech Stack:** Python 3.12+, psycopg 3, PostgreSQL 18 with pgvector, Neo4j 2026.06, pytest, pathlib, package-owned SQL migrations.

**Spec:** `docs/superpowers/specs/2026-09-01-long-term-knowledge-migration-design.md`

## Global Constraints

- The source database remains unchanged and available for rollback.
- The target database contains only the current package-owned knowledge schema.
- Migrate Product Family identities, aliases, the newest approved profile, 43 approved assertions, approved retrieval terms, and 4 approved Design Lessons.
- Do not migrate retired design-governance lifecycle state or create compatibility tables/views for it.
- Proposed/rejected profiles and blocked/unpublished lesson candidates are excluded.
- PostgreSQL remains authoritative; Neo4j is rebuilt from the target outbox.
- Runtime cutover occurs only after content, scope, retrieval, and idempotency checks pass.
- Database credentials and URLs must never appear in reports, command output, or committed files.
- Preserve macOS and Windows support; do not depend on POSIX shell behavior.
- The basketball model and original lesson review card remain byte-for-byte unchanged.

---

## File Structure

- Create `src/mechanical_design_agent/long_term_knowledge_migration.py`: canonical export types, pure source-to-current mapping, deterministic identities, manifest generation, and parity probe construction.
- Create `src/mechanical_design_agent/long_term_knowledge_database.py`: read-only source queries, target database creation, transactional import, backup publication, validation, and cutover orchestration.
- Modify `src/mechanical_design_agent/cli.py`: add `knowledge migrate-long-term` with analyze and execute modes.
- Modify `src/mechanical_design_agent/workspace_bootstrap.py`: atomically update one selected environment-file key without exposing its value.
- Create `tests/test_long_term_knowledge_migration.py`: pure mapping, exclusions, hashes, status normalization, and parity-corpus tests.
- Create `tests/test_long_term_knowledge_database.py`: fake-connection tests for source allowlists, transactions, idempotency, conflict handling, backup, and cutover.
- Create `tests/test_long_term_knowledge_migration_live.py`: isolated source/target PostgreSQL acceptance.
- Modify `tests/test_cli.py` or create `tests/test_knowledge_migration_cli.py`: parser, redaction, analyze mode, execute mode, and exit-code coverage.
- Modify `docs/DATABASE_DEPLOYMENT.md`: document the one-time migration and rollback commands without presenting retired process concepts as product architecture.

### Task 1: Canonical Long-Term Knowledge Export

**Files:**
- Create: `src/mechanical_design_agent/long_term_knowledge_migration.py`
- Create: `tests/test_long_term_knowledge_migration.py`

**Interfaces:**
- Consumes: JSON-compatible rows returned by the source reader.
- Produces: `build_long_term_export(source: Mapping[str, Sequence[Mapping[str, object]]]) -> LongTermKnowledgeExport`, `LongTermKnowledgeExport.as_dict() -> dict[str, object]`, `LongTermKnowledgeExport.sha256 -> str`, and `build_parity_probes(export: LongTermKnowledgeExport) -> tuple[RetrievalProbe, ...]`.

- [ ] **Step 1: Write failing tests for the exact allowed source records**

```python
def test_export_contains_only_approved_long_term_knowledge():
    export = build_long_term_export(source_fixture())
    assert [row["id"] for row in export.product_families] == [
        "PF-PILOT-001",
        "horizontal-vacuum-vessel",
    ]
    assert len(export.knowledge_assertions) == 43
    assert len(export.design_lessons) == 4
    assert "proposed-profile" not in canonical_json(export.as_dict())
    assert "blocked-summary" not in canonical_json(export.as_dict())


def test_export_preserves_family_matching_inputs():
    export = build_long_term_export(source_fixture())
    family = next(row for row in export.product_families if row["id"] == "PF-PILOT-001")
    assert family["canonical_name"] == "Pilot Product Family"
    assert family["aliases"] == ["pilot-family", "PF pilot"]
    assert "guide rail" in family["knowledge"]["retrieval_terms"]
    assert family["knowledge"]["approved_profile"]["mechanism_description"]


def test_design_lesson_mapping_retains_retrieval_and_applicability():
    export = build_long_term_export(source_fixture())
    lesson = export.design_lessons[0]["lesson"]
    assert lesson["problem"]
    assert lesson["decision"]
    assert lesson["prevention_action"]
    assert lesson["search_terms"]
    assert lesson["applicability"]
    assert lesson["non_applicable_conditions"]
```

- [ ] **Step 2: Run the tests and verify they fail because the module does not exist**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_migration.py
```

Expected: FAIL with `ModuleNotFoundError: mechanical_design_agent.long_term_knowledge_migration`.

- [ ] **Step 3: Implement immutable export types and strict status mapping**

```python
@dataclass(frozen=True)
class RetrievalProbe:
    query: str
    kind: Literal["product_family", "design_lesson"]
    expected_id: str
    product_family_id: str | None = None


@dataclass(frozen=True)
class LongTermKnowledgeExport:
    organizations: tuple[dict[str, object], ...]
    design_groups: tuple[dict[str, object], ...]
    product_families: tuple[dict[str, object], ...]
    knowledge_assertions: tuple[dict[str, object], ...]
    design_lesson_reviews: tuple[dict[str, object], ...]
    design_lessons: tuple[dict[str, object], ...]
    source_counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "LongTermKnowledgeExport/v1",
            "organizations": list(self.organizations),
            "design_groups": list(self.design_groups),
            "product_families": list(self.product_families),
            "knowledge_assertions": list(self.knowledge_assertions),
            "design_lesson_reviews": list(self.design_lesson_reviews),
            "design_lessons": list(self.design_lessons),
            "source_counts": self.source_counts,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()
```

Implement `build_long_term_export` with explicit source allowlists:

```python
ALLOWED_SOURCE_KEYS = frozenset({
    "organizations",
    "design_groups",
    "product_families",
    "family_profiles",
    "knowledge_assertions",
    "knowledge_search_documents",
    "design_lesson_events",
})
```

Select only `family_profiles.status == "approved"`, taking the highest revision per family. Select only `knowledge_assertions.status == "approved"` and `design_lesson_events.status == "approved"`. Map a family containing approved reusable knowledge to current status `active`. Generate each lesson review card and SHA-256 from canonical mapped content; never depend on a source Working Copy ID for target identity.

- [ ] **Step 4: Add deterministic and rejection tests**

```python
def test_export_is_deterministic_under_source_row_reordering():
    first = build_long_term_export(source_fixture())
    second = build_long_term_export(reversed_source_fixture())
    assert first.sha256 == second.sha256
    assert first.as_dict() == second.as_dict()


@pytest.mark.parametrize("status", ["proposed", "rejected"])
def test_nonapproved_profile_is_excluded(status):
    source = source_fixture(profile_status=status)
    export = build_long_term_export(source)
    assert all("approved_profile" not in row["knowledge"] for row in export.product_families)


def test_dangling_family_reference_fails_closed():
    source = source_fixture()
    source["knowledge_assertions"][0]["family_id"] = "missing-family"
    with pytest.raises(ValueError, match="missing-family"):
        build_long_term_export(source)
```

- [ ] **Step 5: Run the focused tests**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_migration.py
```

Expected: PASS.

- [ ] **Step 6: Commit the canonical mapper**

```bash
git add src/mechanical_design_agent/long_term_knowledge_migration.py tests/test_long_term_knowledge_migration.py
git commit -m "feat: map reusable long-term knowledge"
```

### Task 2: Read-Only Source Inventory and Backup

**Files:**
- Create: `src/mechanical_design_agent/long_term_knowledge_database.py`
- Create: `tests/test_long_term_knowledge_database.py`

**Interfaces:**
- Consumes: `source_database_url: str`, `backup_path: Path`.
- Produces: `read_source_export(source_database_url: str) -> LongTermKnowledgeExport` and `publish_source_backup(export: LongTermKnowledgeExport, destination: Path) -> dict[str, object]`.

- [ ] **Step 1: Write failing tests for read-only behavior and exact table access**

```python
def test_source_reader_uses_read_only_transaction_and_allowed_tables(fake_connection):
    read_source_export("postgresql://redacted", connect=fake_connection.connect)
    assert "SET TRANSACTION READ ONLY" in fake_connection.statements
    queried = {table for table in SOURCE_TABLES if table in " ".join(fake_connection.statements)}
    assert queried == set(SOURCE_TABLES)
    assert not any(name in " ".join(fake_connection.statements) for name in RETIRED_TABLE_NAMES)


def test_backup_contains_canonical_export_and_no_database_url(tmp_path):
    export = build_long_term_export(source_fixture())
    result = publish_source_backup(export, tmp_path / "source-export.json")
    payload = (tmp_path / "source-export.json").read_text(encoding="utf-8")
    assert export.sha256 == result["sha256"]
    assert "postgresql://" not in payload
```

- [ ] **Step 2: Run the tests and verify the database adapter is missing**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_database.py
```

Expected: FAIL importing `long_term_knowledge_database`.

- [ ] **Step 3: Implement a fixed-query source reader**

Define only these source queries:

```python
SOURCE_QUERIES = {
    "organizations": "SELECT id,name FROM organizations ORDER BY id",
    "design_groups": "SELECT id,organization_id,name FROM design_groups ORDER BY id",
    "product_families": (
        "SELECT id,organization_id,design_group_id,canonical_name,aliases,status,config,revision "
        "FROM product_families ORDER BY id"
    ),
    "family_profiles": (
        "SELECT id,family_id,revision,status,profile,evidence,created_at "
        "FROM family_profiles ORDER BY family_id,revision"
    ),
    "knowledge_assertions": (
        "SELECT id,organization_id,design_group_id,family_id,subject_ref,predicate,object_value," 
        "applicability,non_applicable_conditions,evidence,status,supersedes,source_kind,risk_level," 
        "confidence,created_by,created_at FROM knowledge_assertions ORDER BY id"
    ),
    "knowledge_search_documents": (
        "SELECT assertion_id,family_id,exact_terms,search_text FROM knowledge_search_documents "
        "ORDER BY assertion_id"
    ),
    "design_lesson_events": (
        "SELECT id,lesson_key,revision,organization_id,source_design_group_id,source_family_id,title," 
        "problem,root_causes,corrections,prevention,applicability,non_applicable_conditions," 
        "search_terms,evidence_manifest,status,supersedes,approved_by,approval_text,approved_at "
        "FROM design_lesson_events ORDER BY lesson_key,revision"
    ),
}
```

Open the source connection with a repeatable-read, read-only transaction. Convert UUIDs, timestamps, arrays, and JSON values to deterministic JSON-compatible values before calling `build_long_term_export`.

- [ ] **Step 4: Implement immutable backup publication**

Serialize `LongTermKnowledgeExport.as_dict()` with `canonical_json`, publish it with `atomic_publish_new`, set it read-only, and return only the file path, SHA-256, and record counts. Refuse symlinked or existing mismatched destinations.

- [ ] **Step 5: Run focused tests**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_database.py
```

Expected: PASS.

- [ ] **Step 6: Commit source inventory and backup support**

```bash
git add src/mechanical_design_agent/long_term_knowledge_database.py tests/test_long_term_knowledge_database.py
git commit -m "feat: export approved knowledge safely"
```

### Task 3: New-Database Import, Idempotency, and Validation

**Files:**
- Modify: `src/mechanical_design_agent/long_term_knowledge_database.py`
- Modify: `tests/test_long_term_knowledge_database.py`
- Modify: `src/mechanical_design_agent/knowledge_repository.py`
- Create: `src/mechanical_design_agent/resources/migrations/postgres/004_knowledge_import_receipts.sql`
- Modify: `tests/test_design_lesson_repository.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `LongTermKnowledgeExport`, `source_database_url`, and `target_database_name`.
- Produces: `create_target_database(source_database_url: str, target_database_name: str) -> str`, `import_long_term_export(target_database_url: str, export: LongTermKnowledgeExport) -> MigrationImportResult`, and `validate_target(target_database_url: str, export: LongTermKnowledgeExport) -> dict[str, object]`.

- [ ] **Step 1: Write failing tests for target isolation and exact table inventory**

```python
def test_target_name_must_be_safe_and_different_from_source():
    with pytest.raises(ValueError, match="different"):
        create_target_database(SOURCE_URL, "mechanical_design")
    with pytest.raises(ValueError, match="safe"):
        create_target_database(SOURCE_URL, "knowledge;drop database")


def test_target_validation_rejects_extra_tables(fake_target):
    fake_target.tables.add("design_jobs")
    with pytest.raises(KnowledgeMigrationError, match="unexpected target tables"):
        validate_target(fake_target.url, expected_export())


def test_import_is_idempotent_for_same_export(fake_target):
    first = import_long_term_export(fake_target.url, expected_export())
    second = import_long_term_export(fake_target.url, expected_export())
    assert first.status == "imported"
    assert second.status == "already_imported"
    assert second.export_sha256 == first.export_sha256
```

- [ ] **Step 2: Run tests and verify they fail on missing import functions**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_database.py
```

Expected: FAIL because target import functions are undefined.

- [ ] **Step 3: Implement safe target creation and current-schema bootstrap**

Use `psycopg.conninfo.conninfo_to_dict` and `make_conninfo` to derive an administrative URL without logging it. Validate `target_database_name` with `^[A-Za-z][A-Za-z0-9_]{0,62}$`. Connect to database `postgres` with autocommit, compare the source database name, and execute `CREATE DATABASE` using `psycopg.sql.Identifier`. If the target already exists, inspect rather than recreate it.

Apply `KnowledgeRepository.apply_migrations(postgres_migrations_directory())` to the target. Do not add a fourth migration containing source compatibility structures.

- [ ] **Step 4: Add one forward-only current-schema migration receipt table**

Create `004_knowledge_import_receipts.sql` with a generic import receipt that contains no source lifecycle entities. Do not modify the hashes of migrations 001-003 already used by existing current-schema databases:

```sql
CREATE TABLE IF NOT EXISTS knowledge_import_receipts (
    export_sha256 char(64) PRIMARY KEY,
    source_kind text NOT NULL CHECK (source_kind = 'long_term_knowledge_export'),
    manifest jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE knowledge_assertions
    ADD COLUMN IF NOT EXISTS search_terms text[] NOT NULL DEFAULT '{}';

DROP INDEX IF EXISTS knowledge_assertions_search_idx;
ALTER TABLE knowledge_assertions DROP COLUMN IF EXISTS search_document;
ALTER TABLE knowledge_assertions
    ADD COLUMN search_document tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(subject, '') || ' ' || coalesce(predicate, '') || ' ' ||
            array_to_string(search_terms, ' ')
        )
    ) STORED;
CREATE INDEX knowledge_assertions_search_idx
    ON knowledge_assertions USING gin(search_document);
```

Update `_EXPECTED_MIGRATIONS`, packaged migration inventory tests, and current-schema table inventory tests to include migration 004. This table records only content identity and counts; it is not an approval or task-state table.

- [ ] **Step 5: Implement one-transaction import**

Within one target transaction, insert organizations, design groups, families, assertions, lesson reviews, and lessons in dependency order. Insert fresh `knowledge_outbox` rows for families, assertions, and lessons. Finally insert `knowledge_import_receipts`. On a repeated export SHA, verify target content hashes and return `already_imported`; on any differing row with the same stable ID, raise `KNOWLEDGE_IMPORT_CONFLICT` and roll back.

- [ ] **Step 6: Implement structural and content validation**

Validate:

```python
EXPECTED_TARGET_TABLES = frozenset({
    "knowledge_schema_migrations",
    "organizations",
    "design_groups",
    "product_families",
    "knowledge_assertions",
    "design_lesson_reviews",
    "design_lessons",
    "knowledge_review_decisions",
    "knowledge_outbox",
    "knowledge_projection_state",
    "knowledge_import_receipts",
})
```

Compare row counts, stable IDs, canonical JSON hashes, family foreign keys, lesson review hashes, and outbox aggregates with the export. Reject missing or additional target tables except PostgreSQL extension-owned tables outside `public`.

- [ ] **Step 7: Run focused repository and migration tests**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_database.py tests/test_design_lesson_repository.py tests/test_migrations.py
```

Expected: PASS.

- [ ] **Step 8: Commit the isolated target importer**

```bash
git add src/mechanical_design_agent/long_term_knowledge_database.py src/mechanical_design_agent/knowledge_repository.py src/mechanical_design_agent/resources/migrations/postgres/004_knowledge_import_receipts.sql tests/test_long_term_knowledge_database.py tests/test_design_lesson_repository.py tests/test_migrations.py
git commit -m "feat: import knowledge into current schema"
```

### Task 4: Retrieval Parity, Scope Isolation, and Neo4j Rebuild

**Files:**
- Modify: `src/mechanical_design_agent/long_term_knowledge_database.py`
- Modify: `src/mechanical_design_agent/knowledge_repository.py`
- Modify: `src/mechanical_design_agent/knowledge_service.py`
- Modify: `tests/test_long_term_knowledge_database.py`
- Modify: `tests/test_design_lesson_repository.py`
- Create: `tests/test_long_term_knowledge_migration_live.py`

**Interfaces:**
- Consumes: `LongTermKnowledgeExport`, current `KnowledgeRepository.search`, and `Neo4jProjection.rebuild`.
- Produces: `run_retrieval_parity(target_database_url: str, export: LongTermKnowledgeExport) -> dict[str, object]` and `complete_projection_rebuild(settings: KnowledgeSettings) -> dict[str, object]`.

- [ ] **Step 1: Write failing parity and isolation tests**

```python
def test_every_exported_retrieval_probe_hits_expected_identity(imported_repository):
    result = run_retrieval_parity(imported_repository.url, expected_export())
    assert result["status"] == "passed"
    assert result["failed"] == []
    assert result["probe_count"] == len(build_parity_probes(expected_export()))


def test_wrong_scope_cannot_retrieve_imported_knowledge(imported_database_url):
    other = KnowledgeRepository(imported_database_url, KnowledgeScope("other-org", "other-group"))
    result = other.search(query="guide rail")
    assert result["status"] == "completed_no_match"
    assert result["matches"] == []


def test_design_context_exposes_migrated_assertions_as_approved_facts(knowledge_service):
    context = knowledge_service.design_context_build(
        organization_id="org-001",
        design_group_id="group-001",
        requested_family_id="PF-PILOT-001",
        design_features={},
        lesson_query="guide rail",
    )
    assert context["approved_facts"]
    assert context["approved_facts"][0]["kind"] == "knowledge_assertion"
```

- [ ] **Step 2: Run the tests and verify parity support is absent**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_database.py -k 'parity or scope'
```

Expected: FAIL because `run_retrieval_parity` is undefined.

- [ ] **Step 3: Make approved assertions part of current knowledge retrieval**

Extend `KnowledgeRepository.search` to query approved `knowledge_assertions` using their generated `search_document`, the same organization/design-group scope, and the optional family filter. Return them in an `assertions` collection and include them in `matches`. Update `KnowledgeService.design_context_build` so `approved_facts` is populated from `result["assertions"]`.

- [ ] **Step 4: Implement exhaustive parity probes**

Build probes from every family canonical name, alias, legacy exact term, and nonblank legacy search-text phrase, plus every Design Lesson search term. Query with the migrated organization/design-group scope and optional family filter. Require the expected stable ID in the correct result collection. Emit only query text, expected ID, observed IDs, and pass/fail; never emit knowledge bodies or credentials.

- [ ] **Step 5: Add isolated live PostgreSQL acceptance**

The live test creates uniquely named source and target databases on the configured loopback PostgreSQL service. It installs a minimal source fixture containing the seven allowed source tables plus unrelated source-only tables, imports twice, validates exact target table inventory, and runs the complete probe corpus. Cleanup drops only the UUID-named test databases created by the test.

Run:

```bash
MECH_DESIGN_RUN_LIVE_KNOWLEDGE_MIGRATION=1 PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_migration_live.py
```

Expected: PASS when the isolated service is configured; otherwise one precisely explained skip.

- [ ] **Step 6: Rebuild Neo4j only from the target outbox**

After relational parity passes, call:

```python
projection = Neo4jProjection(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
projection.initialize_constraints()
projection_result = projection.rebuild(repository)
```

Validate projected counts for 2 Product Families, 43 assertions, and 4 imported Design Lessons before permitting runtime cutover.

- [ ] **Step 7: Run focused and live tests**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_long_term_knowledge_database.py tests/test_design_lesson_repository.py tests/test_long_term_knowledge_migration_live.py
```

Expected: offline tests PASS; live test PASS when explicitly enabled or reports its configured skip.

- [ ] **Step 8: Commit parity and projection validation**

```bash
git add src/mechanical_design_agent/long_term_knowledge_database.py src/mechanical_design_agent/knowledge_repository.py src/mechanical_design_agent/knowledge_service.py tests/test_long_term_knowledge_database.py tests/test_design_lesson_repository.py tests/test_long_term_knowledge_migration_live.py
git commit -m "test: prove migrated knowledge retrieval parity"
```

### Task 5: CLI, Atomic Cutover, and Operational Documentation

**Files:**
- Modify: `src/mechanical_design_agent/cli.py`
- Modify: `src/mechanical_design_agent/workspace_bootstrap.py`
- Create: `tests/test_knowledge_migration_cli.py`
- Modify: `docs/DATABASE_DEPLOYMENT.md`

**Interfaces:**
- Consumes: `migrate_long_term_knowledge(...)`, selected environment file, workspace, backup directory, and target database name.
- Produces: `mechanical-design knowledge migrate-long-term` JSON output and `replace_env_file_setting(path: Path, key: str, value: str) -> Path`.

- [ ] **Step 1: Write failing parser, redaction, and cutover tests**

```python
def test_migration_cli_requires_explicit_target_and_backup(parser):
    args = parser.parse_args([
        "knowledge", "migrate-long-term", "--workspace", "/workspace",
        "--target-database", "mechanical_design_knowledge",
        "--backup-dir", "/workspace/output/knowledge-migration",
        "--analyze-only",
    ])
    assert args.target_database == "mechanical_design_knowledge"
    assert args.analyze_only is True


def test_execute_requires_the_analysis_report(parser):
    args = parser.parse_args([
        "knowledge", "migrate-long-term", "--workspace", "/workspace",
        "--target-database", "mechanical_design_knowledge",
        "--backup-dir", "/workspace/output/knowledge-migration",
        "--execute-and-cutover", "--analysis-report", "/workspace/output/report.json",
    ])
    assert args.analysis_report == Path("/workspace/output/report.json")


def test_cli_output_never_contains_database_password(run_cli):
    result = run_cli(password="secret-value")
    assert "secret-value" not in result.stdout
    assert "postgresql://" not in result.stdout


def test_cutover_replaces_only_database_url(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("A=1\nMECH_DESIGN_DATABASE_URL=old\nB=2\n", encoding="utf-8")
    backup = replace_env_file_setting(env, "MECH_DESIGN_DATABASE_URL", "new")
    assert env.read_text(encoding="utf-8") == "A=1\nMECH_DESIGN_DATABASE_URL=new\nB=2\n"
    assert backup.read_text(encoding="utf-8") == "A=1\nMECH_DESIGN_DATABASE_URL=old\nB=2\n"
```

- [ ] **Step 2: Run tests and verify the new command is absent**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_knowledge_migration_cli.py
```

Expected: FAIL because `migrate-long-term` is not registered.

- [ ] **Step 3: Add analyze and execute command modes**

Register:

```text
mechanical-design knowledge migrate-long-term \
  --workspace PATH \
  --target-database NAME \
  --backup-dir PATH \
  [--analyze-only | --execute-and-cutover --analysis-report PATH]
```

`--analyze-only` opens only the read-only source transaction and writes a canonical backup plus analysis report. `--execute-and-cutover` requires `--analysis-report`; it verifies that report's export and backup hashes before creating/importing the target, runs structural and retrieval validation, rebuilds Neo4j, writes a passed report, backs up the selected environment file, and then replaces only `MECH_DESIGN_DATABASE_URL`.

- [ ] **Step 4: Implement safe environment-file replacement**

Parse the existing UTF-8 file with `parse_selected_env_file`, preserve comments and unrelated lines, require exactly one existing database URL assignment, write a sibling backup with `atomic_publish_new`, and replace the original with `atomic_replace`. Apply user-only permissions where the platform supports them. Return paths only, never values.

- [ ] **Step 5: Document migration and rollback**

Add commands using clearly marked example values, document the passed-report requirement, and document rollback as restoring the generated environment-file backup. State explicitly that source records remain unchanged and the target contains only current knowledge tables.

- [ ] **Step 6: Run focused tests**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_knowledge_migration_cli.py tests/test_bootstrap_diagnostics.py tests/test_public_documentation.py
```

Expected: PASS.

- [ ] **Step 7: Commit CLI and documentation**

```bash
git add src/mechanical_design_agent/cli.py src/mechanical_design_agent/workspace_bootstrap.py tests/test_knowledge_migration_cli.py docs/DATABASE_DEPLOYMENT.md
git commit -m "feat: add controlled knowledge migration command"
```

### Task 6: Complete Regression and Local Migration Execution

**Files:**
- Runtime artifact: `/Users/yuxiangguo/Documents/Codex/FreeCad Connect/output/knowledge-migration/<attempt-id>/source-export.json`
- Runtime artifact: `/Users/yuxiangguo/Documents/Codex/FreeCad Connect/output/knowledge-migration/<attempt-id>/migration-report.json`
- Runtime configuration: `/Users/yuxiangguo/Documents/Codex/FreeCad Connect/.env.local`
- Design state: `/Users/yuxiangguo/Documents/Codex/FreeCad Connect/designs/four-basketball-carrier-20260830/design.json`
- Selected review: `/Users/yuxiangguo/Documents/Codex/FreeCad Connect/designs/four-basketball-carrier-20260830/lesson-review/review-selected-2.json`

**Interfaces:**
- Consumes: passed software tests, explicit source environment file, and selected lesson review SHA-256 `9d4716cdacbde02af9786cd29be5c97fe87a9b1947228a68fcffc7f7820165ac`.
- Produces: passed migration report, switched local knowledge URL, rebuilt projection, published lesson ID, and unchanged model SHA-256 `8069db8691d7d6c2bafe77ce1f6f1960a5e80c4dd59c97a9060d9d4b5510ba4d`.

- [ ] **Step 1: Run the complete offline test suite**

Run:

```bash
PYTHONPATH=src python -m pytest -q
```

Expected: all supported offline tests pass; only documented environment-dependent skips remain.

- [ ] **Step 2: Run package and public-boundary validation**

Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_packaging.py tests/test_public_release_contract.py tests/test_database_deployment.py
python -m build --no-isolation --outdir /private/tmp/ai-mechanical-design-knowledge-migration-dist
```

Expected: PASS and both wheel and source distribution are created without runtime artifacts or credentials.

- [ ] **Step 3: Analyze and back up the local source knowledge**

Run:

```bash
MECH_DESIGN_ENV_FILE='/Users/yuxiangguo/Documents/Codex/FreeCad Connect/.env.local' \
PYTHONPATH=ai-mechanical-design-agent/src \
python -m mechanical_design_agent.cli knowledge migrate-long-term \
  --workspace '/Users/yuxiangguo/Documents/Codex/FreeCad Connect' \
  --target-database mechanical_design_knowledge \
  --backup-dir '/Users/yuxiangguo/Documents/Codex/FreeCad Connect/output/knowledge-migration' \
  --analyze-only
```

Expected: status `ready_to_migrate`, counts 2 families, 43 assertions, 4 lessons, and no secret values in output.

- [ ] **Step 4: Execute migration, parity checks, projection rebuild, and cutover**

Run the same command with `--execute-and-cutover --analysis-report <exact-report-path-returned-in-step-3>`.

Expected: status `ready`, target structure validation passed, retrieval parity passed with zero failed probes, scope isolation passed, Neo4j rebuild passed, and `.env.local` backup path reported.

- [ ] **Step 5: Retry publication of selected basketball lesson 2**

Call `DesignLessonWorkflow.decide` with the current selected review card, decision text `只同意发布2`, `selected_lesson_numbers=[2]`, and the target `KnowledgeService`.

Expected:

```json
{
  "decision_state": "APPROVE",
  "status": "published",
  "publication_id": "9d4716cdacbde02af9786cd29be5c97fe87a9b1947228a68fcffc7f7820165ac"
}
```

- [ ] **Step 6: Verify the published lesson and unchanged design artifacts**

Verify the target review contains exactly one lesson, its `selection.lesson_numbers` equals `[2]`, lesson 1 is absent, and a search using its spherical-cradle terms returns the new lesson. Recompute model and review hashes and require:

```text
model.FCStd = 8069db8691d7d6c2bafe77ce1f6f1960a5e80c4dd59c97a9060d9d4b5510ba4d
review.json = 85130b2e70888daca402d445b7a366b2531ce10476ec5b314abe0924a1fccc3c
review-selected-2.json = 9d4716cdacbde02af9786cd29be5c97fe87a9b1947228a68fcffc7f7820165ac
```

- [ ] **Step 7: Confirm repository and runtime-artifact boundaries**

```bash
git status --short
git diff --check
```

Expected: the tracked implementation is clean. Do not commit runtime backups, migration reports, environment files, model files, validation artifacts, or database contents. Record the implementation commit IDs in the ignored migration report rather than creating an empty Git commit.
