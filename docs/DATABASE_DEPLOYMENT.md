# Local and evaluation database deployment

This guide describes the supported Docker Compose path for local development,
evaluation, and release testing of AI Mechanical 3DCAD Design Agent version
0.1.0. It is not a production deployment, high-availability design, backup
system, remote-access configuration, or managed-service recommendation.

Compose provisions services. The installed Mechanical Design Agent owns schema migration
and verification. The Compose file does not mount repository
migration directories and does not run migrations from a container entrypoint.

## Prerequisites and certified boundaries

Install the `ai-mechanical-3dcad-design-agent` wheel into CPython 3.12 or newer.
Install Docker Engine with the Docker Compose plugin, or Docker Desktop using
Linux containers. Keep the repository checkout containing `compose.yaml` and
`.env.example`; neither file is a runtime resource inside the wheel.

The checked-in service identities are immutable release inputs:

- PostgreSQL 18 with pgvector:
  `pgvector/pgvector:0.8.5-pg18@sha256:12a379b47ad65289572ea0756efc11b7c241a6662833e8af7038cd3b73d647e0`
- Neo4j Community:
  `neo4j:2026.06.0@sha256:42fd5b9ead4dd4211f6f91bd831c358e4e2117367d04633fbf88682ca4792b30`

Both published ports bind to `127.0.0.1`. Do not change them to a wildcard or
remote interface without a separate security and deployment review. The images
and their licenses remain third-party external services; including their
identities here does not relicense them under the project's Apache-2.0 license.

The macOS release gate has exercised native Linux/arm64 containers. The Windows
D3 release gate has exercised Windows 11 x64, CPython 3.12 x64, Docker Desktop
4.87.0, Docker Engine 29.7.2, Docker Compose 5.4.0, WSL 2.7.12, and native
Linux/amd64 containers on two distinct fixed NTFS volumes. These are
evidence-backed acceptance boundaries, not general production certifications.

## Prepare a protected environment file

`.env.example` is comment-only and deliberately nonfunctional. Copy it to a
private file, uncomment the database settings, replace every placeholder with a
new local secret, and keep that file outside version control. The raw PostgreSQL
password and its URL-encoded representation may differ.

On macOS or Linux:

```bash
umask 077
cp .env.example .mechanical-design.env
chmod 600 .mechanical-design.env
```

On Windows PowerShell, use a local non-reparse NTFS path and restrict the file
to the current account with the host's normal ACL administration process:

```powershell
Copy-Item .env.example .mechanical-design.env
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls .mechanical-design.env /inheritance:r /grant:r "${account}:(R,W)"
```

Do not paste credentials into shell history, logs, reports, issue trackers, or
evidence archives. Never reuse development, staging, or production credentials.

## Validate and start the services

Use the same explicit env file for every Compose operation:

```bash
docker compose --env-file .mechanical-design.env -f compose.yaml config
docker compose --env-file .mechanical-design.env -f compose.yaml pull
docker compose --env-file .mechanical-design.env -f compose.yaml up -d --wait
docker compose --env-file .mechanical-design.env -f compose.yaml ps
```

`config` must resolve without a missing-variable error. `pull` must retain the
exact tag and digest above. `ps` must report both services healthy, with only
the configured PostgreSQL and Neo4j Bolt ports published on loopback. Because
`config` renders interpolated values, treat its console output as sensitive and
do not redirect or attach it to a report.

PowerShell uses the same commands:

```powershell
docker compose --env-file .mechanical-design.env -f compose.yaml config
docker compose --env-file .mechanical-design.env -f compose.yaml pull
docker compose --env-file .mechanical-design.env -f compose.yaml up -d --wait
docker compose --env-file .mechanical-design.env -f compose.yaml ps
```

## Initialize the workspace and migrate the databases

Create an explicit workspace. The examples below use synthetic public
identities only.

On macOS or Linux:

```bash
mechanical-design init --workspace /path/to/mechanical-design-workspace
mechanical-design database bootstrap \
  --workspace /path/to/mechanical-design-workspace \
  --env-file .mechanical-design.env
mechanical-design database bootstrap \
  --workspace /path/to/mechanical-design-workspace \
  --env-file .mechanical-design.env
```

On Windows PowerShell:

```powershell
mechanical-design init --workspace C:\path\to\mechanical-design-workspace
mechanical-design database bootstrap `
  --workspace C:\path\to\mechanical-design-workspace `
  --env-file .mechanical-design.env
mechanical-design database bootstrap `
  --workspace C:\path\to\mechanical-design-workspace `
  --env-file .mechanical-design.env
```

The first bootstrap applies package-owned PostgreSQL migrations `001` through
`009`, verifies `pgcrypto`, `pg_trgm`, and `vector`, and verifies package-owned
Neo4j migrations and constraints. The second bootstrap must report the
PostgreSQL migrations as already applied and keep the Neo4j state valid. A
partial migration, digest mismatch, or missing extension is a blocking failure;
do not edit the migration ledger or copy migration files from the repository.

## Create the first product family and start MCP

An initialized workspace may contain zero product families. Create and select a
synthetic first family before starting operational workflows:

```bash
mechanical-design family create \
  --workspace /path/to/mechanical-design-workspace \
  --organization-id example-org \
  --organization-name "Example Organization" \
  --design-group-id example-design-group \
  --design-group-name "Example Design Group" \
  --family-id example-family \
  --family-name "Example Product Family" \
  --set-default
mechanical-design status --workspace /path/to/mechanical-design-workspace
MECH_DESIGN_WORKSPACE=/path/to/mechanical-design-workspace \
MECH_DESIGN_ENV_FILE=/path/to/.mechanical-design.env mechanical-design-mcp
```

PowerShell sets the same explicit inputs before starting MCP:

```powershell
$env:MECH_DESIGN_WORKSPACE = "C:\path\to\mechanical-design-workspace"
$env:MECH_DESIGN_ENV_FILE = "C:\path\to\.mechanical-design.env"
mechanical-design-mcp
```

## Stop, restart, and remove local data

`stop` keeps both named volumes so the local state is available after the next
`up`:

In other words, `docker compose stop` pauses the services without deleting
their data.

```bash
docker compose --env-file .mechanical-design.env -f compose.yaml stop
docker compose --env-file .mechanical-design.env -f compose.yaml up -d --wait
```

`down` removes the containers and network but retains the named volumes:

Use `docker compose down` when the containers should be recreated while the
named-volume data remains available.

```bash
docker compose --env-file .mechanical-design.env -f compose.yaml down
```

> **Irreversible data-loss warning:** `down -v` deletes both named volumes and
> all PostgreSQL and Neo4j data in this Compose project. Confirm backups and the
> exact project boundary before running it; the deleted local data is not
> recoverable through this project.

```bash
docker compose --env-file .mechanical-design.env -f compose.yaml down -v
```

Never use a broad Docker cleanup command as a substitute for exact Compose
ownership. Release tests use UUID-owned projects and require container,
network, volume, process, port, and temporary-directory cleanup to succeed.

## Troubleshooting

- **Port conflict:** choose unused loopback ports in the protected env file,
  rerun `config`, and restart the project. Do not expose a wildcard address.
- **Authentication failure:** confirm the same env file was used for Compose and
  `mechanical-design database bootstrap`; recreate only a disposable local
  project when credentials have diverged.
- **Missing extension:** confirm the exact pgvector image and digest. Do not
  install extensions manually to conceal an image or migration mismatch.
- **Digest mismatch:** stop. Pull the approved immutable identity or perform a
  new image and migration acceptance before changing the release input.
- **Unhealthy service:** inspect local container health without publishing raw
  credentials. Fix the service before running bootstrap.
- **Partial migration:** preserve the failure evidence, do not rewrite the
  ledger, and restore or recreate only a proven disposable target before a full
  retry.

Raw logs can contain paths and credentials. Keep Raw logs local and return only
schema-limited, privacy-scanned evidence.

## Upgrade and production non-goals

Changing an image tag, digest, architecture, Compose service contract, or any
package-owned migration requires a new clean-machine build, first/second
bootstrap, installed-wheel live integration, cleanup, and public artifact
equivalence acceptance on every claimed platform.

Version 0.1.0 does not define production secrets management, TLS termination,
remote database exposure, backups, replication, high availability, monitoring,
resource sizing, disaster recovery, rolling upgrades, or orchestration beyond
this local and evaluation Compose boundary.
