from __future__ import annotations

from pathlib import Path
import hashlib
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from mechanical_design_agent import cli
from mechanical_design_agent.config import database_url_from_environment
from mechanical_design_agent.migrations import (
    discover_postgres_migrations,
    neo4j_migrations_directory,
    postgres_migrations_directory,
)
from mechanical_design_agent.repository import PostgresRepository
from mechanical_design_agent.service import MechanicalDesignService


def _migration_bytes(name: str) -> bytes:
    with postgres_migrations_directory() as migrations:
        return (migrations / name).read_bytes()


def _migration_text(name: str) -> str:
    return _migration_bytes(name).decode("utf-8")


def _neo4j_migration_bytes(name: str) -> bytes:
    with neo4j_migrations_directory() as migrations:
        return (migrations / name).read_bytes()


def test_design_lesson_review_migration_has_audited_lifecycle():
    sql = _migration_text("005_design_lesson_reviews.sql")
    assert "CREATE TABLE IF NOT EXISTS design_lesson_reviews" in sql
    assert "CHECK (id ~ '^DLR-[A-Za-z0-9-]+$')" in sql
    assert "awaiting-engineer-review" in sql
    assert "superseded" in sql
    assert "rejected" in sql
    assert "invalid" in sql
    assert "approved-retrieval-pending" in sql
    assert "stored-and-retrievable" in sql
    assert "package_sha256 char(64) NOT NULL UNIQUE" in sql
    assert "supersedes_review_id text REFERENCES design_lesson_reviews(id)" in sql
    assert "published_design_lesson_id uuid REFERENCES design_lesson_events(id)" in sql


def test_delivery_approval_binding_migration_persists_the_exact_final_sha256():
    sql = _migration_text("006_delivery_approval_binding.sql")
    assert "approved_final_sha256 char(64)" in sql
    assert "approved_for_delivery" in sql


def test_review_snapshot_binding_migration_persists_immutable_model_paths():
    migration = _migration_bytes("007_review_immutable_snapshots.sql")
    sql = migration.decode("utf-8")
    assert "approved_final_artifact_path text" in sql
    assert "design_working_copies" in sql
    assert "design_lesson_reviews" in sql
    assert "ADD CONSTRAINT design_working_copies_delivery_artifact_check" in sql
    assert "ADD CONSTRAINT design_lesson_reviews_approved_artifact_check" in sql
    assert hashlib.sha256(migration).hexdigest() == (
        "30be8f73a06285fd0299375cdfddfa68916c967e2848ad68ace6fd2986410834"
    )


def test_snapshot_compatibility_migration_drops_round2_constraints():
    sql = _migration_text("008_drop_legacy_snapshot_constraints.sql")
    assert "DROP CONSTRAINT IF EXISTS design_working_copies_delivery_artifact_check" in sql
    assert "DROP CONSTRAINT IF EXISTS design_lesson_reviews_approved_artifact_check" in sql


def test_design_jobs_migration_has_authoritative_lifecycle_and_event_history():
    sql = _migration_text("010_design_jobs.sql")
    normalized = " ".join(sql.split())

    assert "CREATE TABLE IF NOT EXISTS design_jobs" in sql
    assert "id uuid PRIMARY KEY" in sql
    assert "workspace_id uuid NOT NULL" in sql
    assert "UNIQUE(workspace_id,idempotency_token)" in sql
    assert "UNIQUE(workspace_id,display_id)" in sql
    assert "revision integer NOT NULL DEFAULT 0" in sql
    assert "job_type IN ('mechanical_design','product_family_onboarding')" in sql
    assert "status IN ('active','blocked','completed','cancelled','archived')" in sql
    assert "phase IN ( 'requirements','design','validation','delivery','lesson_capture','completed' )" in normalized
    assert "phase IN ( 'intake','analysis','knowledge_review','database_publication','completed' )" in normalized
    assert "provisioning_state text NOT NULL DEFAULT 'provisioning'" in sql
    assert "provisioning_state IN ('provisioning','ready')" in sql
    assert "directory_name IS NOT NULL OR provisioning_state = 'provisioning'" in sql
    assert "UNIQUE(id,organization_id)" in sql
    assert "UNIQUE(id,organization_id,design_group_id)" in sql
    assert "FOREIGN KEY (design_group_id,organization_id)" in sql
    assert "FOREIGN KEY (family_id,organization_id,design_group_id)" in sql
    assert "FOREIGN KEY (created_by,organization_id)" in sql
    assert "CREATE INDEX IF NOT EXISTS design_jobs_scope_idx" in sql
    assert "CREATE TABLE IF NOT EXISTS design_job_events" in sql
    assert "job_id uuid NOT NULL REFERENCES design_jobs(id)" in sql
    assert "UNIQUE(job_id,revision)" in sql
    assert "CREATE INDEX IF NOT EXISTS design_job_events_job_idx" in sql
    assert "CREATE TRIGGER design_job_events_append_only" in sql
    assert "BEFORE UPDATE OR DELETE ON design_job_events" in sql


def test_design_jobs_migration_keeps_the_task2_immutable_digest():
    migration = _migration_bytes("010_design_jobs.sql")

    assert hashlib.sha256(migration).hexdigest() == (
        "9e6782549cb7292ec2367541bb51e393db1dffda1193f53fadb71e4bcdf1e154"
    )
    assert b"display_id ~" not in migration


def test_design_job_working_copy_migration_is_additive_scoped_and_authoritative():
    sql = _migration_text("011_design_job_working_copies.sql")
    normalized = " ".join(sql.split())

    assert "ALTER TABLE design_working_copies ADD COLUMN IF NOT EXISTS job_id uuid" in normalized
    assert "ALTER TABLE design_jobs ADD COLUMN IF NOT EXISTS active_working_copy_id uuid" in normalized
    assert "CREATE TABLE IF NOT EXISTS design_job_source_snapshots" in sql
    assert "job_id uuid NOT NULL" in sql
    assert "source_filename text NOT NULL" in sql
    assert "stored_path text NOT NULL" in sql
    assert "source_model_revision_id uuid" in sql
    assert "sha256 char(64) NOT NULL" in sql
    assert "FOREIGN KEY (job_id,organization_id,design_group_id)" in normalized
    assert "CONSTRAINT design_working_copies_model_scope_fk" in normalized
    assert "CONSTRAINT design_working_copies_family_scope_fk" in normalized
    assert "CONSTRAINT design_working_copies_creator_scope_fk" in normalized
    assert "FOREIGN KEY (active_working_copy_id,id,organization_id,design_group_id)" in normalized
    assert "CREATE INDEX IF NOT EXISTS design_working_copies_job_idx" in sql
    assert "CREATE INDEX IF NOT EXISTS design_job_source_snapshots_job_idx" in sql
    assert "reject_legacy_null_job_working_copy_insert" in sql
    assert "design_job_source_snapshots_append_only" in sql
    assert "BEFORE UPDATE OR DELETE ON design_job_source_snapshots" in normalized


def test_design_job_working_copy_migration_has_the_task6_immutable_digest():
    migration = _migration_bytes("011_design_job_working_copies.sql")

    assert hashlib.sha256(migration).hexdigest() == (
        "3017f69d5f55efb325ff2b1baca281a072a69f8054708fbb499cd3cc5c19cb52"
    )


def test_database_with_task2_010_digest_skips_the_unchanged_migration():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        name = "010_design_jobs.sql"
        (root / name).write_bytes(_migration_bytes(name))
        connection = _Connection()
        connection.migrations[10] = {
            "filename": name,
            "sha256": "9e6782549cb7292ec2367541bb51e393db1dffda1193f53fadb71e4bcdf1e154",
        }
        repository = PostgresRepository("postgresql://unused")
        repository.connection = lambda: connection  # type: ignore[method-assign]

        result = repository.apply_migrations(root)

    assert result == {"applied": [], "skipped": [name]}
    assert connection.executed_sql == []


def test_database_with_round2_007_digest_skips_it_and_applies_008():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name in (
            "007_review_immutable_snapshots.sql",
            "008_drop_legacy_snapshot_constraints.sql",
        ):
            (root / name).write_bytes(_migration_bytes(name))
        connection = _Connection()
        connection.migrations[7] = {
            "filename": "007_review_immutable_snapshots.sql",
            "sha256": "30be8f73a06285fd0299375cdfddfa68916c967e2848ad68ace6fd2986410834",
        }
        repository = PostgresRepository("postgresql://unused")
        repository.connection = lambda: connection  # type: ignore[method-assign]

        result = repository.apply_migrations(root)

    assert result == {
        "applied": ["008_drop_legacy_snapshot_constraints.sql"],
        "skipped": ["007_review_immutable_snapshots.sql"],
    }
    assert connection.executed_sql == [
        _migration_text("008_drop_legacy_snapshot_constraints.sql")
    ]


class _Result:
    def __init__(self, row: dict[str, str] | None = None):
        self.row = row

    def fetchone(self) -> dict[str, str] | None:
        return self.row


class _Transaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _TrackedTransaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    def __enter__(self) -> None:
        self.connection.events.append("transaction-begin")
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.connection.events.append(
            "transaction-rollback" if exc_type is not None else "transaction-commit"
        )
        return None


class _Connection:
    def __init__(self) -> None:
        self.migrations: dict[int, dict[str, str]] = {}
        self.executed_sql: list[str] = []
        self.events: list[str] = []

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def transaction(self) -> _Transaction:
        return _Transaction()

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Result:
        if query.startswith("SELECT pg_advisory_lock"):
            self.events.append("lock")
            return _Result()
        if query.startswith("SELECT pg_advisory_unlock"):
            self.events.append("unlock")
            return _Result()
        if query.startswith("SELECT filename,sha256"):
            return _Result(self.migrations.get(int(parameters[0])))
        if query.startswith("INSERT INTO schema_migrations"):
            self.migrations[int(parameters[0])] = {"filename": str(parameters[1]), "sha256": str(parameters[2])}
        elif not query.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            self.executed_sql.append(query)
            self.events.append(f"sql:{query}")
        return _Result()


class _BootstrapRepository:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply_migrations(self, root: Path) -> dict[str, list[str]]:
        self.calls.append(f"migrations:{root}")
        return {"applied": [], "skipped": []}

    def initialize_bootstrap(self, config: dict[str, str], actor_id: str) -> None:
        self.calls.append(f"bootstrap:{config['family_id']}:{actor_id}")

    def status(self) -> dict[str, str]:
        return {"status": "healthy"}


class _CliRepository:
    def __init__(self) -> None:
        self.root: Path | None = None

    def apply_migrations(self, root: Path) -> dict[str, list[str]]:
        self.root = root
        return {"applied": ["002_design_lessons.sql"], "skipped": ["001_init.sql"]}


class MigrationTests(unittest.TestCase):
    def test_migrate_database_url_loads_only_the_explicit_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "migration.env"
            env_file.write_text(
                "MECH_DESIGN_DATABASE_URL=postgresql://packaged-migration\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"MECH_DESIGN_ENV_FILE": str(env_file)},
                clear=True,
            ):
                self.assertEqual(
                    database_url_from_environment(),
                    "postgresql://packaged-migration",
                )

    def test_packaged_postgres_migrations_contain_the_exact_ordered_baseline(self) -> None:
        with postgres_migrations_directory() as root:
            self.assertEqual(
                [path.name for path in discover_postgres_migrations(root)],
                [
                    "001_init.sql",
                    "002_design_lessons.sql",
                    "003_design_lesson_hardening.sql",
                    "004_validation_report_digest.sql",
                    "005_design_lesson_reviews.sql",
                    "006_delivery_approval_binding.sql",
                    "007_review_immutable_snapshots.sql",
                    "008_drop_legacy_snapshot_constraints.sql",
                    "009_design_lifecycle_closure.sql",
                    "010_design_jobs.sql",
                    "011_design_job_working_copies.sql",
                ],
            )

    def test_packaged_neo4j_migrations_contain_the_exact_ordered_baseline(self) -> None:
        expected = {
            "001_constraints.cypher": "b15cfb1df2d87fa04e0efd1aea3220ab42b232f44976377ad33e66264f9a4670",
            "002_design_lessons.cypher": "54a74a7bbabca088d0831dd05355d2c2328b79333249670a6597719499e74d3a",
            "003_projection_state.cypher": "caccce10612363aeb580f46531b94e128482321e159db6835685f99c0581122e",
        }
        with neo4j_migrations_directory() as root:
            self.assertEqual(
                [path.name for path in sorted(root.glob("*.cypher"))],
                list(expected),
            )
        self.assertEqual(
            {
                name: hashlib.sha256(_neo4j_migration_bytes(name)).hexdigest()
                for name in expected
            },
            expected,
        )

    def test_discovers_numbered_sql_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
            (root / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "notes.txt").write_text("ignored", encoding="utf-8")
            self.assertEqual(
                [path.name for path in discover_postgres_migrations(root)],
                ["001_first.sql", "002_second.sql"],
            )

    def test_rejects_duplicate_numeric_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "002_a.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "002_b.sql").write_text("SELECT 2;", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate migration version"):
                discover_postgres_migrations(root)

    def test_applies_each_migration_once_then_skips_matching_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            connection = _Connection()
            repository = PostgresRepository("postgresql://unused")
            repository.connection = lambda: connection  # type: ignore[method-assign]

            self.assertEqual(repository.apply_migrations(root), {"applied": ["001_first.sql"], "skipped": []})
            self.assertEqual(connection.executed_sql, ["SELECT 1;"])
            self.assertEqual(repository.apply_migrations(root), {"applied": [], "skipped": ["001_first.sql"]})
            self.assertEqual(connection.executed_sql, ["SELECT 1;"])

    def test_global_advisory_lock_covers_discovery_execution_and_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            migration = root / "001_first.sql"
            migration.write_text("SELECT 1;", encoding="utf-8")
            connection = _Connection()
            repository = PostgresRepository("postgresql://unused")
            repository.connection = lambda: connection  # type: ignore[method-assign]

            def discover(locked_root: Path) -> list[Path]:
                connection.events.append("discover")
                self.assertEqual(locked_root, root)
                return [migration]

            with patch(
                "mechanical_design_agent.repository.discover_postgres_migrations",
                side_effect=discover,
            ):
                repository.apply_migrations(root)

            self.assertEqual(
                connection.events,
                ["lock", "discover", "sql:SELECT 1;", "unlock"],
            )

    def test_session_lock_does_not_collapse_per_migration_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
            connection = _Connection()
            connection.transaction = lambda: _TrackedTransaction(connection)  # type: ignore[method-assign]
            repository = PostgresRepository("postgresql://unused")
            repository.connection = lambda: connection  # type: ignore[method-assign]

            repository.apply_migrations(root)

            self.assertEqual(
                connection.events,
                [
                    "transaction-begin",
                    "lock",
                    "transaction-commit",
                    "transaction-begin",
                    "sql:SELECT 1;",
                    "transaction-commit",
                    "transaction-begin",
                    "sql:SELECT 2;",
                    "transaction-commit",
                    "transaction-begin",
                    "unlock",
                    "transaction-commit",
                ],
            )

    def test_hashes_and_executes_one_exact_migration_byte_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            migration = root / "001_first.sql"
            migration.write_bytes(b"SELECT 1;\n")
            connection = _Connection()
            repository = PostgresRepository("postgresql://unused")
            repository.connection = lambda: connection  # type: ignore[method-assign]
            original_read_bytes = Path.read_bytes
            migration_reads = 0

            def read_once(path: Path) -> bytes:
                nonlocal migration_reads
                data = original_read_bytes(path)
                if path.name == migration.name:
                    migration_reads += 1
                    if migration_reads == 1:
                        migration.write_bytes(b"SELECT 2;\n")
                return data

            with patch.object(Path, "read_bytes", autospec=True, side_effect=read_once):
                repository.apply_migrations(root)

            self.assertEqual(migration_reads, 1)
            self.assertEqual(connection.executed_sql, ["SELECT 1;\n"])
            self.assertEqual(
                connection.migrations[1]["sha256"],
                hashlib.sha256(b"SELECT 1;\n").hexdigest(),
            )

    def test_rejects_changed_digest_for_an_applied_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            migration = root / "001_first.sql"
            migration.write_text("SELECT 1;", encoding="utf-8")
            connection = _Connection()
            repository = PostgresRepository("postgresql://unused")
            repository.connection = lambda: connection  # type: ignore[method-assign]
            repository.apply_migrations(root)
            migration.write_text("SELECT 2;", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "migration digest mismatch: 001_first.sql"):
                repository.apply_migrations(root)

    def test_database_initialization_migrates_before_bootstrap(self) -> None:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        repository = _BootstrapRepository()
        service.repository = repository
        service.settings = SimpleNamespace(actor_id="owner")
        service.bootstrap_config = {"family_id": "family"}

        migration_root = Path("/package/resources/migrations/postgres")
        with patch(
            "mechanical_design_agent.service.postgres_migrations_directory"
        ) as migrations:
            migrations.return_value.__enter__.return_value = migration_root
            service._initialize_database()

        self.assertEqual(
            repository.calls,
            [
                f"migrations:{migration_root}",
                "bootstrap:family:owner",
            ],
        )

    def test_database_requirement_reinitializes_migrations_and_bootstrap(self) -> None:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        repository = _BootstrapRepository()
        service.repository = repository
        service.settings = SimpleNamespace(actor_id="owner")
        service.bootstrap_config = {"family_id": "family"}
        service.bootstrap_error = "initial database connection failed"

        migration_root = Path("/package/resources/migrations/postgres")
        with patch(
            "mechanical_design_agent.service.postgres_migrations_directory"
        ) as migrations:
            migrations.return_value.__enter__.return_value = migration_root
            service._require_database()

        self.assertEqual(service.bootstrap_error, "")
        self.assertEqual(
            repository.calls,
            [
                f"migrations:{migration_root}",
                "bootstrap:family:owner",
            ],
        )

    def test_migrate_cli_prints_migration_result_as_json(self) -> None:
        repository = _CliRepository()
        output = StringIO()
        guard_calls: list[tuple[str, object]] = []
        runtime = SimpleNamespace(
            require_initialized=lambda capability: guard_calls.append(
                ("initialized", capability)
            ),
            require_capability=lambda capability, probe: guard_calls.append(
                ("capability", (capability, probe))
            ),
            secret_value=lambda name: (
                guard_calls.append(("secret", name)) or "postgresql://unused"
            ),
        )

        with (
            patch(
                "mechanical_design_agent.cli.Settings.from_environment",
                side_effect=AssertionError("migrate must not load full runtime settings"),
            ),
            patch(
                "mechanical_design_agent.cli.BootstrapRuntime.from_process",
                return_value=runtime,
            ),
            patch(
                "mechanical_design_agent.cli.postgres_migrations_directory"
            ) as migrations,
            patch("mechanical_design_agent.cli.PostgresRepository", return_value=repository),
            patch("sys.argv", ["mechanical-design", "migrate"]),
            redirect_stdout(output),
        ):
            migrations.return_value.__enter__.return_value = Path(
                "/package/resources/migrations/postgres"
            )
            cli.main()

        self.assertEqual(
            repository.root, Path("/package/resources/migrations/postgres")
        )
        self.assertEqual(
            guard_calls,
            [
                ("initialized", "postgres_migration"),
                ("capability", ("postgres_migration", True)),
                ("secret", "postgresql"),
            ],
        )
        self.assertEqual(
            output.getvalue(),
            '{\n  "applied": [\n    "002_design_lessons.sql"\n  ],\n  "skipped": [\n    "001_init.sql"\n  ]\n}\n',
        )


class LiveMigrationConcurrencyTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("MECH_DESIGN_DATABASE_URL"),
        "MECH_DESIGN_DATABASE_URL is not configured; live migration race skipped",
    )
    def test_two_runners_apply_the_ordered_migrations_without_duplicate_ledger_rows(self) -> None:
        database_url = os.environ["MECH_DESIGN_DATABASE_URL"]
        repositories = [PostgresRepository(database_url), PostgresRepository(database_url)]
        with repositories[0].connection() as connection:
            used_versions = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations WHERE version BETWEEN 900 AND 999"
                ).fetchall()
            }
        version = next(value for value in range(999, 899, -1) if value not in used_versions)
        token = f"{version}_{os.getpid()}"
        table_name = f"migration_race_probe_{token}"
        filename = f"{version:03d}_concurrency_probe.sql"
        barrier = Barrier(2)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / filename).write_text(
                f"CREATE TABLE {table_name}(marker integer NOT NULL);"
                f"INSERT INTO {table_name}(marker) VALUES (1);",
                encoding="utf-8",
            )

            def apply(repository: PostgresRepository):
                barrier.wait(timeout=5)
                return repository.apply_migrations(root)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(apply, repositories))

                self.assertEqual(
                    sum(filename in result["applied"] for result in results),
                    1,
                )
                self.assertEqual(
                    sum(filename in result["skipped"] for result in results),
                    1,
                )
                with repositories[0].connection() as connection:
                    side_effect_count = connection.execute(
                        f"SELECT count(*) AS count FROM {table_name}"
                    ).fetchone()["count"]
                    ledger_count = connection.execute(
                        "SELECT count(*) AS count FROM schema_migrations "
                        "WHERE version=%s AND filename=%s",
                        (version, filename),
                    ).fetchone()["count"]
                self.assertEqual(int(side_effect_count), 1)
                self.assertEqual(int(ledger_count), 1)
            finally:
                with repositories[0].connection() as connection, connection.transaction():
                    connection.execute(f"DROP TABLE IF EXISTS {table_name}")
                    connection.execute(
                        "DELETE FROM schema_migrations WHERE version=%s AND filename=%s",
                        (version, filename),
                    )

    @unittest.skipUnless(
        os.environ.get("MECH_DESIGN_DATABASE_URL"),
        "MECH_DESIGN_DATABASE_URL is not configured; live index verification skipped",
    )
    def test_design_lesson_assertion_lookup_index_is_installed_by_later_migration(self) -> None:
        repository = PostgresRepository(os.environ["MECH_DESIGN_DATABASE_URL"])
        with postgres_migrations_directory() as migrations:
            repository.apply_migrations(migrations)

        with repository.connection() as connection:
            index = connection.execute(
                "SELECT to_regclass('public.design_lesson_assertions_assertion_id_idx')::text AS name"
            ).fetchone()

        self.assertEqual(index["name"], "design_lesson_assertions_assertion_id_idx")


if __name__ == "__main__":
    unittest.main()
