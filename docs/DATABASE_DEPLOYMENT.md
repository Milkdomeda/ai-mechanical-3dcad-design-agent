# Knowledge database deployment

PostgreSQL stores durable Product Families, Knowledge Assertions, and Design
Lessons. The baseline uses normalized exact-term matching followed by
PostgreSQL full-text search and has no pgvector requirement. Neo4j is an
optional, rebuild-only relationship view. Neither database is required to
create, edit, or validate CAD.

The included Docker Compose configuration is for local development, evaluation,
and release acceptance. It is not a production, backup, or high-availability
deployment.

## Prerequisites

- Python 3.12 or newer with the installed package
- Docker Engine with Docker Compose, or Docker Desktop, when using the included
  local service bundle
- a private environment file based on `.env.example`

The base package supports PostgreSQL knowledge operations without the Neo4j
driver. Install the optional projection support separately:

```bash
pip install ai-mechanical-3dcad-design-agent[neo4j]
```

The published ports bind to the loopback interface. Keep PostgreSQL and an
optional Neo4j service on loopback unless a separate security review approves
another deployment. The Compose PostgreSQL image includes pgvector for local
compatibility, but the product schema neither creates nor uses that extension.

## Start services

On macOS:

```bash
cp .env.example .mechanical-design.env
chmod 600 .mechanical-design.env
docker compose --env-file .mechanical-design.env -f compose.yaml config
docker compose --env-file .mechanical-design.env -f compose.yaml up -d --wait
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .mechanical-design.env
docker compose --env-file .mechanical-design.env -f compose.yaml config
docker compose --env-file .mechanical-design.env -f compose.yaml up -d --wait
```

Do not attach rendered Compose configuration to reports because it can contain
credentials.

## Initialize the knowledge schema

Create a workspace, export the required database settings from the private
environment file, and run:

```bash
mechanical-design init \
  --workspace /path/to/workspace \
  --actor engineer \
  --organization example-org \
  --design-group example-group
mechanical-design knowledge bootstrap --workspace /path/to/workspace
mechanical-design knowledge bootstrap --workspace /path/to/workspace
```

The second bootstrap is an idempotency check. Version 0.7.0 owns one
PostgreSQL baseline migration:

- `001_knowledge.sql`

That baseline creates `product_families`, `knowledge_assertions`, and
`design_lessons`, plus the technical `knowledge_schema_migrations` ledger.
Neo4j, when installed and configured, owns only
`001_knowledge_projection.cypher`.

The installed package owns schema migration; Compose only provisions services.
If bootstrap reports an incompatible database, select a fresh knowledge
database. This release intentionally does not convert databases created by
earlier process schemas.

## Analyze, execute, and cut over a prior knowledge export

Migration is deliberately split into separate commands. Analyze reads the old
database only through a repeatable-read, read-only transaction and writes a
canonical backup plus a redacted report under ignored `output/`:

```bash
mechanical-design knowledge-migrate --analyze-only \
  --source-env .env.local \
  --output output/knowledge-migration/analysis.json
```

Review the passed analysis before execution. Execution requires that exact
report and creates only the named distinct target database:

```bash
mechanical-design knowledge-migrate --execute \
  --analysis-report output/knowledge-migration/analysis.json \
  --target-name mechanical_design_knowledge
```

The command imports in one transaction, validates canonical content and scope,
and runs every recorded parity probe. It does not edit the environment file.
Cutover is a third, explicit operation added only after all gates pass:

```bash
mechanical-design knowledge-migrate --execute \
  --analysis-report output/knowledge-migration/analysis.json \
  --target-name mechanical_design_knowledge \
  --cutover-env .env.local
```

The old database remains read-only throughout. Generated exports and reports
belong under `output/`; never commit them or private database credentials.

## Operation and recovery

- PostgreSQL publishes Product Family profiles and their generated Assertions
  together, and publishes approved Lesson content directly with its source
  evidence folded into provenance.
- An explicit Neo4j rebuild reads all three PostgreSQL collections and replaces
  only Agent-owned nodes. Failure does not change PostgreSQL knowledge.
- A failed lesson publication can be retried with the same review-card hash.
  The completed CAD model remains completed.
- Back up PostgreSQL before destructive database maintenance. Neo4j can be
  rebuilt solely from the three PostgreSQL business tables.

Inspect readiness with:

```bash
mechanical-design status --workspace /path/to/workspace
```

Stop services without deleting data:

```bash
docker compose --env-file .mechanical-design.env -f compose.yaml stop
```

`docker compose down -v` irreversibly removes the named volumes. Use it only
when intentionally discarding the knowledge database.
