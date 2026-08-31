from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .product_families import validate_product_family_config
from .workspace_bootstrap import (
    BootstrapFailure,
    ParsedEnvFile,
    parse_selected_env_file,
    validate_actor_id,
)
from .secure_fs import FileIdentity


DEFAULT_DATABASE_URL = (
    "postgresql://mechanical_design:change-me@127.0.0.1:55432/mechanical_design"
)


def load_env_file(path: Path) -> None:
    """Load a small KEY=VALUE file without adding a dotenv dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def database_url_from_environment() -> str:
    requested_env = os.environ.get("MECH_DESIGN_ENV_FILE", "").strip()
    if requested_env:
        load_env_file(Path(requested_env).expanduser().resolve())
    return os.environ.get("MECH_DESIGN_DATABASE_URL", DEFAULT_DATABASE_URL)


def _legacy_value(
    key: str,
    *,
    environ: Mapping[str, str],
    env_file: ParsedEnvFile | None,
) -> str | None:
    if key in environ:
        return environ[key]
    if env_file is not None and key in env_file.values:
        return env_file.values[key].value
    return None


def _legacy_failure(
    code: str,
    message: str,
    *,
    status: str = "blocked",
) -> BootstrapFailure:
    return BootstrapFailure(code, message, status=status)


@dataclass(frozen=True)
class Settings:
    workspace: Path
    package_root: Path
    database_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    freecadcmd: Path
    actor_id: str
    artifact_root: Path
    family_config_path: Path | None
    freecadcmd_sha256: str = ""
    freecadcmd_identity: FileIdentity | None = None
    freecadcmd_version: str = ""

    @classmethod
    def from_environment(cls) -> "Settings":
        package_root = Path(__file__).resolve().parents[2]
        environ: Mapping[str, str] = MappingProxyType(dict(os.environ))
        try:
            env_file = parse_selected_env_file(None, environ, Path.cwd())
        except BootstrapFailure as exc:
            raise _legacy_failure(
                "LEGACY_ENV_FILE_INVALID",
                f"explicit legacy env file is invalid ({exc.code})",
            ) from exc

        family_raw = _legacy_value(
            "MECH_DESIGN_FAMILY_CONFIG",
            environ=environ,
            env_file=env_file,
        )
        if family_raw is None or not family_raw.strip():
            raise _legacy_failure(
                "LEGACY_FAMILY_CONFIG_REQUIRED",
                "set MECH_DESIGN_FAMILY_CONFIG for the legacy settings API",
                status="setup_required",
            )
        actor_raw = _legacy_value(
            "MECH_DESIGN_ACTOR_ID",
            environ=environ,
            env_file=env_file,
        )
        if actor_raw is None or not actor_raw.strip():
            raise _legacy_failure(
                "LEGACY_ACTOR_ID_REQUIRED",
                "set MECH_DESIGN_ACTOR_ID for the legacy settings API",
                status="setup_required",
            )

        workspace_raw = _legacy_value(
            "MECH_DESIGN_WORKSPACE",
            environ=environ,
            env_file=env_file,
        )
        explicit_workspace = workspace_raw is not None and bool(workspace_raw.strip())
        if explicit_workspace:
            try:
                workspace = Path(workspace_raw).expanduser().resolve(strict=True)
            except OSError as exc:
                raise _legacy_failure(
                    "LEGACY_WORKSPACE_INVALID",
                    "the explicitly configured legacy workspace is invalid",
                ) from exc
            if not workspace.is_dir():
                raise _legacy_failure(
                    "LEGACY_WORKSPACE_INVALID",
                    "the explicitly configured legacy workspace is not a directory",
                )
        else:
            workspace = package_root.parent.resolve()

        family_setting = Path(family_raw).expanduser()
        if not family_setting.is_absolute():
            if not explicit_workspace:
                raise _legacy_failure(
                    "LEGACY_WORKSPACE_REQUIRED",
                    "a relative MECH_DESIGN_FAMILY_CONFIG requires MECH_DESIGN_WORKSPACE",
                    status="setup_required",
                )
            family_setting = workspace / family_setting
        try:
            family_path = family_setting.resolve(strict=True)
        except OSError as exc:
            raise _legacy_failure(
                "LEGACY_FAMILY_CONFIG_INVALID",
                "the explicitly configured legacy product-family file is invalid",
            ) from exc
        if not family_path.is_file():
            raise _legacy_failure(
                "LEGACY_FAMILY_CONFIG_INVALID",
                "the explicitly configured legacy product-family path is not a file",
            )
        if not Path(family_raw).expanduser().is_absolute() and not family_path.is_relative_to(
            workspace
        ):
            raise _legacy_failure(
                "LEGACY_FAMILY_CONFIG_INVALID",
                "the workspace-relative legacy product-family path escapes the workspace",
            )
        try:
            family_value = json.loads(family_path.read_text(encoding="utf-8"))
            validate_product_family_config(
                family_value,
                path=family_path,
                require_filename_match=False,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, BootstrapFailure) as exc:
            raise _legacy_failure(
                "LEGACY_FAMILY_CONFIG_INVALID",
                "the explicitly configured legacy product-family file is invalid",
            ) from exc

        try:
            actor_id = validate_actor_id(actor_raw)
        except BootstrapFailure as exc:
            raise _legacy_failure(
                "LEGACY_ACTOR_ID_INVALID",
                "MECH_DESIGN_ACTOR_ID is not a valid actor identifier",
            ) from exc

        artifact_setting = Path(
            _legacy_value(
                "MECH_DESIGN_ARTIFACT_ROOT",
                environ=environ,
                env_file=env_file,
            )
            or "data/artifacts"
        )
        artifact_root = artifact_setting if artifact_setting.is_absolute() else package_root / artifact_setting
        settings = cls(
            workspace=workspace,
            package_root=package_root,
            database_url=_legacy_value(
                "MECH_DESIGN_DATABASE_URL",
                environ=environ,
                env_file=env_file,
            )
            or DEFAULT_DATABASE_URL,
            neo4j_uri=_legacy_value(
                "MECH_DESIGN_NEO4J_URI",
                environ=environ,
                env_file=env_file,
            )
            or "bolt://127.0.0.1:57687",
            neo4j_user=_legacy_value(
                "MECH_DESIGN_NEO4J_USER",
                environ=environ,
                env_file=env_file,
            )
            or "neo4j",
            neo4j_password=_legacy_value(
                "MECH_DESIGN_NEO4J_PASSWORD",
                environ=environ,
                env_file=env_file,
            )
            or "change-me-too",
            freecadcmd=Path(
                _legacy_value(
                    "MECH_DESIGN_FREECADCMD",
                    environ=environ,
                    env_file=env_file,
                )
                or "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd"
            ).expanduser().resolve(),
            actor_id=actor_id,
            artifact_root=artifact_root.resolve(),
            family_config_path=family_path,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.workspace.is_dir():
            raise ValueError(f"MECH_DESIGN_WORKSPACE does not exist: {self.workspace}")
        if self.family_config_path is not None and not self.family_config_path.is_file():
            raise ValueError(f"family bootstrap config does not exist: {self.family_config_path}")
        if not self._is_under(self.artifact_root, self.workspace):
            raise ValueError("MECH_DESIGN_ARTIFACT_ROOT must remain inside the workspace")
        if not self.freecadcmd.is_file():
            raise ValueError(f"FreeCADCmd does not exist: {self.freecadcmd}")

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False


@dataclass(frozen=True)
class JobSettings:
    """Minimal runtime configuration for family-independent Job operations."""

    workspace: Path
    package_root: Path
    database_url: str
    actor_id: str
    organization_id: str
    design_group_id: str


@dataclass(frozen=True)
class JobCadSettings(JobSettings):
    """Family-independent Job authority plus the certified FreeCAD boundary."""

    freecadcmd: Path
    freecadcmd_sha256: str
    freecadcmd_identity: FileIdentity
    freecadcmd_version: str


@dataclass(frozen=True)
class DesignSettings:
    """Filesystem and certified-FreeCAD settings for design sessions."""

    workspace: Path
    package_root: Path
    design_root: Path
    freecadcmd: Path
    freecadcmd_sha256: str
    freecadcmd_identity: FileIdentity
    freecadcmd_version: str


def resolve_workspace_output(settings: Settings, requested: str | Path) -> Path:
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = settings.workspace / candidate
    candidate = candidate.expanduser().resolve()
    if not Settings._is_under(candidate, settings.workspace):
        raise ValueError(f"output path must remain inside workspace: {candidate}")
    return candidate
