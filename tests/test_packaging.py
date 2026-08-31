from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zipfile

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


DATABASE_URL = os.environ.get("MECH_DESIGN_DATABASE_URL", "").strip()
NEO4J_URI = os.environ.get("MECH_DESIGN_NEO4J_URI", "").strip()
NEO4J_USER = os.environ.get("MECH_DESIGN_NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.environ.get("MECH_DESIGN_NEO4J_PASSWORD", "").strip()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MIGRATIONS = [
    "001_knowledge.sql",
]
EXPECTED_NEO4J_MIGRATIONS = [
    "001_constraints.cypher",
    "002_design_lessons.cypher",
    "003_projection_state.cypher",
]
EXPECTED_NEO4J_CONSTRAINTS = [
    "assertion_id_unique",
    "design_lesson_id_unique",
    "family_id_unique",
    "family_profile_id_unique",
    "model_revision_id_unique",
    "product_id_unique",
    "projection_state_name_unique",
    "source_node_key_unique",
    "subfamily_id_unique",
]


@unittest.skipUnless(
    DATABASE_URL and NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD,
    "knowledge services are not configured; installed-wheel migration test skipped",
)
class InstalledWheelMigrationTests(unittest.TestCase):
    def test_installed_wheel_migrates_a_fresh_postgresql_database(self) -> None:
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "uv is required to build the release wheel")
        database_name = f"packaging_migration_{uuid.uuid4().hex}"
        admin_config = conninfo_to_dict(DATABASE_URL)
        database_url = make_conninfo(**{**admin_config, "dbname": database_name})

        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )

        try:
            with tempfile.TemporaryDirectory(prefix="packaged-migrations-") as temporary:
                root = Path(temporary)
                dist = root / "dist"
                venv = root / "venv"
                environment = dict(os.environ)
                environment.setdefault("UV_CACHE_DIR", str(root / "uv-cache"))
                environment.pop("PYTHONPATH", None)
                subprocess.run(
                    [str(uv), "build", "--wheel", "--out-dir", str(dist)],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                wheel = next(dist.glob("*.whl"))
                with zipfile.ZipFile(wheel) as archive:
                    packaged = sorted(
                        Path(name).name
                        for name in archive.namelist()
                        if "/resources/migrations/postgres/" in name
                    )
                self.assertEqual(packaged, EXPECTED_MIGRATIONS)

                subprocess.run(
                    [
                        str(uv),
                        "venv",
                        "--python",
                        sys.executable,
                        str(venv),
                    ],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                cli = venv / (
                    "Scripts/mechanical-design.exe"
                    if os.name == "nt"
                    else "bin/mechanical-design"
                )
                subprocess.run(
                    [
                        str(uv),
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        str(wheel),
                    ],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )

                module_path = subprocess.run(
                    [
                        str(python),
                        "-c",
                        "import mechanical_design_agent as package; print(package.__file__)",
                    ],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertTrue(Path(module_path).is_relative_to(venv))

                migrate_environment = {
                    **environment,
                    "MECH_DESIGN_DATABASE_URL": database_url,
                }
                migrate_environment.pop("MECH_DESIGN_ENV_FILE", None)
                workspace = root / "workspace"
                initialized = subprocess.run(
                    [
                        str(cli), "init", "--workspace", str(workspace),
                        "--actor", "packaging-test", "--organization", "example-org",
                        "--design-group", "example-group",
                    ],
                    cwd=root,
                    env=migrate_environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    initialized.returncode,
                    0,
                    initialized.stderr + initialized.stdout,
                )
                result = subprocess.run(
                    [str(cli), "knowledge", "bootstrap", "--workspace", str(workspace)],
                    cwd=root,
                    env=migrate_environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(
                    json.loads(result.stdout)["postgresql"],
                    {"applied": EXPECTED_MIGRATIONS, "skipped": []},
                )

                with psycopg.connect(database_url) as connection:
                    ledger = connection.execute(
                        "SELECT version,filename FROM knowledge_schema_migrations ORDER BY version"
                    ).fetchall()
                self.assertEqual(
                    [(int(version), filename) for version, filename in ledger],
                    list(enumerate(EXPECTED_MIGRATIONS, start=1)),
                )
        finally:
            with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database_name)
                    )
                )


@unittest.skipUnless(
    NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD,
    "Neo4j live integration is not configured; installed-wheel migration test skipped",
)
class InstalledWheelNeo4jMigrationTests(unittest.TestCase):
    def test_installed_wheel_migrates_a_fresh_neo4j_database_idempotently(self) -> None:
        uv = shutil.which("uv")
        docker = shutil.which("docker")
        self.assertIsNotNone(uv, "uv is required to build the release wheel")
        self.assertIsNotNone(docker, "Docker is required for isolated Neo4j migration testing")
        container_name = f"packaged-neo4j-migration-{uuid.uuid4().hex}"
        password = f"packaging-{uuid.uuid4().hex}"
        image = os.environ.get("MECH_DESIGN_NEO4J_TEST_IMAGE", "neo4j:2026.06.0")
        container_started = False

        try:
            with tempfile.TemporaryDirectory(prefix="packaged-neo4j-migrations-") as temporary:
                root = Path(temporary)
                dist = root / "dist"
                venv = root / "venv"
                environment = dict(os.environ)
                environment.setdefault("UV_CACHE_DIR", str(root / "uv-cache"))
                environment.pop("PYTHONPATH", None)
                subprocess.run(
                    [str(uv), "build", "--wheel", "--out-dir", str(dist)],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                wheel = next(dist.glob("*.whl"))
                with zipfile.ZipFile(wheel) as archive:
                    packaged = sorted(
                        Path(name).name
                        for name in archive.namelist()
                        if "/resources/migrations/neo4j/" in name
                    )
                self.assertEqual(packaged, EXPECTED_NEO4J_MIGRATIONS)

                subprocess.run(
                    [str(uv), "venv", "--python", sys.executable, str(venv)],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                subprocess.run(
                    [str(uv), "pip", "install", "--python", str(python), str(wheel)],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                module_path = subprocess.run(
                    [
                        str(python),
                        "-c",
                        "import mechanical_design_agent as package; print(package.__file__)",
                    ],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertTrue(Path(module_path).is_relative_to(venv))

                subprocess.run(
                    [
                        str(docker),
                        "run",
                        "--detach",
                        "--rm",
                        "--name",
                        container_name,
                        "--publish",
                        "127.0.0.1::7687",
                        "--env",
                        f"NEO4J_AUTH=neo4j/{password}",
                        image,
                    ],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                container_started = True
                port_output = subprocess.run(
                    [str(docker), "port", container_name, "7687/tcp"],
                    cwd=root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                port = int(port_output.rsplit(":", 1)[1])

                for _ in range(60):
                    ready = subprocess.run(
                        [
                            str(docker),
                            "exec",
                            container_name,
                            "cypher-shell",
                            "-u",
                            "neo4j",
                            "-p",
                            password,
                            "RETURN 1;",
                        ],
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    self.fail("temporary Neo4j did not become ready within 60 seconds")

                installed_environment = {
                    **environment,
                    "PACKAGING_NEO4J_URI": f"bolt://127.0.0.1:{port}",
                    "PACKAGING_NEO4J_USER": "neo4j",
                    "PACKAGING_NEO4J_PASSWORD": password,
                }
                script = (
                    "import json, os\n"
                    "from mechanical_design_agent.projection import Neo4jProjection\n"
                    "projection = Neo4jProjection(os.environ['PACKAGING_NEO4J_URI'], "
                    "os.environ['PACKAGING_NEO4J_USER'], os.environ['PACKAGING_NEO4J_PASSWORD'])\n"
                    "def constraints():\n"
                    "    with projection._driver() as driver, driver.session() as session:\n"
                    "        return [record['name'] for record in session.run("
                    "'SHOW CONSTRAINTS YIELD name RETURN name ORDER BY name')]\n"
                    "before = constraints()\n"
                    "projection.initialize_constraints()\n"
                    "first = constraints()\n"
                    "projection.initialize_constraints()\n"
                    "second = constraints()\n"
                    "print(json.dumps({'before': before, 'first': first, 'second': second}))\n"
                )
                result = subprocess.run(
                    [str(python), "-c", script],
                    cwd=root,
                    env=installed_environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                migration_state = json.loads(result.stdout)
                self.assertEqual(migration_state["before"], [])
                self.assertEqual(migration_state["first"], EXPECTED_NEO4J_CONSTRAINTS)
                self.assertEqual(migration_state["second"], EXPECTED_NEO4J_CONSTRAINTS)
        finally:
            if container_started:
                subprocess.run(
                    [str(docker), "stop", container_name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
