from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .secure_fs import FileIdentity


DEFAULT_DATABASE_URL = (
    "postgresql://mechanical_design:change-me@127.0.0.1:55432/mechanical_design"
)


def load_env_file(path: Path) -> None:
    """Load a small explicit KEY=VALUE file without another dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized = key.strip()
        if normalized:
            os.environ.setdefault(normalized, value.strip().strip('"').strip("'"))


def database_url_from_environment() -> str:
    requested = os.environ.get("MECH_DESIGN_ENV_FILE", "").strip()
    if requested:
        load_env_file(Path(requested).expanduser().resolve())
    return os.environ.get("MECH_DESIGN_DATABASE_URL", DEFAULT_DATABASE_URL)


@dataclass(frozen=True)
class DesignSettings:
    workspace: Path
    package_root: Path
    design_root: Path
    freecadcmd: Path
    freecadcmd_sha256: str
    freecadcmd_identity: FileIdentity
    freecadcmd_version: str


@dataclass(frozen=True)
class KnowledgeSettings:
    workspace: Path
    database_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    organization_id: str
    design_group_id: str


@dataclass(frozen=True)
class StandardPartSettings:
    workspace: Path
    catalog_root: Path | None


__all__ = [
    "DEFAULT_DATABASE_URL",
    "DesignSettings",
    "KnowledgeSettings",
    "StandardPartSettings",
    "database_url_from_environment",
    "load_env_file",
]
