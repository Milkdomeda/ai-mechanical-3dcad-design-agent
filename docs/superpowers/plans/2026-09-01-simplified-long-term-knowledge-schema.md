# Simplified Long-Term Knowledge Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the undeployed governance-oriented knowledge schema with a PostgreSQL source of truth containing only Product Families, Knowledge Assertions, and Design Lessons, while preserving the completed canonical export and all 605 retrieval parity probes.

**Architecture:** One new PostgreSQL baseline migration defines three typed business tables plus the migration ledger. A deterministic target transformer consumes the existing `LongTermKnowledgeExport/v1`, imports it transactionally, and validates canonical hashes without persistent import receipts. Repository search and `design_context_build` read PostgreSQL directly; Neo4j is an optional full-rebuild projection with no outbox dependency.

**Tech Stack:** Python 3.12+, psycopg 3, PostgreSQL 18, PostgreSQL GIN/full-text search, pytest, optional Neo4j 5/6 driver.

**Spec:** `docs/superpowers/specs/2026-09-01-simplified-long-term-knowledge-schema-design.md`

## Global Constraints

- Do not modify the old knowledge database. All source access remains repeatable-read and read-only.
- Do not create the real target database or change `.env.local` until Task 7 reaches its explicit execution approval gate.
- Preserve commits `7973ca4` and `372a705`, the completed canonical export containing 2 Product Families, 43 Knowledge Assertions, and 4 Design Lessons, and all 605 parity probes.
- Treat the uncommitted pre-simplification Task 3 changes as abandoned implementation, but preserve them in a recoverable Git stash before editing their paths.
- The durable business schema contains exactly `product_families`, `knowledge_assertions`, and `design_lessons`; `knowledge_schema_migrations` is the only technical table.
- Do not add organization, design-group, review, decision, publication, event, summary, outbox, projection-state, or import-receipt tables or compatibility views.
- Do not create the `vector` extension or embedding columns. PostgreSQL exact and full-text retrieval must work without pgvector and Neo4j.
- Keep `organization_id` and `design_group_id` as scalar scope fields in each business table.
- Long-term status values are exactly `active`, `superseded`, and `revoked`; source `approved` records map to `active`.
- Use parameterized SQL and portable Python. Do not introduce POSIX-only runtime behavior or hard-coded machine paths.
- Keep generated migration artifacts and reports under ignored `output/`; do not commit customer data, exports, database URLs, or credentials.
- Run focused tests before broader suites. PostgreSQL 18 live acceptance and Neo4j-absent acceptance are release requirements for this change.

## File Structure

- Replace `src/mechanical_design_agent/resources/migrations/postgres/001_knowledge_core.sql`, `002_knowledge_search.sql`, and `003_knowledge_projection.sql` with `001_knowledge.sql`: the complete undeployed baseline.
- Do not retain `src/mechanical_design_agent/resources/migrations/postgres/004_knowledge_import_receipts.sql`.
- Modify `src/mechanical_design_agent/knowledge_repository.py`: migration inventory, direct publication, direct search, status changes, and projection source reads.
- Keep `src/mechanical_design_agent/long_term_knowledge_database.py` focused on read-only source export and backup publication.
- Create `src/mechanical_design_agent/long_term_knowledge_target.py`: canonical target payload, target database creation, transactional import, idempotency, and validation.
- Create `src/mechanical_design_agent/knowledge_matching.py`: query normalization, feature-term extraction, and deterministic applicability checks.
- Modify `src/mechanical_design_agent/knowledge_service.py`: family matching, Assertion/Lesson context construction, and optional projection rebuild entry point.
- Modify `src/mechanical_design_agent/projection.py`: three-type full rebuild only.
- Replace the Neo4j migration inventory with one constraint baseline for ProductFamily, KnowledgeAssertion, and DesignLesson.
- Modify `src/mechanical_design_agent/cli.py`: analyze-only migration, isolated target execution, report production, and separately authorized cutover.
- Modify `src/mechanical_design_agent/server.py`: remove incremental projection sync and retain optional rebuild.
- Modify `pyproject.toml`: move the Neo4j driver to an optional dependency extra.
- Modify `README.md`, `docs/ARCHITECTURE.md`, and `docs/DATABASE_DEPLOYMENT.md`: document the three-table authority and optional dependencies.
- Create `tests/test_simplified_knowledge_schema.py`, `tests/test_long_term_knowledge_target.py`, `tests/test_knowledge_matching.py`, and `tests/test_simplified_knowledge_repository.py`.
- Modify existing migration, packaging, repository, service, projection, CLI, live database, Windows resource, and public documentation tests where their contracts change.

---

### Task 1: Freeze the abandoned Task 3 draft and establish the new baseline schema

**Files:**
- Delete: `src/mechanical_design_agent/resources/migrations/postgres/001_knowledge_core.sql`
- Delete: `src/mechanical_design_agent/resources/migrations/postgres/002_knowledge_search.sql`
- Delete: `src/mechanical_design_agent/resources/migrations/postgres/003_knowledge_projection.sql`
- Delete if present after stashing: `src/mechanical_design_agent/resources/migrations/postgres/004_knowledge_import_receipts.sql`
- Create: `src/mechanical_design_agent/resources/migrations/postgres/001_knowledge.sql`
- Modify: `src/mechanical_design_agent/knowledge_repository.py`
- Create: `tests/test_simplified_knowledge_schema.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/test_packaging.py`
- Modify: `tests/windows_release_helpers.py`
- Modify: `tests/test_database_deployment_live.py`

**Interfaces:**
- Consumes: committed exporter work at `7973ca4` and `372a705`.
- Produces: `_EXPECTED_MIGRATIONS = ("001_knowledge.sql",)` and the four-table PostgreSQL baseline used by every later task.

- [ ] **Step 1: Preserve the abandoned Task 3 working-tree changes without committing them**

Run from the `codex/knowledge-migration` worktree:

```bash
git stash push -u -m "paused-task3-before-schema-simplification" -- docs/superpowers/plans/2026-09-01-long-term-knowledge-migration.md src/mechanical_design_agent/knowledge_repository.py src/mechanical_design_agent/long_term_knowledge_database.py src/mechanical_design_agent/resources/migrations/postgres/001_knowledge_core.sql src/mechanical_design_agent/resources/migrations/postgres/004_knowledge_import_receipts.sql tests/test_database_deployment_live.py tests/test_long_term_knowledge_database.py tests/test_migrations.py tests/test_packaging.py tests/windows_release_helpers.py
git status --short
git stash list -n 1
```

Expected: the listed Task 3 paths are clean, the new simplified spec and plan commits remain present, and the newest stash is named `paused-task3-before-schema-simplification`.

- [ ] **Step 2: Write the failing baseline-inventory tests**

Create `tests/test_simplified_knowledge_schema.py`:

```python
from mechanical_design_agent.migrations import postgres_migrations_directory


EXPECTED_TABLES = {
    "knowledge_schema_migrations",
    "product_families",
    "knowledge_assertions",
    "design_lessons",
}


def test_postgres_has_one_simplified_baseline() -> None:
    with postgres_migrations_directory() as root:
        names = sorted(path.name for path in root.glob("*.sql"))
        sql = (root / "001_knowledge.sql").read_text(encoding="utf-8")
    assert names == ["001_knowledge.sql"]
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table}" in sql
    for forbidden in (
        "organizations", "design_groups", "review", "decision", "outbox",
        "projection_state", "import_receipt", "CREATE EXTENSION", "vector(",
    ):
        assert forbidden not in sql.lower()
```

Update `tests/test_migrations.py`, `tests/test_packaging.py`, `tests/windows_release_helpers.py`, and `tests/test_database_deployment_live.py` to expect only `001_knowledge.sql`.

- [ ] **Step 3: Run the focused tests and verify the old inventory fails**

Run:

```bash
uv run pytest -q tests/test_simplified_knowledge_schema.py tests/test_migrations.py tests/test_packaging.py
```

Expected: FAIL because `001_knowledge.sql` does not exist and the repository still expects the old migration sequence.

- [ ] **Step 4: Create the complete baseline schema**

Create `001_knowledge.sql` with:

```sql
CREATE TABLE knowledge_schema_migrations (
    version integer PRIMARY KEY,
    filename text NOT NULL UNIQUE,
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE product_families (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    design_group_id text NOT NULL CHECK (btrim(design_group_id) <> ''),
    canonical_name text NOT NULL CHECK (btrim(canonical_name) <> ''),
    aliases text[] NOT NULL DEFAULT '{}',
    profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_terms text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL CHECK (btrim(search_text) <> ''),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, design_group_id, id)
);

CREATE TABLE knowledge_assertions (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    design_group_id text NOT NULL CHECK (btrim(design_group_id) <> ''),
    product_family_id text,
    subject text NOT NULL CHECK (btrim(subject) <> ''),
    predicate text NOT NULL CHECK (btrim(predicate) <> ''),
    object_value jsonb NOT NULL,
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    search_terms text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL CHECK (btrim(search_text) <> ''),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    supersedes_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, product_family_id)
        REFERENCES product_families(organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, supersedes_id)
        REFERENCES knowledge_assertions(organization_id, design_group_id, id),
    CHECK (supersedes_id IS NULL OR supersedes_id <> id)
);

CREATE TABLE design_lessons (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    design_group_id text NOT NULL CHECK (btrim(design_group_id) <> ''),
    product_family_id text,
    content jsonb NOT NULL,
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_terms text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL CHECK (btrim(search_text) <> ''),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    supersedes_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, product_family_id)
        REFERENCES product_families(organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, supersedes_id)
        REFERENCES design_lessons(organization_id, design_group_id, id),
    CHECK (supersedes_id IS NULL OR supersedes_id <> id)
);

CREATE INDEX product_families_scope_idx
    ON product_families(organization_id, design_group_id, status);
CREATE INDEX product_families_terms_idx ON product_families USING gin(search_terms);
CREATE INDEX product_families_text_idx
    ON product_families USING gin(to_tsvector('simple', search_text));

CREATE INDEX knowledge_assertions_scope_idx
    ON knowledge_assertions(organization_id, design_group_id, product_family_id, status);
CREATE INDEX knowledge_assertions_terms_idx ON knowledge_assertions USING gin(search_terms);
CREATE INDEX knowledge_assertions_text_idx
    ON knowledge_assertions USING gin(to_tsvector('simple', search_text));

CREATE INDEX design_lessons_scope_idx
    ON design_lessons(organization_id, design_group_id, product_family_id, status);
CREATE INDEX design_lessons_terms_idx ON design_lessons USING gin(search_terms);
CREATE INDEX design_lessons_text_idx
    ON design_lessons USING gin(to_tsvector('simple', search_text));
```

Delete the other PostgreSQL migration resources and set `_EXPECTED_MIGRATIONS` to `("001_knowledge.sql",)`.

- [ ] **Step 5: Run schema and packaging tests**

Run:

```bash
uv run pytest -q tests/test_simplified_knowledge_schema.py tests/test_migrations.py tests/test_packaging.py tests/test_package_resources.py tests/test_windows_packaging.py
```

Expected: PASS, with no reference to `002`, `003`, `004`, pgvector, or projection tables.

- [ ] **Step 6: Run isolated PostgreSQL 18 baseline acceptance**

Run the existing live deployment test against the configured disposable PostgreSQL 18 database:

```bash
uv run pytest -q tests/test_database_deployment_live.py -m live_database
```

Expected: PASS; the public table inventory is exactly the four tables above and every index exists.

- [ ] **Step 7: Commit the baseline**

```bash
git add src/mechanical_design_agent/resources/migrations/postgres src/mechanical_design_agent/knowledge_repository.py tests/test_simplified_knowledge_schema.py tests/test_migrations.py tests/test_packaging.py tests/test_package_resources.py tests/test_windows_packaging.py tests/windows_release_helpers.py tests/test_database_deployment_live.py
git commit -m "refactor: establish minimal knowledge schema"
```

---

### Task 2: Build the deterministic target payload and receipt-free importer

**Files:**
- Create: `src/mechanical_design_agent/long_term_knowledge_target.py`
- Create: `tests/test_long_term_knowledge_target.py`
- Modify: `tests/test_database_deployment_live.py`

**Interfaces:**
- Consumes: `LongTermKnowledgeExport`, `canonical_json`, `KnowledgeRepository.apply_migrations()`.
- Produces:
  - `SimplifiedKnowledgePayload`
  - `build_simplified_payload(export: LongTermKnowledgeExport) -> SimplifiedKnowledgePayload`
  - `derive_target_database_url(source_database_url: str, target_database_name: str) -> str`
  - `create_target_database(source_database_url: str, target_database_name: str, *, connect=psycopg.connect) -> str`
  - `import_simplified_payload(target_database_url: str, payload: SimplifiedKnowledgePayload, *, connect=psycopg.connect) -> MigrationImportResult`
  - `validate_simplified_target(target_database_url: str, payload: SimplifiedKnowledgePayload, *, connect=psycopg.connect) -> dict[str, object]`

- [ ] **Step 1: Write failing transformation tests**

Create tests that assert the exact target boundary:

```python
def test_payload_contains_only_three_business_collections() -> None:
    payload = build_simplified_payload(expected_export())
    assert set(payload.as_dict()) == {
        "schema_version", "source_export_sha256", "product_families",
        "knowledge_assertions", "design_lessons",
    }
    assert len(payload.product_families) == 2
    assert len(payload.knowledge_assertions) == 43
    assert len(payload.design_lessons) == 4
    encoded = canonical_json(payload.as_dict())
    for forbidden in ("design_lesson_reviews", "authorization", "outbox", "receipt"):
        assert forbidden not in encoded


def test_source_approval_becomes_active_and_review_becomes_provenance() -> None:
    payload = build_simplified_payload(expected_export())
    assert {row["status"] for row in payload.knowledge_assertions} == {"active"}
    lesson = payload.design_lessons[0]
    assert lesson["status"] == "active"
    assert len(lesson["provenance"]["source_review_sha256"]) == 64
```

- [ ] **Step 2: Run transformation tests and verify failure**

```bash
uv run pytest -q tests/test_long_term_knowledge_target.py -k payload
```

Expected: FAIL because the target module and payload type do not exist.

- [ ] **Step 3: Implement immutable payload types and deterministic search text**

Implement the central type and helper:

```python
@dataclass(frozen=True)
class SimplifiedKnowledgePayload:
    source_export_sha256: str
    product_families: tuple[dict[str, object], ...]
    knowledge_assertions: tuple[dict[str, object], ...]
    design_lessons: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "SimplifiedKnowledgePayload/v1",
            "source_export_sha256": self.source_export_sha256,
            "product_families": list(self.product_families),
            "knowledge_assertions": list(self.knowledge_assertions),
            "design_lessons": list(self.design_lessons),
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


def _search_text(*values: object) -> str:
    text = " ".join(_flatten_search_values(values))
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("knowledge record has no searchable text")
    return normalized
```

Transform family `approved_profile` into `profile`; include normalized canonical names and aliases in family `search_terms`; drop organization/group lookup rows, review rows, and Assertion authorization; fold Lesson review data and source timestamps into provenance; fold Assertion source timestamps into evidence; sort every collection by stable ID. Database `created_at` values record first target insertion time and are excluded from the semantic payload hash.

- [ ] **Step 4: Write failing importer/idempotency tests**

```python
def test_import_has_no_receipt_or_projection_side_effects(fake_target) -> None:
    result = import_simplified_payload(fake_target.url, expected_payload())
    assert result.status == "imported"
    assert fake_target.public_tables() == {
        "knowledge_schema_migrations", "product_families",
        "knowledge_assertions", "design_lessons",
    }


def test_exact_rerun_is_idempotent_without_receipt(fake_target) -> None:
    first = import_simplified_payload(fake_target.url, expected_payload())
    second = import_simplified_payload(fake_target.url, expected_payload())
    assert first.payload_sha256 == second.payload_sha256
    assert second.status == "already_imported"


def test_conflicting_nonempty_target_fails_closed(fake_target) -> None:
    import_simplified_payload(fake_target.url, expected_payload())
    fake_target.change_family_name("PF-PILOT-001", "conflict")
    with pytest.raises(KnowledgeMigrationError, match="TARGET_CONTENT_MISMATCH"):
        import_simplified_payload(fake_target.url, expected_payload())
```

- [ ] **Step 5: Implement target creation, transaction import, and canonical validation**

Use a safe database-name regex and psycopg `sql.Identifier` for database creation. On import:

1. apply `001_knowledge.sql`;
2. if business tables are empty, insert families, then Assertions and Lessons with `supersedes_id=NULL`, then apply scoped supersession updates in one transaction;
3. if business tables are nonempty, do not insert;
4. select all three collections in stable order, exclude database-generated `created_at`, and compare their canonical semantic payload SHA-256;
5. return `already_imported` only for an exact match.

Define:

```python
@dataclass(frozen=True)
class MigrationImportResult:
    status: Literal["imported", "already_imported"]
    source_export_sha256: str
    payload_sha256: str
    counts: dict[str, int]
```

Do not query or insert an import receipt.

- [ ] **Step 6: Run target unit and live PostgreSQL 18 tests**

```bash
uv run pytest -q tests/test_long_term_knowledge_target.py
uv run pytest -q tests/test_database_deployment_live.py -m live_database -k simplified_import
```

Expected: PASS, including exact rerun and conflicting-target rejection.

- [ ] **Step 7: Commit the target transformer and importer**

```bash
git add src/mechanical_design_agent/long_term_knowledge_target.py tests/test_long_term_knowledge_target.py tests/test_database_deployment_live.py
git commit -m "feat: import canonical knowledge into minimal schema"
```

---

### Task 3: Implement direct Product Family, Assertion, and Lesson retrieval

**Files:**
- Create: `src/mechanical_design_agent/knowledge_matching.py`
- Modify: `src/mechanical_design_agent/knowledge_repository.py`
- Modify: `src/mechanical_design_agent/knowledge_service.py`
- Modify: `src/mechanical_design_agent/server.py`
- Create: `tests/test_knowledge_matching.py`
- Create: `tests/test_simplified_knowledge_repository.py`
- Modify: `tests/test_design_lesson_repository.py`
- Modify: `tests/test_product_family_knowledge.py`
- Modify: `tests/test_design_mcp.py`

**Interfaces:**
- Consumes: the Task 1 schema and Task 2 row shapes.
- Produces:
  - `normalize_search_term(value: str) -> str`
  - `collect_design_terms(query: str, features: Mapping[str, object]) -> tuple[str, ...]`
  - `KnowledgeRepository.match_product_family(*, query: str, design_features: Mapping[str, object], requested_family_id: str | None = None) -> dict[str, object] | None`
  - `KnowledgeRepository.search(*, query: str, product_family_id: str | None = None, limit: int = 20) -> dict[str, object]`
  - search result collections `families`, `assertions`, `lessons`, and combined `matches`.

- [ ] **Step 1: Write failing normalization and family-matching tests**

```python
def test_collect_design_terms_is_stable_and_deduplicated() -> None:
    assert collect_design_terms(
        "Printed carrier", {"design_type": "carrier", "material": "PETG"}
    ) == ("carrier", "petg", "printed carrier")


def test_family_match_prefers_exact_alias_before_full_text(repository) -> None:
    match = repository.match_product_family(
        query="四篮球载具", design_features={}, requested_family_id=None
    )
    assert match["id"] == "PF-PILOT-001"
    assert match["match_kind"] == "exact_term"


def test_explicit_family_must_exist_in_scope(repository) -> None:
    with pytest.raises(ValueError, match="does not exist in this scope"):
        repository.match_product_family(
            query="carrier", design_features={}, requested_family_id="PF-OTHER-SCOPE"
        )
```

- [ ] **Step 2: Run the focused tests and verify failure**

```bash
uv run pytest -q tests/test_knowledge_matching.py tests/test_simplified_knowledge_repository.py -k family
```

Expected: FAIL because direct family matching is absent.

- [ ] **Step 3: Implement normalization and parameterized family matching**

Normalize with Unicode NFKC, whitespace collapse, and `casefold()` in Python. Store the normalized canonical name, aliases, and retrieval terms together in `search_terms` at publication/import time. Query exact terms first with:

```sql
WHERE organization_id=%s AND design_group_id=%s AND status='active'
  AND search_terms && %s::text[]
ORDER BY id
LIMIT 2
```

Reject ambiguous exact matches rather than silently selecting one. If exact matching returns none, use the expression-indexed full-text query and stable ID tie-break.

- [ ] **Step 4: Write failing three-collection retrieval tests**

```python
def test_search_returns_family_assertion_and_lesson(repository) -> None:
    result = repository.search(query="spherical cradle", product_family_id="PF-PILOT-001")
    assert result["status"] == "completed_matches"
    assert result["families"][0]["id"] == "PF-PILOT-001"
    assert result["assertions"][0]["kind"] == "knowledge_assertion"
    assert result["lessons"][0]["kind"] == "design_lesson"
    assert result["matches"] == [
        *result["families"], *result["assertions"], *result["lessons"]
    ]


def test_search_excludes_nonactive_and_other_scope_records(repository) -> None:
    result = repository.search(query="private revoked term")
    assert result["matches"] == []
    assert result["status"] == "completed_no_match"
```

- [ ] **Step 5: Implement direct Assertion and Lesson SQL**

Use one query per typed table. Each query must require the repository scope, `status='active'`, optional family filtering, and either exact-term overlap or indexed full-text match. Return stable IDs as `assertion_id`, `design_lesson_ref`, and `knowledge_id` so `DesignKnowledgeService` can record actual use.

Update Product Family publication to write `profile`, normalized arrays, and `search_text`. Update `publish_design_lesson_review` to insert Lessons directly and fold these values into provenance:

```python
provenance = {
    "source_review_sha256": review_sha256,
    "decision_text": decision_text.strip(),
    "source_review_id": card["review_id"],
    "evidence": lesson.get("evidence", []),
}
```

Idempotent Lesson publication finds existing Lessons by `provenance->>'source_review_sha256'` and verifies their canonical content before returning `resumed=True`.

Product Family onboarding analysis already supplies an `assertions` list. Publish those records into `knowledge_assertions` in the same transaction as the family. Remove `assertions` from the stored family profile, map `object` to `object_value`, default missing applicability/evidence/search terms to empty values, and generate a stable ID from `family_id` plus the first 16 hex characters of the canonical assertion SHA-256. An exact repeated publication verifies the family and generated Assertions before returning `resumed=True`.

Add this repository test:

```python
def test_family_publication_persists_profile_and_generated_assertions(repository) -> None:
    result = repository.publish_product_family(
        family_id="carrier-family", family_name="Printed Ball Carriers",
        aliases=["sports-ball carrier"],
        knowledge={
            "mechanism": "spherical cradle",
            "assertions": [{
                "subject": "handle root", "predicate": "uses",
                "object": "broad radiused transition",
                "search_terms": ["handle root"],
            }],
        },
        decision_text="approved",
    )
    assert result["assertion_ids"]
    found = repository.search(query="handle root", product_family_id="carrier-family")
    assert found["assertions"][0]["subject"] == "handle root"
```

- [ ] **Step 6: Remove repository dependencies on deleted tables**

Delete SQL and methods for:

- `design_lesson_reviews`;
- `knowledge_review_decisions`;
- `knowledge_outbox`;
- `knowledge_projection_state`;
- pending/mark projection events.

Also remove the generic `KnowledgeService.knowledge_review` method and `knowledge_review` MCP tool. Product Family and Design Lesson approval remains in the existing Job Workspace workflows; no generic durable-review API remains.

Keep `projection_records()` as a simple read of the three typed tables for Task 5.

- [ ] **Step 7: Run repository and publication tests**

```bash
uv run pytest -q tests/test_knowledge_matching.py tests/test_simplified_knowledge_repository.py tests/test_design_lesson_repository.py tests/test_product_family_knowledge.py tests/test_design_mcp.py
```

Expected: PASS with no SQL referencing deleted tables.

- [ ] **Step 8: Commit direct retrieval**

```bash
git add src/mechanical_design_agent/knowledge_matching.py src/mechanical_design_agent/knowledge_repository.py src/mechanical_design_agent/knowledge_service.py src/mechanical_design_agent/server.py tests/test_knowledge_matching.py tests/test_simplified_knowledge_repository.py tests/test_design_lesson_repository.py tests/test_product_family_knowledge.py tests/test_design_mcp.py
git commit -m "feat: retrieve typed knowledge directly from postgres"
```

---

### Task 4: Build effective Agent design context with deterministic applicability

**Files:**
- Modify: `src/mechanical_design_agent/knowledge_matching.py`
- Modify: `src/mechanical_design_agent/knowledge_service.py`
- Modify: `tests/test_knowledge_matching.py`
- Modify: `tests/test_design_knowledge.py`
- Modify: `tests/test_design_mcp.py`

**Interfaces:**
- Consumes: `match_product_family()` and typed `search()` results.
- Produces:
  - `applicability_matches(applicability: Mapping[str, object], features: Mapping[str, object]) -> bool`
  - populated `DesignContext/v2` collections for Product Families, Assertions, and Lessons.

- [ ] **Step 1: Write failing applicability-contract tests**

Use the existing exported Assertion shape, where machine-evaluable conditions are under `applicability.conditions`. Descriptive keys such as `summary` do not exclude a record.

```python
def test_applicability_requires_declared_feature_values() -> None:
    applicability = {"conditions": {"design_type": "carrier", "material": ["PETG", "ABS"]}}
    assert applicability_matches(applicability, {"design_type": "carrier", "material": "PETG"})
    assert not applicability_matches(applicability, {"design_type": "shaft", "material": "PETG"})


def test_descriptive_applicability_is_not_treated_as_a_hidden_filter() -> None:
    assert applicability_matches({"summary": "printed carriers"}, {"design_type": "carrier"})
```

Matching rules are exact after JSON scalar normalization: scalar expected values require equality; an expected list accepts any listed value; missing required features fail; unrecognized descriptive structures do not silently reject knowledge.

- [ ] **Step 2: Run applicability tests and verify failure**

```bash
uv run pytest -q tests/test_knowledge_matching.py -k applicability
```

Expected: FAIL because `applicability_matches` does not exist.

- [ ] **Step 3: Implement the applicability helper**

Implement only the declared `conditions` contract:

```python
def applicability_matches(applicability, features):
    conditions = applicability.get("conditions", {})
    if not isinstance(conditions, Mapping):
        raise ValueError("applicability.conditions must be an object")
    for key, expected in conditions.items():
        if key not in features:
            return False
        actual = features[key]
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True
```

Do not infer executable rules from free-form `summary` or non-applicable prose.

- [ ] **Step 4: Write failing `design_context_build` tests**

```python
def test_context_build_uses_features_and_populates_all_knowledge(repository, service) -> None:
    context = service.design_context_build(
        organization_id="org-1",
        design_group_id="group-1",
        requested_family_id=None,
        design_features={"design_type": "carrier", "material": "PETG"},
        lesson_query="spherical cradle",
    )
    assert context["specialized_knowledge"][0]["id"] == "PF-PILOT-001"
    assert context["approved_facts"]
    assert context["approved_design_lessons"]
    assert all(item["status"] == "active" for item in context["approved_facts"])


def test_context_excludes_inapplicable_assertion(repository, service) -> None:
    context = service.design_context_build(
        organization_id="org-1",
        design_group_id="group-1",
        requested_family_id="PF-PILOT-001",
        design_features={"design_type": "shaft"},
        lesson_query="carrier",
    )
    assert "assertion-carrier-only" not in {
        row["assertion_id"] for row in context["approved_facts"]
    }
```

- [ ] **Step 5: Implement context construction**

Validate that method scope arguments match `repository.scope`; call `match_product_family`; call `search` with the selected family; filter Assertions and Lessons through `applicability_matches`; return:

```python
{
    "schema_version": "DesignContext/v2",
    "hard_constraints": [],
    "preferences": [],
    "approved_facts": applicable_assertions,
    "specialized_knowledge": [family] if family else [],
    "approved_design_lessons": applicable_lessons,
    "similar_models": [],
}
```

Do not access Neo4j in this path.

- [ ] **Step 6: Run service and MCP contract tests**

```bash
uv run pytest -q tests/test_knowledge_matching.py tests/test_design_knowledge.py tests/test_design_mcp.py
```

Expected: PASS; `approved_facts` is no longer always empty and design features affect applicability.

- [ ] **Step 7: Commit Agent context construction**

```bash
git add src/mechanical_design_agent/knowledge_matching.py src/mechanical_design_agent/knowledge_service.py tests/test_knowledge_matching.py tests/test_design_knowledge.py tests/test_design_mcp.py
git commit -m "feat: build design context from applicable knowledge"
```

---

### Task 5: Make Neo4j an optional full-rebuild projection

**Files:**
- Delete: `src/mechanical_design_agent/resources/migrations/neo4j/001_constraints.cypher`
- Delete: `src/mechanical_design_agent/resources/migrations/neo4j/002_design_lessons.cypher`
- Delete: `src/mechanical_design_agent/resources/migrations/neo4j/003_projection_state.cypher`
- Create: `src/mechanical_design_agent/resources/migrations/neo4j/001_knowledge_projection.cypher`
- Modify: `src/mechanical_design_agent/projection.py`
- Modify: `src/mechanical_design_agent/knowledge_service.py`
- Modify: `src/mechanical_design_agent/server.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_projection.py`
- Modify: `tests/test_database_deployment.py`
- Modify: `tests/test_packaging.py`
- Modify: `tests/windows_release_helpers.py`

**Interfaces:**
- Consumes: `KnowledgeRepository.projection_records()`.
- Produces: `Neo4jProjection.rebuild(repository: object) -> dict[str, object]` as the only projection mutation path.

- [ ] **Step 1: Write failing optional-projection tests**

```python
def test_regular_context_does_not_open_neo4j(knowledge_service, unavailable_projection) -> None:
    context = knowledge_service.design_context_build(
        organization_id="org-1", design_group_id="group-1",
        requested_family_id="PF-PILOT-001",
        design_features={"design_type": "carrier"}, lesson_query="cradle",
    )
    assert context["approved_facts"]
    assert unavailable_projection.calls == []


def test_projection_rebuild_reads_three_postgres_collections(repository, projection) -> None:
    result = projection.rebuild(repository)
    assert result["counts"] == {
        "product_family": 2, "assertion": 43, "design_lesson": 4
    }
```

- [ ] **Step 2: Run projection tests and verify the outbox contract fails**

```bash
uv run pytest -q tests/test_projection.py tests/test_database_deployment.py -k projection
```

Expected: FAIL because tests and code still expect outbox sync and obsolete constraints.

- [ ] **Step 3: Replace the Neo4j constraint baseline**

Create:

```cypher
CREATE CONSTRAINT product_family_id_unique IF NOT EXISTS
FOR (n:ProductFamily) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT knowledge_assertion_id_unique IF NOT EXISTS
FOR (n:KnowledgeAssertion) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT design_lesson_id_unique IF NOT EXISTS
FOR (n:DesignLesson) REQUIRE n.id IS UNIQUE;
```

The installed inventory must contain only `001_knowledge_projection.cypher`.

- [ ] **Step 4: Remove incremental synchronization**

Delete `Neo4jProjection.sync`, `KnowledgeService.projection_sync`, and the MCP `projection_sync` tool. Keep `projection_rebuild`, which deletes only nodes with `projection_owner='ai-mechanical-design-agent'` and then writes records returned by `projection_records()`.

If the Neo4j driver cannot be imported or connected, explicit status/rebuild calls return an unavailable result or raise a bounded projection error; PostgreSQL services remain constructible and usable.

- [ ] **Step 5: Make the Python Neo4j driver optional**

Move the dependency from mandatory project dependencies to:

```toml
[project.optional-dependencies]
neo4j = ["neo4j>=5.28.0,<7"]
```

Update package tests to install and test the base package without the extra, and separately verify the `neo4j` extra metadata.

- [ ] **Step 6: Run projection, packaging, and Neo4j-absent tests**

```bash
uv run pytest -q tests/test_projection.py tests/test_database_deployment.py tests/test_packaging.py tests/test_public_distribution.py
```

Also run the base-package import test in an isolated environment without the Neo4j extra. Expected: PostgreSQL repository, search, and context imports pass; only explicit projection use reports unavailable.

- [ ] **Step 7: Commit optional projection support**

```bash
git add src/mechanical_design_agent/resources/migrations/neo4j src/mechanical_design_agent/projection.py src/mechanical_design_agent/knowledge_service.py src/mechanical_design_agent/server.py pyproject.toml uv.lock tests/test_projection.py tests/test_database_deployment.py tests/test_packaging.py tests/test_public_distribution.py tests/windows_release_helpers.py
git commit -m "refactor: make neo4j a rebuild-only optional projection"
```

---

### Task 6: Replace the migration CLI and documentation without creating a real target

**Files:**
- Modify: `src/mechanical_design_agent/cli.py`
- Create: `tests/test_simplified_knowledge_migration_cli.py`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DATABASE_DEPLOYMENT.md`
- Modify: `tests/test_public_documentation.py`
- Modify: `tests/test_public_release_contract.py`

**Interfaces:**
- Consumes: read-only `read_source_export`, source backup publication, `build_simplified_payload`, target importer/validator, and existing parity probes.
- Produces:
  - analyze-only report `SimplifiedKnowledgeMigrationAnalysis/v1`;
  - execution report `SimplifiedKnowledgeMigrationReport/v1`;
  - no environment mutation unless the explicit cutover flag and passed report are both supplied.

- [ ] **Step 1: Write failing CLI safety tests**

```python
def test_analyze_only_never_connects_to_target_or_edits_environment(cli_runner, tmp_path) -> None:
    result = cli_runner.invoke([
        "knowledge-migrate", "--analyze-only",
        "--source-env", str(tmp_path / ".env.local"),
        "--output", str(tmp_path / "analysis.json"),
    ])
    assert result.exit_code == 0
    assert cli_runner.target_connections == []
    assert cli_runner.environment_writes == []


def test_execute_requires_passed_analysis_and_separate_cutover_flag(cli_runner) -> None:
    result = cli_runner.invoke(["knowledge-migrate", "--execute"])
    assert result.exit_code != 0
    assert "--analysis-report" in result.output
    assert cli_runner.environment_writes == []
```

- [ ] **Step 2: Run CLI tests and verify failure**

```bash
uv run pytest -q tests/test_simplified_knowledge_migration_cli.py
```

Expected: FAIL because the simplified command contract is absent.

- [ ] **Step 3: Implement analyze-only and isolated execute modes**

Analyze-only must:

- open only the source read-only transaction;
- reuse the canonical export and 605 probes;
- write the source export backup, source SHA-256, target-payload SHA-256, counts, and probe inventory;
- redact database credentials and URLs.

Execute must require a matching passed analysis report, create/import only the named distinct target, validate structure/content/parity/scope, and write a report. It must not edit an environment file unless `--cutover-env PATH` is explicitly present and every gate passed.

- [ ] **Step 4: Write atomic cutover tests**

```python
def test_failed_parity_never_changes_environment(cli_runner, passed_analysis, env_file) -> None:
    cli_runner.parity_result = {"status": "failed", "failed": 1}
    result = cli_runner.invoke([
        "knowledge-migrate", "--execute", "--analysis-report", str(passed_analysis),
        "--target-name", "mechanical_design_knowledge", "--cutover-env", str(env_file),
    ])
    assert result.exit_code != 0
    assert env_file.read_text(encoding="utf-8") == "MECH_DESIGN_DATABASE_URL=old\n"


def test_passed_execute_without_cutover_leaves_environment_unchanged(
    cli_runner, passed_analysis, env_file
) -> None:
    result = cli_runner.invoke([
        "knowledge-migrate", "--execute", "--analysis-report", str(passed_analysis),
        "--target-name", "mechanical_design_knowledge",
    ])
    assert result.exit_code == 0
    assert env_file.read_text(encoding="utf-8") == "MECH_DESIGN_DATABASE_URL=old\n"
```

- [ ] **Step 5: Update public documentation**

Document:

- PostgreSQL's three durable business tables;
- no pgvector requirement;
- Neo4j optional `neo4j` extra and rebuild-only behavior;
- exact/full-text retrieval order;
- analyze, execute, and cutover as separate operations;
- old database read-only guarantee;
- generated export/report locations under ignored `output/`.

Remove statements that pgvector or Neo4j is required for durable knowledge operations.

- [ ] **Step 6: Run CLI and documentation tests**

```bash
uv run pytest -q tests/test_simplified_knowledge_migration_cli.py tests/test_public_documentation.py tests/test_public_release_contract.py
```

Expected: PASS; no test creates a real target or modifies the developer `.env.local`.

- [ ] **Step 7: Commit CLI and documentation**

```bash
git add src/mechanical_design_agent/cli.py tests/test_simplified_knowledge_migration_cli.py README.md docs/ARCHITECTURE.md docs/DATABASE_DEPLOYMENT.md tests/test_public_documentation.py tests/test_public_release_contract.py
git commit -m "feat: add gated simplified knowledge migration"
```

---

### Task 7: Verify the implementation, then stop at the real-migration approval gate

**Files:**
- Modify if required by verified behavior: `tests/test_long_term_knowledge_migration.py`
- Modify if required by verified behavior: `tests/test_database_deployment_live.py`
- Runtime output only after later approval: `output/knowledge-migration/<attempt-id>/`

**Interfaces:**
- Consumes: Tasks 1–6 and the unchanged canonical export/parity corpus.
- Produces: verified software commits and a user-facing readiness report. Real target creation and `.env.local` cutover remain separate, explicitly approved operations.

- [ ] **Step 1: Run all focused knowledge tests**

```bash
uv run pytest -q tests/test_long_term_knowledge_migration.py tests/test_long_term_knowledge_database.py tests/test_long_term_knowledge_target.py tests/test_simplified_knowledge_schema.py tests/test_simplified_knowledge_repository.py tests/test_knowledge_matching.py tests/test_design_lesson_repository.py tests/test_product_family_knowledge.py tests/test_design_knowledge.py tests/test_projection.py tests/test_simplified_knowledge_migration_cli.py
```

Expected: PASS.

- [ ] **Step 2: Run the complete offline suite**

```bash
uv run pytest -q -m "not live_database and not live_freecad and not live_neo4j"
```

Expected: PASS with only precisely documented environmental skips.

- [ ] **Step 3: Build and inspect distributions**

```bash
uv build
uv run pytest -q tests/test_public_distribution.py tests/test_packaging.py tests/test_windows_packaging.py tests/test_package_resources.py
```

Expected: wheel and source distribution contain `001_knowledge.sql`, the single Neo4j constraint baseline, and no old migration, generated export, report, credential, or machine-local path.

- [ ] **Step 4: Run isolated PostgreSQL 18 acceptance**

Use only disposable test databases created by the live test harness:

```bash
uv run pytest -q tests/test_database_deployment_live.py -m live_database
```

Expected: schema creation, first import, exact rerun, conflict rejection, scope isolation, and direct search all pass on PostgreSQL 18.

- [ ] **Step 5: Run all 605 parity probes against the disposable target**

Run the parity harness with the completed canonical export. Require:

```text
probe_count=605
passed=605
failed=0
product_families=2
knowledge_assertions=43
design_lessons=4
```

Also require negative probes from unrelated organization and design-group scopes to return no matches.

- [ ] **Step 6: Verify Neo4j-absent and optional rebuild behavior**

First run Product Family matching, repository search, Design Lesson search, and `design_context_build` with no Neo4j driver/configuration. Expected: all pass.

If a disposable Neo4j test service is configured, run the explicit rebuild and require exactly 2 ProductFamily, 43 KnowledgeAssertion, and 4 DesignLesson nodes owned by the Agent. Neo4j unavailability must not change the PostgreSQL results.

- [ ] **Step 7: Verify the source and prior artifacts remain unchanged**

Compare the pre-recorded SHA-256 values for:

- canonical source export;
- basketball model;
- original Lesson review card;
- selected Lesson 2 review card.

Open the old database only with the exporter read-only transaction and confirm no source write occurred. Confirm `.env.local` still contains the old database URL.

- [ ] **Step 8: Commit only test corrections proven necessary by the final runs**

If verification required test-only corrections, stage their exact files and commit:

```bash
git commit -m "test: verify simplified knowledge migration"
```

If no correction was necessary, do not create an empty commit.

- [ ] **Step 9: Stop and request explicit real-migration execution approval**

Report the commit range, offline test result, PostgreSQL 18 result, 605/605 parity result, source-artifact hashes, and confirmation that `.env.local` is unchanged.

Do not create the real target database, rebuild the user's Neo4j projection, publish the selected basketball Lesson, or change `.env.local` until the user separately approves real migration execution.
