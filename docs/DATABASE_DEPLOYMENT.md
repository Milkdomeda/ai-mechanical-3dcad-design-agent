# Knowledge database deployment

PostgreSQL with pgvector stores durable Product Family Knowledge, Design Lesson
review decisions, published lessons, searchable assertions, and projection
events. Neo4j stores a rebuildable relationship view. Neither service is
required to create, edit, or validate CAD.

The included Docker Compose configuration is for local development, evaluation,
and release acceptance. It is not a production, backup, or high-availability
deployment.

## Prerequisites

- Python 3.12 or newer with the installed package
- Docker Engine with Docker Compose, or Docker Desktop
- a private environment file based on `.env.example`

The published ports bind to the loopback interface. Keep PostgreSQL and Neo4j
on loopback unless a separate security review approves another deployment.

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

The second bootstrap is an idempotency check. Version 0.7.0 owns three
PostgreSQL migrations:

- `001_knowledge_core.sql`
- `002_knowledge_search.sql`
- `003_knowledge_projection.sql`

The installed package owns schema migration; Compose only provisions services.
If bootstrap reports an incompatible database, select a fresh knowledge
database. This release intentionally does not convert databases created by
earlier process schemas.

## Operation and recovery

- A PostgreSQL transaction publishes the complete immutable lesson review card
  and its outbox event together.
- Neo4j synchronization is idempotent. A failed projection remains pending and
  does not change PostgreSQL knowledge.
- A failed lesson publication can be retried with the same review-card hash.
  The completed CAD model remains completed.
- Back up PostgreSQL before destructive database maintenance. Neo4j can be
  rebuilt from PostgreSQL and the outbox.

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
