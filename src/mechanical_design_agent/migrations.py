from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import as_file, files
import re
from pathlib import Path
from typing import Iterator


MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
NEO4J_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.cypher$")
NEO4J_CONSTRAINT_STATEMENT = re.compile(
    r"^CREATE\s+CONSTRAINT\s+([a-z][a-z0-9_]*)\s+IF\s+NOT\s+EXISTS\s+"
    r"FOR\s+\([^)]+\)\s+REQUIRE\s+.+\s+IS\s+UNIQUE$",
    re.IGNORECASE | re.DOTALL,
)


@contextmanager
def postgres_migrations_directory() -> Iterator[Path]:
    resource = files("mechanical_design_agent").joinpath(
        "resources", "migrations", "postgres"
    )
    with as_file(resource) as root:
        yield root


@contextmanager
def neo4j_migrations_directory() -> Iterator[Path]:
    resource = files("mechanical_design_agent").joinpath(
        "resources", "migrations", "neo4j"
    )
    with as_file(resource) as root:
        yield root


def discover_postgres_migrations(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    found: list[tuple[int, Path]] = []
    versions: set[int] = set()
    for path in sorted(root.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in versions:
            raise ValueError(f"duplicate migration version: {version:03d}")
        versions.add(version)
        found.append((version, path))
    return [path for _, path in sorted(found)]


def discover_neo4j_migrations(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    found: list[tuple[int, Path]] = []
    versions: set[int] = set()
    for path in sorted(root.glob("*.cypher")):
        match = NEO4J_MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in versions:
            raise ValueError(f"duplicate Neo4j migration version: {version:03d}")
        versions.add(version)
        found.append((version, path))
    return [path for _, path in sorted(found)]


def neo4j_constraint_names(paths: list[Path]) -> list[str]:
    names: list[str] = []
    for path in paths:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Neo4j migration is not UTF-8: {path.name}") from exc
        for raw_statement in text.split(";"):
            statement = raw_statement.strip()
            if not statement:
                continue
            match = NEO4J_CONSTRAINT_STATEMENT.fullmatch(statement)
            if match is None:
                raise ValueError(
                    f"unsupported Neo4j bootstrap migration statement: {path.name}"
                )
            name = match.group(1)
            if name in names:
                raise ValueError(f"duplicate Neo4j constraint name: {name}")
            names.append(name)
    return sorted(names)
