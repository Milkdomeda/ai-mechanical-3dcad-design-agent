from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from .bootstrap_diagnostics import (
    DOCTOR_PARTICIPANTS,
    STATUS_PARTICIPANTS,
    CapabilityRequest,
    ComponentDiagnostic,
    DiagnosticGateError,
    build_diagnostic_report,
    guard_response,
)
from .product_families import (
    ProductFamilyCatalog,
    ProductFamilySelection,
    build_product_family_config,
    create_product_family_config,
    load_product_family_catalog,
    resolve_product_family,
    set_default_product_family as update_default_product_family,
)
from .freecad_discovery import (
    CERTIFIED_FREECADCMD_VERSIONS,
    FreeCADCandidate,
    FreeCADDiscoveryError,
    FreeCADDiscoveryResult,
    default_windows_discovery,
    run_freecad_version,
    validate_freecadcmd,
    validate_local_freecadcmd,
)
from .standard_part_configuration import (
    StandardPartSources,
    _configuration_result,
    disable_standard_part_catalog,
    enable_standard_part_catalog,
    load_standard_part_provider_catalog,
    load_standard_part_sources,
    probe_standard_part_catalog,
)
from .workspace_bootstrap import (
    DEFAULT_ARTIFACT_ROOT,
    BootstrapFailure,
    ParsedEnvFile,
    ResolvedSetting,
    SettingSource,
    WorkspaceManifest,
    WorkspaceSelection,
    parse_selected_env_file,
    read_workspace_manifest,
    resolve_setting,
    select_workspace,
    validate_actor_id,
    validate_workspace_managed_state,
)
from .secure_fs import (
    FileIdentity,
    SecureFilesystemError,
    atomic_publish_new,
    ensure_managed_directory,
    remove_owned_tree,
    read_managed_file,
    validate_managed_path,
)

if TYPE_CHECKING:
    from .config import JobCadSettings, JobSettings, Settings


_INIT_NEXT_STEP = "mechanical-design init --workspace <path>"

_RESOURCE_SETS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "freecad_scripts": (
            "freecad/create_empty_working_copy.py",
            "freecad/extract_model_manifest.py",
            "freecad/normalize_working_copy.py",
            "freecad/validate_external_step.py",
            "freecad/validate_fastener_interfaces.py",
            "freecad/validate_mechanical_interfaces.py",
            "freecad/validate_working_copy.py",
        ),
        "neo4j_migrations": (
            "migrations/neo4j/001_constraints.cypher",
            "migrations/neo4j/002_design_lessons.cypher",
            "migrations/neo4j/003_projection_state.cypher",
        ),
        "postgres_migrations": (
            "migrations/postgres/001_init.sql",
            "migrations/postgres/002_design_lessons.sql",
            "migrations/postgres/003_design_lesson_hardening.sql",
            "migrations/postgres/004_validation_report_digest.sql",
            "migrations/postgres/005_design_lesson_reviews.sql",
            "migrations/postgres/006_delivery_approval_binding.sql",
            "migrations/postgres/007_review_immutable_snapshots.sql",
            "migrations/postgres/008_drop_legacy_snapshot_constraints.sql",
            "migrations/postgres/009_design_lifecycle_closure.sql",
            "migrations/postgres/010_design_jobs.sql",
            "migrations/postgres/011_design_job_working_copies.sql",
            "migrations/postgres/012_design_job_binding_hardening.sql",
            "migrations/postgres/013_design_job_binding_security.sql",
            "migrations/postgres/014_design_job_knowledge.sql",
            "migrations/postgres/015_product_family_match_decisions.sql",
            "migrations/postgres/016_design_approval_envelopes.sql",
            "migrations/postgres/017_design_lesson_single_confirmation.sql",
            "migrations/postgres/018_design_job_obligations.sql",
        ),
        "schemas": (
            "schemas/design-lesson-package-v1.schema.json",
            "schemas/design-lesson-screening-package-v1.schema.json",
        ),
        "standard_part_provider_config": (
            "config/standard_part_providers.json",
        ),
        "validation": ("validation/step_component.json",),
    }
)


@dataclass(frozen=True)
class _SecretSetting:
    value: str | None
    source: SettingSource | None

    @property
    def configured(self) -> bool:
        return bool(self.value)

    def redacted(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "source": _source_dict(self.source) if self.source is not None else None,
        }


@dataclass(frozen=True)
class _Inspection:
    components: Mapping[str, ComponentDiagnostic]
    selection: WorkspaceSelection | None = None
    manifest: WorkspaceManifest | None = None
    actor: ResolvedSetting | None = None
    artifact_root: Path | None = None
    artifact_source: SettingSource | None = None
    freecad_command: Path | None = None
    freecad_candidate: FreeCADCandidate | None = None
    freecad_source: SettingSource | None = None
    secrets: Mapping[str, _SecretSetting] = MappingProxyType({})
    product_families: ProductFamilyCatalog | None = None
    selected_product_family: ProductFamilySelection | None = None
    standard_part_sources: StandardPartSources | None = None


@dataclass(frozen=True)
class ProbeResult:
    available: bool
    details: Mapping[str, object] = MappingProxyType({})
    error_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        if self.available and self.error_type is not None:
            raise ValueError("available probe result must not have an error type")
        if not self.available and not self.error_type:
            raise ValueError("unavailable probe result requires an error type")


@dataclass(frozen=True)
class DoctorProbes:
    postgresql: Callable[[str], ProbeResult]
    neo4j: Callable[[str, str, str], ProbeResult]
    freecadcmd: Callable[[Path, str, FileIdentity, Path], ProbeResult]
    artifact_root: Callable[[Path], ProbeResult]
    standard_part_catalog: Callable[[Path], ProbeResult] | None = None


def _source_dict(source: SettingSource) -> dict[str, object]:
    return {
        "kind": source.kind,
        "location": source.location,
        "line": source.line,
    }


def _diagnostic(
    name: str,
    status: str,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> ComponentDiagnostic:
    return ComponentDiagnostic(
        name=name,
        status=status,  # type: ignore[arg-type]
        code=code,
        message=message,
        details=details or {},
    )


def _not_evaluated_components() -> dict[str, ComponentDiagnostic]:
    names = (
        "workspace_selection",
        "workspace_manifest",
        "managed_config_integrity",
        "actor_identity",
        "artifact_root",
        "product_family",
        "postgresql",
        "neo4j",
        "freecadcmd",
        "standard_part_sources",
        "package_resources",
    )
    return {
        name: _diagnostic(
            name,
            "setup_required",
            f"{name.upper()}_NOT_EVALUATED",
            f"{name} cannot be evaluated before workspace initialization",
        )
        for name in names
    }


def _package_resources() -> ComponentDiagnostic:
    try:
        root = files("mechanical_design_agent").joinpath("resources")
        for relative_paths in _RESOURCE_SETS.values():
            for relative in relative_paths:
                resource = root.joinpath(*Path(relative).parts)
                if not resource.is_file() or not resource.read_bytes():
                    raise ValueError(f"missing or empty package resource: {relative}")
                if relative.endswith(".json"):
                    json.loads(resource.read_text(encoding="utf-8"))
        load_standard_part_provider_catalog()
    except Exception as exc:
        return _diagnostic(
            "package_resources",
            "blocked",
            "PACKAGE_RESOURCE_INVALID",
            f"required installed package resource is unavailable: {type(exc).__name__}",
        )
    return _diagnostic(
        "package_resources",
        "ok",
        "PACKAGE_RESOURCES_READY",
        "required installed package resources are available",
        {"verified_resource_sets": sorted(_RESOURCE_SETS)},
    )


def _secret_setting(
    key: str,
    environ: Mapping[str, str],
    env_file: ParsedEnvFile | None,
) -> _SecretSetting:
    if key in environ:
        value = environ[key].strip()
        return _SecretSetting(
            value=value or None,
            source=SettingSource(kind="process_environment"),
        )
    if env_file is not None and key in env_file.values:
        entry = env_file.values[key]
        return _SecretSetting(
            value=entry.value.strip() or None,
            source=SettingSource(
                kind="env_file",
                location=str(env_file.path),
                line=entry.line,
            ),
        )
    return _SecretSetting(value=None, source=None)


def _workspace_path(
    workspace: Path,
    resolved: ResolvedSetting,
    *,
    label: str,
) -> Path:
    candidate = Path(resolved.value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    canonical = candidate.resolve()
    if not canonical.is_relative_to(workspace):
        raise BootstrapFailure(
            f"{label.upper()}_OUTSIDE_WORKSPACE",
            f"{label} must remain inside the workspace",
        )
    return canonical


def _default_postgresql_probe(database_url: str) -> ProbeResult:
    import psycopg

    with psycopg.connect(
        database_url,
        connect_timeout=3,
        autocommit=True,
    ) as connection:
        row = connection.execute(
            "SELECT current_database(),current_setting('server_version'),"
            "EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector'),"
            "EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_trgm')"
        ).fetchone()
    return ProbeResult(
        available=True,
        details={
            "database": str(row[0]),
            "version": str(row[1]),
            "vector_enabled": bool(row[2]),
            "trigram_enabled": bool(row[3]),
        },
    )


def _default_neo4j_probe(uri: str, user: str, password: str) -> ProbeResult:
    from neo4j import GraphDatabase

    with GraphDatabase.driver(
        uri,
        auth=(user, password),
        connection_timeout=3,
        connection_acquisition_timeout=3,
    ) as driver:
        driver.verify_connectivity()
    return ProbeResult(available=True, details={"connectivity": "verified"})


def _default_freecadcmd_probe(
    command: Path,
    expected_sha256: str,
    expected_identity: FileIdentity,
    controlled_directory: Path,
) -> ProbeResult:
    environment = {
        "HOME": str(controlled_directory),
        "TMPDIR": str(controlled_directory),
        "TMP": str(controlled_directory),
        "TEMP": str(controlled_directory),
        "USERPROFILE": str(controlled_directory),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    try:
        before = read_managed_file(command)
        if (
            before.sha256 != expected_sha256
            or before.identity != expected_identity
            or before.link_count != 1
        ):
            raise RuntimeError("reviewed executable changed")
        try:
            result = subprocess.run(
                [str(command), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
                check=False,
                cwd=controlled_directory,
                env=environment,
            )
        finally:
            after = read_managed_file(command)
            if (
                after.sha256 != expected_sha256
                or after.identity != expected_identity
                or after.link_count != 1
            ):
                raise RuntimeError("reviewed executable changed")
    except Exception:
        return ProbeResult(
            available=False,
            error_type="FreeCADExecutableTrustError",
        )
    if result.returncode != 0:
        return ProbeResult(
            available=False,
            error_type=f"FreeCADCmdExit{result.returncode}",
        )
    return ProbeResult(
        available=True,
        details={"version_verified": True},
    )


def _default_artifact_root_probe(root: Path) -> ProbeResult:
    attempt = root / f".mechanical-design-doctor-{uuid.uuid4().hex}"
    created = False
    try:
        validate_managed_path(root, allow_missing_leaf=False)
        ensure_managed_directory(attempt, parents=False, exist_ok=False)
        created = True
        atomic_publish_new(attempt / "write-probe", b"artifact-root-write-probe\n")
    finally:
        if created:
            remove_owned_tree(
                attempt,
                expected_parent=root,
                label="artifact root write probe",
            )
    return ProbeResult(available=True, details={"write_probe": "passed"})


def _default_standard_part_catalog_probe(root: Path) -> ProbeResult:
    try:
        probe_standard_part_catalog(root)
    except BootstrapFailure as exc:
        return ProbeResult(
            available=False,
            details={"failure_code": exc.code},
            error_type=type(exc).__name__,
        )
    return ProbeResult(available=True, details={"write_probe": "passed"})


def _default_doctor_probes() -> DoctorProbes:
    return DoctorProbes(
        postgresql=_default_postgresql_probe,
        neo4j=_default_neo4j_probe,
        freecadcmd=_default_freecadcmd_probe,
        artifact_root=_default_artifact_root_probe,
        standard_part_catalog=_default_standard_part_catalog_probe,
    )


def _run_probe(callback: Callable[..., ProbeResult], *args: object) -> ProbeResult:
    try:
        result = callback(*args)
    except Exception as exc:
        return ProbeResult(
            available=False,
            error_type=(
                exc.code if isinstance(exc, SecureFilesystemError) else type(exc).__name__
            ),
        )
    if not isinstance(result, ProbeResult):
        return ProbeResult(
            available=False,
            error_type="InvalidProbeResult",
        )
    return result


class BootstrapRuntime:
    def __init__(
        self,
        *,
        cwd: Path,
        environ: Mapping[str, str],
        workspace: str | Path | None,
        env_file_path: str | None,
        env_file: ParsedEnvFile | None,
        product_family_id: str | None,
        freecad_command: str | Path | None,
        freecad_sha256: str | None,
        freecad_discovery: Callable[[Mapping[str, str]], FreeCADDiscoveryResult],
        freecad_validator: Callable[[Path, str], FreeCADCandidate] | None,
        bootstrap_failure: BootstrapFailure | None,
        probes: DoctorProbes,
    ) -> None:
        self.cwd = cwd.resolve()
        self.environ = MappingProxyType(dict(environ))
        self.runtime_workspace = workspace
        self.runtime_env_file = env_file_path
        self.env_file = env_file
        self.runtime_product_family_id = product_family_id
        self.runtime_freecad_command = freecad_command
        self.runtime_freecad_sha256 = freecad_sha256
        self.freecad_discovery = freecad_discovery
        self.freecad_validator = freecad_validator
        self.bootstrap_failure = bootstrap_failure
        self.probes = probes

    @classmethod
    def from_process(
        cls,
        *,
        cwd: Path,
        environ: Mapping[str, str],
        workspace: str | Path | None = None,
        env_file: str | Path | None = None,
        product_family_id: str | None = None,
        freecad_command: str | Path | None = None,
        freecad_sha256: str | None = None,
        freecad_discovery: Callable[
            [Mapping[str, str]], FreeCADDiscoveryResult
        ] | None = None,
        freecad_validator: Callable[[Path, str], FreeCADCandidate] | None = None,
        probes: DoctorProbes | None = None,
    ) -> "BootstrapRuntime":
        parsed: ParsedEnvFile | None = None
        failure: BootstrapFailure | None = None
        env_file_path = str(env_file) if env_file is not None else None
        try:
            parsed = parse_selected_env_file(env_file_path, environ, cwd)
        except BootstrapFailure as exc:
            failure = exc
        return cls(
            cwd=cwd,
            environ=environ,
            workspace=workspace,
            env_file_path=env_file_path,
            env_file=parsed,
            product_family_id=product_family_id,
            freecad_command=freecad_command,
            freecad_sha256=freecad_sha256,
            freecad_discovery=(
                freecad_discovery
                if freecad_discovery is not None
                else (
                    default_windows_discovery
                    if os.name == "nt"
                    else lambda _environ: FreeCADDiscoveryResult(None, (), False)
                )
            ),
            freecad_validator=freecad_validator,
            bootstrap_failure=failure,
            probes=probes or _default_doctor_probes(),
        )

    def _inspect(self) -> _Inspection:
        components = _not_evaluated_components()
        components["package_resources"] = _package_resources()
        if self.bootstrap_failure is not None:
            failure = self.bootstrap_failure
            components["workspace_selection"] = _diagnostic(
                "workspace_selection",
                failure.status,
                failure.code,
                failure.message,
            )
            return _Inspection(components=MappingProxyType(components))

        try:
            selection = select_workspace(
                runtime_workspace=self.runtime_workspace,
                environ=self.environ,
                env_file=self.env_file,
                cwd=self.cwd,
                require_manifest=False,
            )
        except BootstrapFailure as exc:
            code = (
                "WORKSPACE_NOT_INITIALIZED"
                if exc.code == "WORKSPACE_NOT_SELECTED"
                else exc.code
            )
            details: dict[str, object] = {}
            if exc.status == "setup_required":
                details["next_steps"] = [_INIT_NEXT_STEP]
            components["workspace_selection"] = _diagnostic(
                "workspace_selection",
                exc.status,
                code,
                "initialize or select a workspace" if code == "WORKSPACE_NOT_INITIALIZED" else exc.message,
                details,
            )
            return _Inspection(components=MappingProxyType(components))

        components["workspace_selection"] = _diagnostic(
            "workspace_selection",
            "ok",
            "WORKSPACE_SELECTED",
            "workspace selected",
            {
                "workspace": str(selection.path),
                "source": _source_dict(selection.source),
            },
        )
        try:
            manifest = read_workspace_manifest(selection.path)
        except BootstrapFailure as exc:
            code = (
                "WORKSPACE_NOT_INITIALIZED"
                if exc.code == "WORKSPACE_NOT_INITIALIZED"
                else exc.code
            )
            details = {"next_steps": [_INIT_NEXT_STEP]} if exc.status == "setup_required" else {}
            components["workspace_manifest"] = _diagnostic(
                "workspace_manifest",
                exc.status,
                code,
                "workspace manifest is not initialized" if code == "WORKSPACE_NOT_INITIALIZED" else exc.message,
                details,
            )
            return _Inspection(
                components=MappingProxyType(components),
                selection=selection,
            )

        components["workspace_manifest"] = _diagnostic(
            "workspace_manifest",
            "ok",
            "WORKSPACE_MANIFEST_READY",
            "workspace manifest is valid",
            {"workspace_id": str(manifest.workspace_id)},
        )
        standard_part_sources: StandardPartSources | None = None
        try:
            standard_part_sources = load_standard_part_sources(manifest)
        except BootstrapFailure as exc:
            components["standard_part_sources"] = _diagnostic(
                "standard_part_sources",
                exc.status,
                exc.code,
                exc.message,
            )
        else:
            details = standard_part_sources.catalog_dict()
            if not standard_part_sources.enabled:
                details["next_steps"] = (
                    "mechanical-design standard-parts catalog enable --root <existing-path>",
                )
            components["standard_part_sources"] = _diagnostic(
                "standard_part_sources",
                standard_part_sources.status,
                standard_part_sources.code,
                standard_part_sources.message,
                details,
            )
        try:
            validate_workspace_managed_state(manifest)
        except BootstrapFailure as exc:
            legacy_only = (
                standard_part_sources is not None
                and standard_part_sources.schema_kind == "legacy"
                and exc.code == "MANAGED_CONFIG_INVALID"
                and exc.message == "unsupported standard-part sources schema"
            )
            if legacy_only:
                components["managed_config_integrity"] = _diagnostic(
                    "managed_config_integrity",
                    "warning",
                    "STANDARD_PART_SOURCES_LEGACY_FORMAT",
                    "managed configuration uses the supported legacy standard-part source format",
                )
            else:
                components["managed_config_integrity"] = _diagnostic(
                    "managed_config_integrity",
                    exc.status,
                    exc.code,
                    exc.message,
                )
        else:
            components["managed_config_integrity"] = _diagnostic(
                "managed_config_integrity",
                "ok",
                "MANAGED_CONFIG_READY",
                "workspace-managed configuration is valid",
            )

        actor = resolve_setting(
            environment_key="MECH_DESIGN_ACTOR_ID",
            runtime_value=None,
            environ=self.environ,
            env_file=self.env_file,
            manifest_value=manifest.actor_id,
            package_default=manifest.actor_id,
        )
        try:
            validate_actor_id(actor.value)
        except BootstrapFailure as exc:
            components["actor_identity"] = _diagnostic(
                "actor_identity",
                exc.status,
                exc.code,
                exc.message,
                {"source": _source_dict(actor.source)},
            )
        else:
            components["actor_identity"] = _diagnostic(
                "actor_identity",
                "ok",
                "ACTOR_IDENTITY_READY",
                "effective actor identity is valid",
                {"source": _source_dict(actor.source)},
            )

        artifact_manifest_value = None
        paths = manifest.raw.get("paths")
        if isinstance(paths, Mapping):
            artifact_manifest_value = paths.get("artifact_root")
        artifact_setting = resolve_setting(
            environment_key="MECH_DESIGN_ARTIFACT_ROOT",
            runtime_value=None,
            environ=self.environ,
            env_file=self.env_file,
            manifest_value=artifact_manifest_value,
            package_default=DEFAULT_ARTIFACT_ROOT,
        )
        artifact_root: Path | None = None
        try:
            artifact_root = _workspace_path(
                manifest.workspace,
                artifact_setting,
                label="artifact_root",
            )
            if artifact_root.is_symlink() or not artifact_root.is_dir():
                raise BootstrapFailure(
                    "ARTIFACT_ROOT_INVALID",
                    "artifact root must be an existing real directory",
                )
        except BootstrapFailure as exc:
            components["artifact_root"] = _diagnostic(
                "artifact_root",
                exc.status,
                exc.code,
                exc.message,
                {"source": _source_dict(artifact_setting.source)},
            )
        else:
            components["artifact_root"] = _diagnostic(
                "artifact_root",
                "ok",
                "ARTIFACT_ROOT_READY",
                "artifact root is contained in the workspace",
                {"source": _source_dict(artifact_setting.source)},
            )

        product_families: ProductFamilyCatalog | None = None
        selected_product_family: ProductFamilySelection | None = None
        try:
            product_families = load_product_family_catalog(manifest.product_families)
        except BootstrapFailure as exc:
            components["product_family"] = _diagnostic(
                "product_family",
                "blocked",
                exc.code,
                exc.message,
                {"state": "invalid"},
            )
        else:
            try:
                selected_product_family = resolve_product_family(
                    catalog=product_families,
                    runtime_family_id=self.runtime_product_family_id,
                    environ=self.environ,
                    env_file=self.env_file,
                    manifest_default=manifest.default_product_family_id,
                )
            except BootstrapFailure as exc:
                state = "missing" if exc.code == "PRODUCT_FAMILY_NOT_FOUND" else "invalid"
                components["product_family"] = _diagnostic(
                    "product_family",
                    "blocked",
                    exc.code,
                    exc.message,
                    {"state": state},
                )
            else:
                if selected_product_family is not None:
                    components["product_family"] = _diagnostic(
                        "product_family",
                        "ok",
                        "PRODUCT_FAMILY_SELECTED",
                        "product family is selected for this operational session",
                        {
                            "state": "selected",
                            "family_id": selected_product_family.family_id,
                            "source": _source_dict(selected_product_family.source),
                        },
                    )
                elif product_families.state == "empty":
                    components["product_family"] = _diagnostic(
                        "product_family",
                        "ok",
                        "PRODUCT_FAMILY_EMPTY",
                        "product-family directory is empty",
                        {"state": "empty"},
                    )
                else:
                    components["product_family"] = _diagnostic(
                        "product_family",
                        "ok",
                        "PRODUCT_FAMILY_UNSELECTED",
                        "registered product families require an explicit selection",
                        {
                            "state": "unselected",
                            "available_family_ids": sorted(product_families.families),
                        },
                    )

        secrets = MappingProxyType(
            {
                "postgresql": _secret_setting(
                    "MECH_DESIGN_DATABASE_URL", self.environ, self.env_file
                ),
                "neo4j_uri": _secret_setting(
                    "MECH_DESIGN_NEO4J_URI", self.environ, self.env_file
                ),
                "neo4j_user": _secret_setting(
                    "MECH_DESIGN_NEO4J_USER", self.environ, self.env_file
                ),
                "neo4j_password": _secret_setting(
                    "MECH_DESIGN_NEO4J_PASSWORD", self.environ, self.env_file
                ),
            }
        )
        postgres = secrets["postgresql"]
        components["postgresql"] = _diagnostic(
            "postgresql",
            "ok" if postgres.configured else "setup_required",
            "POSTGRESQL_CONFIGURED" if postgres.configured else "POSTGRESQL_CREDENTIALS_REQUIRED",
            "PostgreSQL credentials are configured" if postgres.configured else "configure MECH_DESIGN_DATABASE_URL",
            postgres.redacted(),
        )
        neo4j_settings = (
            secrets["neo4j_uri"],
            secrets["neo4j_user"],
            secrets["neo4j_password"],
        )
        configured_count = sum(setting.configured for setting in neo4j_settings)
        if configured_count == 0:
            components["neo4j"] = _diagnostic(
                "neo4j",
                "setup_required",
                "NEO4J_CREDENTIALS_REQUIRED",
                "configure all Neo4j credential settings",
            )
        elif configured_count != len(neo4j_settings):
            components["neo4j"] = _diagnostic(
                "neo4j",
                "blocked",
                "NEO4J_CREDENTIALS_PARTIAL",
                "Neo4j URI, user, and password must be configured together",
            )
        else:
            components["neo4j"] = _diagnostic(
                "neo4j",
                "ok",
                "NEO4J_CONFIGURED",
                "Neo4j credentials are configured",
            )

        freecad_setting = resolve_setting(
            environment_key="MECH_DESIGN_FREECADCMD",
            runtime_value=self.runtime_freecad_command,
            environ=self.environ,
            env_file=self.env_file,
            manifest_value=manifest.freecad_command,
            package_default="",
        )
        freecad_hash_setting = resolve_setting(
            environment_key="MECH_DESIGN_FREECADCMD_SHA256",
            runtime_value=self.runtime_freecad_sha256,
            environ=self.environ,
            env_file=self.env_file,
            manifest_value=manifest.freecad_sha256,
            package_default="",
        )
        freecad_command: Path | None = None
        freecad_candidate: FreeCADCandidate | None = None
        freecad_source = freecad_setting.source
        if not freecad_setting.value.strip():
            discovery_environment = dict(self.environ)
            if freecad_hash_setting.value.strip():
                discovery_environment["MECH_DESIGN_FREECADCMD_SHA256"] = (
                    freecad_hash_setting.value.strip()
                )
            discovery = self.freecad_discovery(discovery_environment)
            if discovery.conflict:
                components["freecadcmd"] = _diagnostic(
                    "freecadcmd",
                    "warning",
                    "FREECADCMD_DISCOVERY_CONFLICT",
                    "multiple distinct FreeCADCmd installations require explicit selection",
                    {
                        "candidate_count": len(discovery.candidates),
                        "candidates": [
                            {"source": item.source, "version": item.version}
                            for item in discovery.candidates
                        ],
                    },
                )
            elif discovery.selected is None:
                components["freecadcmd"] = _diagnostic(
                    "freecadcmd",
                    "warning",
                    "FREECADCMD_NOT_CONFIGURED",
                    "FreeCADCmd is not configured",
                    {"source": _source_dict(freecad_setting.source)},
                )
            else:
                selected = discovery.selected
                freecad_candidate = selected
                freecad_command = selected.path
                freecad_source = SettingSource(
                    kind="platform_discovery",
                    location=selected.source,
                )
                certified = selected.version in CERTIFIED_FREECADCMD_VERSIONS
                components["freecadcmd"] = _diagnostic(
                    "freecadcmd",
                    "ok" if certified else "blocked",
                    (
                        "FREECADCMD_DISCOVERED"
                        if certified
                        else "FREECADCMD_VERSION_UNVALIDATED"
                    ),
                    (
                        f"FreeCADCmd {selected.version} was discovered"
                        if certified
                        else (
                            f"FreeCADCmd {selected.version} is not "
                            "release-validated"
                        )
                    ),
                    {
                        "source": _source_dict(freecad_source),
                        "version": selected.version,
                    },
                )
        else:
            requested = Path(freecad_setting.value).expanduser()
            if os.name != "nt" and not requested.is_absolute():
                requested = manifest.workspace / requested
            expected_digest = freecad_hash_setting.value.strip().lower()
            trust_failure: tuple[str, str] | None = None
            if not expected_digest:
                trust_failure = (
                    "FREECADCMD_SHA256_REQUIRED",
                    "configure the reviewed official FreeCAD 1.1.3 executable SHA-256",
                )
            elif re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
                trust_failure = (
                    "FREECADCMD_SHA256_INVALID",
                    "configured FreeCADCmd SHA-256 is invalid",
                )
            else:
                try:
                    executable_pin = read_managed_file(requested)
                except (OSError, SecureFilesystemError):
                    trust_failure = (
                        "FREECADCMD_EXECUTABLE_INVALID",
                        "FreeCADCmd cannot be pinned before its version probe",
                    )
                else:
                    if (
                        executable_pin.sha256 != expected_digest
                        or executable_pin.link_count != 1
                    ):
                        trust_failure = (
                            "FREECADCMD_SHA256_MISMATCH",
                            "FreeCADCmd does not match the reviewed executable SHA-256",
                        )
            if trust_failure is not None:
                components["freecadcmd"] = _diagnostic(
                    "freecadcmd",
                    "blocked",
                    trust_failure[0],
                    trust_failure[1],
                    {"source": _source_dict(freecad_hash_setting.source)},
                )
            else:
                try:
                    selected = (
                        self.freecad_validator(
                            requested,
                            freecad_setting.source.kind,
                        )
                        if self.freecad_validator is not None
                        else (
                            validate_freecadcmd
                            if os.name == "nt"
                            else validate_local_freecadcmd
                        )(
                            requested,
                            source=freecad_setting.source.kind,
                            run_version=run_freecad_version,
                            expected_sha256=expected_digest,
                        )
                    )
                except FreeCADDiscoveryError as exc:
                    components["freecadcmd"] = _diagnostic(
                        "freecadcmd",
                        "blocked",
                        exc.code,
                        exc.message,
                        {"source": _source_dict(freecad_setting.source)},
                    )
                else:
                    freecad_candidate = selected
                    freecad_command = selected.path
                    certified = selected.version in CERTIFIED_FREECADCMD_VERSIONS
                    components["freecadcmd"] = _diagnostic(
                        "freecadcmd",
                        "ok" if certified else "blocked",
                        (
                            "FREECADCMD_CONFIGURED"
                            if certified
                            else "FREECADCMD_VERSION_UNVALIDATED"
                        ),
                        (
                            f"FreeCADCmd {selected.version} path is configured"
                            if certified
                            else (
                                f"FreeCADCmd {selected.version} is not "
                                "release-validated"
                            )
                        ),
                        {
                            "source": _source_dict(freecad_source),
                            "version": selected.version,
                        },
                    )

        if (
            freecad_candidate is not None
            and freecad_candidate.version in CERTIFIED_FREECADCMD_VERSIONS
        ):
            expected_digest = freecad_hash_setting.value.strip().lower()
            if not expected_digest:
                components["freecadcmd"] = _diagnostic(
                    "freecadcmd",
                    "blocked",
                    "FREECADCMD_SHA256_REQUIRED",
                    "configure the reviewed official FreeCAD 1.1.3 executable SHA-256",
                    {"source": _source_dict(freecad_hash_setting.source)},
                )
            elif re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
                components["freecadcmd"] = _diagnostic(
                    "freecadcmd",
                    "blocked",
                    "FREECADCMD_SHA256_INVALID",
                    "configured FreeCADCmd SHA-256 is invalid",
                    {"source": _source_dict(freecad_hash_setting.source)},
                )
            elif freecad_candidate.sha256 != expected_digest:
                components["freecadcmd"] = _diagnostic(
                    "freecadcmd",
                    "blocked",
                    "FREECADCMD_SHA256_MISMATCH",
                    "FreeCADCmd does not match the reviewed executable SHA-256",
                    {"source": _source_dict(freecad_hash_setting.source)},
                )

        return _Inspection(
            components=MappingProxyType(components),
            selection=selection,
            manifest=manifest,
            actor=actor,
            artifact_root=artifact_root,
            artifact_source=artifact_setting.source,
            freecad_command=freecad_command,
            freecad_candidate=freecad_candidate,
            freecad_source=freecad_source,
            secrets=secrets,
            product_families=product_families,
            selected_product_family=selected_product_family,
            standard_part_sources=standard_part_sources,
        )

    def status(self) -> dict[str, object]:
        inspection = self._inspect()
        return build_diagnostic_report(
            kind="status",
            components=inspection.components,
            participants=STATUS_PARTICIPANTS,
        )

    @staticmethod
    def _probe_details(
        existing: ComponentDiagnostic,
        result: ProbeResult,
    ) -> dict[str, object]:
        details = dict(existing.details)
        details.update(result.details)
        if result.error_type is not None:
            details["error_type"] = result.error_type
        return details

    def _probed_components(
        self,
        inspection: _Inspection,
        participants: tuple[str, ...],
    ) -> dict[str, ComponentDiagnostic]:
        components = dict(inspection.components)
        if inspection.manifest is None:
            return components
        participating = set(participants)

        artifact = components["artifact_root"]
        if (
            "artifact_root" in participating
            and artifact.status == "ok"
            and inspection.artifact_root is not None
        ):
            result = _run_probe(self.probes.artifact_root, inspection.artifact_root)
            failure_code = (
                result.error_type
                if result.error_type is not None
                and result.error_type.startswith("WINDOWS_")
                else "ARTIFACT_ROOT_UNWRITABLE"
            )
            components["artifact_root"] = _diagnostic(
                "artifact_root",
                "ok" if result.available else "blocked",
                "ARTIFACT_ROOT_WRITABLE" if result.available else failure_code,
                "artifact root write probe passed" if result.available else "artifact root is not safely writable",
                self._probe_details(artifact, result),
            )

        postgres = components["postgresql"]
        postgres_secret = inspection.secrets.get("postgresql")
        if (
            "postgresql" in participating
            and postgres.status == "ok"
            and postgres_secret is not None
            and postgres_secret.value is not None
        ):
            result = _run_probe(self.probes.postgresql, postgres_secret.value)
            components["postgresql"] = _diagnostic(
                "postgresql",
                "ok" if result.available else "warning",
                "POSTGRESQL_READY" if result.available else "POSTGRESQL_UNAVAILABLE",
                "PostgreSQL connectivity is ready" if result.available else "configured PostgreSQL is unavailable",
                self._probe_details(postgres, result),
            )

        neo4j = components["neo4j"]
        neo4j_values = tuple(
            inspection.secrets.get(name)
            for name in ("neo4j_uri", "neo4j_user", "neo4j_password")
        )
        if (
            "neo4j" in participating
            and neo4j.status == "ok"
            and all(value is not None and value.value is not None for value in neo4j_values)
        ):
            uri, user, password = (value.value for value in neo4j_values if value is not None)
            assert uri is not None and user is not None and password is not None
            result = _run_probe(self.probes.neo4j, uri, user, password)
            components["neo4j"] = _diagnostic(
                "neo4j",
                "ok" if result.available else "warning",
                "NEO4J_READY" if result.available else "NEO4J_UNAVAILABLE",
                "Neo4j connectivity is ready" if result.available else "configured Neo4j is unavailable",
                self._probe_details(neo4j, result),
            )

        freecad = components["freecadcmd"]
        if (
            "freecadcmd" in participating
            and freecad.status == "ok"
            and inspection.freecad_command is not None
            and inspection.freecad_candidate is not None
            and inspection.artifact_root is not None
        ):
            result = _run_probe(
                self.probes.freecadcmd,
                inspection.freecad_command,
                inspection.freecad_candidate.sha256,
                inspection.freecad_candidate.identity,
                inspection.artifact_root,
            )
            components["freecadcmd"] = _diagnostic(
                "freecadcmd",
                "ok" if result.available else "warning",
                "FREECADCMD_READY" if result.available else "FREECADCMD_UNAVAILABLE",
                "FreeCADCmd version probe passed" if result.available else "configured FreeCADCmd is unavailable",
                self._probe_details(freecad, result),
            )

        standard_parts = components["standard_part_sources"]
        sources = inspection.standard_part_sources
        if (
            "standard_part_sources" in participating
            and standard_parts.status in {"ok", "warning"}
            and sources is not None
            and sources.enabled
            and sources.effective_root is not None
        ):
            callback = (
                self.probes.standard_part_catalog
                or _default_standard_part_catalog_probe
            )
            result = _run_probe(callback, sources.effective_root)
            details = self._probe_details(standard_parts, result)
            if result.available:
                components["standard_part_sources"] = _diagnostic(
                    "standard_part_sources",
                    standard_parts.status,
                    standard_parts.code,
                    standard_parts.message,
                    details,
                )
            else:
                failure_code = details.get("failure_code")
                code = (
                    str(failure_code)
                    if failure_code
                    in {
                        "STANDARD_PART_CATALOG_ROOT_NOT_WRITABLE",
                        "STANDARD_PART_CATALOG_PROBE_CLEANUP_FAILED",
                    }
                    else "STANDARD_PART_CATALOG_ROOT_NOT_WRITABLE"
                )
                components["standard_part_sources"] = _diagnostic(
                    "standard_part_sources",
                    "blocked",
                    code,
                    "standard-part catalog is not safely writable",
                    details,
                )
        return components

    @staticmethod
    def _capability_components(
        components: Mapping[str, ComponentDiagnostic],
        participants: tuple[str, ...],
        capability: str,
    ) -> dict[str, ComponentDiagnostic]:
        remapped = dict(components)
        for name in participants:
            diagnostic = remapped[name]
            if diagnostic.code in {
                "POSTGRESQL_UNAVAILABLE",
                "NEO4J_UNAVAILABLE",
                "FREECADCMD_UNAVAILABLE",
            }:
                remapped[name] = _diagnostic(
                    name,
                    "blocked",
                    diagnostic.code,
                    diagnostic.message,
                    diagnostic.details,
                )
            elif diagnostic.code in {
                "FREECADCMD_NOT_CONFIGURED",
                "FREECADCMD_DISCOVERY_CONFLICT",
            }:
                remapped[name] = _diagnostic(
                    name,
                    "setup_required",
                    diagnostic.code,
                    diagnostic.message,
                    diagnostic.details,
                )
            elif (
                diagnostic.code == "STANDARD_PART_CATALOG_DISABLED"
                and capability == "standard_part_catalog_write_or_reuse"
            ):
                remapped[name] = _diagnostic(
                    name,
                    "setup_required",
                    diagnostic.code,
                    diagnostic.message,
                    diagnostic.details,
                )
            elif diagnostic.code in {"PRODUCT_FAMILY_EMPTY", "PRODUCT_FAMILY_UNSELECTED"}:
                code = (
                    "PRODUCT_FAMILY_REQUIRED"
                    if diagnostic.code == "PRODUCT_FAMILY_EMPTY"
                    else "PRODUCT_FAMILY_SELECTION_REQUIRED"
                )
                next_step = (
                    "mechanical-design family create --help"
                    if diagnostic.code == "PRODUCT_FAMILY_EMPTY"
                    else "mechanical-design family set-default <family_id>"
                )
                remapped[name] = _diagnostic(
                    name,
                    "setup_required",
                    code,
                    "create a product family" if diagnostic.code == "PRODUCT_FAMILY_EMPTY" else "select a registered product family",
                    {
                        **dict(diagnostic.details),
                        "next_steps": [next_step],
                    },
                )
        return remapped

    def doctor(self) -> dict[str, object]:
        inspection = self._inspect()
        components = self._probed_components(inspection, DOCTOR_PARTICIPANTS)
        return build_diagnostic_report(
            kind="doctor",
            components=components,
            participants=DOCTOR_PARTICIPANTS,
        )

    def capability_report(
        self,
        request: str | CapabilityRequest,
        *,
        probe: bool,
    ) -> dict[str, object]:
        capability = request if isinstance(request, CapabilityRequest) else CapabilityRequest(request)
        inspection = self._inspect()
        components: Mapping[str, ComponentDiagnostic] = inspection.components
        if probe:
            components = self._probed_components(inspection, capability.participants)
        components = self._capability_components(
            components,
            capability.participants,
            capability.capability,
        )
        return build_diagnostic_report(
            kind="capability",
            capability=capability.capability,
            components=components,
            participants=capability.participants,
        )

    @staticmethod
    def _require_report(
        report: dict[str, object],
        *,
        capability: str,
    ) -> dict[str, object]:
        status = report.get("status")
        if not isinstance(status, Mapping):
            raise ValueError("diagnostic report status must be a mapping")
        if status.get("overall") in {"setup_required", "blocked"}:
            raise DiagnosticGateError(
                guard_response(report, capability=capability)
            )
        return report

    def require_initialized(self, capability: str) -> dict[str, object]:
        inspection = self._inspect()
        report = build_diagnostic_report(
            kind="capability",
            capability=capability,
            components=inspection.components,
            participants=(
                "workspace_selection",
                "workspace_manifest",
                "managed_config_integrity",
            ),
        )
        return self._require_report(report, capability=capability)

    def require_capability(
        self,
        request: str | CapabilityRequest,
        *,
        probe: bool,
    ) -> dict[str, object]:
        capability = request.capability if isinstance(request, CapabilityRequest) else request
        report = self.capability_report(request, probe=probe)
        return self._require_report(report, capability=capability)

    def secret_value(self, name: str) -> str | None:
        inspection = self._inspect()
        setting = inspection.secrets.get(name)
        return setting.value if setting is not None else None

    def blocked_response(
        self,
        *,
        capability: str,
        code: str,
        message: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "MechanicalDesignSetupResponse/v1",
            "status": "blocked",
            "code": code,
            "message": message,
            "capability": capability,
            "next_steps": ["mechanical-design doctor"],
            "diagnostics": self.capability_report(capability, probe=False),
        }

    @staticmethod
    def standard_part_providers(category: str = "") -> dict[str, object]:
        try:
            return load_standard_part_provider_catalog().as_dict(category)
        except BootstrapFailure as exc:
            return exc.as_dict()

    @staticmethod
    def _standard_part_configuration_failure(
        *,
        operation: str,
        report: dict[str, object],
        sources: StandardPartSources | None,
    ) -> dict[str, object]:
        guarded = guard_response(report, capability=str(report.get("capability")))
        next_steps = guarded.get("next_steps", [])
        return _configuration_result(
            operation=operation,
            status=str(guarded["status"]),
            code=str(guarded["code"]),
            message=str(guarded["message"]),
            changed=False,
            sources=sources,
            next_actions=tuple(
                step for step in next_steps if isinstance(step, str)
            ),
        )

    def standard_part_sources_status(self) -> dict[str, object]:
        report = self.capability_report(
            "standard_part_config_inspection",
            probe=False,
        )
        inspection = self._inspect()
        overall = report["status"]["overall"]
        if overall in {"setup_required", "blocked"}:
            return self._standard_part_configuration_failure(
                operation="status",
                report=report,
                sources=inspection.standard_part_sources,
            )
        sources = inspection.standard_part_sources
        assert sources is not None
        return _configuration_result(
            operation="status",
            status=sources.status,
            code=sources.code,
            message=sources.message,
            changed=False,
            sources=sources,
            next_actions=(
                (
                    "mechanical-design standard-parts catalog enable "
                    "--root <existing-path>"
                ),
            )
            if not sources.enabled
            else (),
        )

    def standard_part_catalog_enable(
        self,
        root_path: str | Path,
    ) -> dict[str, object]:
        report = self.capability_report("standard_part_config_update", probe=False)
        inspection = self._inspect()
        overall = report["status"]["overall"]
        if overall in {"setup_required", "blocked"}:
            return self._standard_part_configuration_failure(
                operation="catalog_enable",
                report=report,
                sources=inspection.standard_part_sources,
            )
        assert inspection.manifest is not None
        try:
            return enable_standard_part_catalog(
                manifest=inspection.manifest,
                root_path=root_path,
            )
        except BootstrapFailure as exc:
            return _configuration_result(
                operation="catalog_enable",
                status=exc.status,
                code=exc.code,
                message=exc.message,
                changed=False,
                sources=inspection.standard_part_sources,
            )

    def standard_part_catalog_disable(self) -> dict[str, object]:
        report = self.capability_report("standard_part_config_update", probe=False)
        inspection = self._inspect()
        overall = report["status"]["overall"]
        if overall in {"setup_required", "blocked"}:
            return self._standard_part_configuration_failure(
                operation="catalog_disable",
                report=report,
                sources=inspection.standard_part_sources,
            )
        assert inspection.manifest is not None
        try:
            return disable_standard_part_catalog(manifest=inspection.manifest)
        except BootstrapFailure as exc:
            return _configuration_result(
                operation="catalog_disable",
                status=exc.status,
                code=exc.code,
                message=exc.message,
                changed=False,
                sources=inspection.standard_part_sources,
            )

    def _family_inspection(self) -> _Inspection:
        self.require_capability("config_inspection", probe=False)
        inspection = self._inspect()
        assert inspection.manifest is not None
        assert inspection.actor is not None
        diagnostic = inspection.components["product_family"]
        if diagnostic.status == "blocked":
            raise BootstrapFailure(
                diagnostic.code,
                diagnostic.message,
            )
        assert inspection.product_families is not None
        return inspection

    def for_product_family(self, product_family_id: str) -> "BootstrapRuntime":
        return BootstrapRuntime(
            cwd=self.cwd,
            environ=self.environ,
            workspace=self.runtime_workspace,
            env_file_path=self.runtime_env_file,
            env_file=self.env_file,
            product_family_id=product_family_id,
            freecad_command=self.runtime_freecad_command,
            freecad_sha256=self.runtime_freecad_sha256,
            freecad_discovery=self.freecad_discovery,
            freecad_validator=self.freecad_validator,
            bootstrap_failure=self.bootstrap_failure,
            probes=self.probes,
        )

    def list_product_families(self) -> dict[str, object]:
        inspection = self._family_inspection()
        assert inspection.product_families is not None
        selected = inspection.selected_product_family
        return {
            "schema_version": "MechanicalDesignProductFamilyList/v1",
            "status": "ok",
            "source": "workspace_config",
            "authority": "bootstrap_configuration_only",
            "state": inspection.product_families.state,
            "families": [
                {
                    "family_id": family.family_id,
                    "family_name": family.value["family_name"],
                    "organization_id": family.value["organization_id"],
                    "design_group_id": family.value["design_group_id"],
                }
                for family in inspection.product_families.families.values()
            ],
            "selected_family_id": selected.family_id if selected is not None else None,
        }

    def create_product_family(
        self,
        *,
        organization_id: str,
        organization_name: str,
        design_group_id: str,
        design_group_name: str,
        family_id: str,
        family_name: str,
        aliases: list[str],
    ) -> dict[str, object]:
        inspection = self._family_inspection()
        assert inspection.manifest is not None
        assert inspection.actor is not None
        config = build_product_family_config(
            organization_id=organization_id,
            organization_name=organization_name,
            design_group_id=design_group_id,
            design_group_name=design_group_name,
            family_id=family_id,
            family_name=family_name,
            aliases=aliases,
            actor_id=inspection.actor.value,
        )
        return create_product_family_config(
            manifest=inspection.manifest,
            config=config,
        )

    def set_default_product_family(self, family_id: str) -> dict[str, object]:
        inspection = self._family_inspection()
        assert inspection.manifest is not None
        return update_default_product_family(
            manifest=inspection.manifest,
            family_id=family_id,
        )

    def active_product_family(self) -> dict[str, object]:
        inspection = self._family_inspection()
        catalog = inspection.product_families
        assert catalog is not None
        selected = inspection.selected_product_family
        if selected is not None:
            return {
                "schema_version": "MechanicalDesignProductFamilySelection/v1",
                "status": "ok",
                "code": "PRODUCT_FAMILY_SELECTED",
                "message": "product family is selected for this operational session",
                "state": "selected",
                "family_id": selected.family_id,
                "family_name": selected.config.value["family_name"],
                "path": str(selected.config.path),
                "source": _source_dict(selected.source),
            }
        empty = catalog.state == "empty"
        return {
            "schema_version": "MechanicalDesignProductFamilySelection/v1",
            "status": "setup_required",
            "code": (
                "PRODUCT_FAMILY_REQUIRED"
                if empty
                else "PRODUCT_FAMILY_SELECTION_REQUIRED"
            ),
            "message": (
                "create a product family"
                if empty
                else "select a registered product family"
            ),
            "state": "empty" if empty else "unselected",
            "available_family_ids": sorted(catalog.families),
            "next_steps": [
                "mechanical-design family create --help"
                if empty
                else "mechanical-design family set-default <family_id>"
            ],
        }

    def operational_settings(self) -> Settings:
        """Resolve family-neutral operational settings for ordinary design."""
        from .config import Settings

        inspection = self._inspect()
        assert inspection.manifest is not None
        assert inspection.actor is not None
        assert inspection.artifact_root is not None
        database_url = inspection.secrets["postgresql"].value
        assert database_url is not None
        return Settings(
            workspace=inspection.manifest.workspace,
            package_root=inspection.manifest.workspace,
            database_url=database_url,
            neo4j_uri=inspection.secrets["neo4j_uri"].value or "",
            neo4j_user=inspection.secrets["neo4j_user"].value or "",
            neo4j_password=inspection.secrets["neo4j_password"].value or "",
            freecadcmd=inspection.freecad_command or Path("FreeCADCmd"),
            actor_id=inspection.actor.value,
            artifact_root=inspection.artifact_root,
            family_config_path=(
                inspection.selected_product_family.config.path
                if inspection.selected_product_family is not None
                else None
            ),
            freecadcmd_sha256=(
                inspection.freecad_candidate.sha256
                if inspection.freecad_candidate is not None
                else ""
            ),
            freecadcmd_identity=(
                inspection.freecad_candidate.identity
                if inspection.freecad_candidate is not None
                else None
            ),
            freecadcmd_version=(
                inspection.freecad_candidate.version
                if inspection.freecad_candidate is not None
                else ""
            ),
        )

    def family_operational_settings(self) -> Settings:
        """Resolve settings for an operation whose subject is a Product Family."""
        request = CapabilityRequest(
            "family_create_or_manage",
            additional_components=("product_family",),
        )
        self.require_capability(request, probe=False)
        settings = self.operational_settings()
        if settings.family_config_path is None:
            raise RuntimeError("selected Product Family configuration is required")
        return settings

    def job_operational_settings(self) -> JobSettings:
        """Resolve the Job authority without selecting a product family."""
        from .config import JobSettings

        self.require_capability(CapabilityRequest("design_job_workspace"), probe=False)
        inspection = self._inspect()
        assert inspection.manifest is not None
        assert inspection.actor is not None
        database_url = inspection.secrets["postgresql"].value
        assert database_url is not None
        identity = inspection.manifest.raw.get("identity")
        if not isinstance(identity, Mapping):
            raise RuntimeError("configured Job scope is unavailable")
        organization_id = identity.get("organization_id")
        design_group_id = identity.get("design_group_id")
        if not isinstance(organization_id, str) or not organization_id.strip() or not isinstance(design_group_id, str) or not design_group_id.strip():
            raise RuntimeError("configured Job organization and design group are required")
        return JobSettings(
            workspace=inspection.manifest.workspace,
            package_root=inspection.manifest.workspace,
            database_url=database_url,
            actor_id=inspection.actor.value,
            organization_id=organization_id.strip(),
            design_group_id=design_group_id.strip(),
        )

    def job_cad_operational_settings(self) -> JobCadSettings:
        """Resolve scoped Job authority with the certified FreeCAD boundary."""
        from .config import JobCadSettings

        self.require_capability(
            CapabilityRequest(
                "design_job_workspace",
                additional_components=("freecadcmd",),
            ),
            probe=False,
        )
        job = self.job_operational_settings()
        inspection = self._inspect()
        if inspection.freecad_command is None or inspection.freecad_candidate is None:
            raise RuntimeError("configured FreeCADCmd is required for Job CAD")
        candidate = inspection.freecad_candidate
        return JobCadSettings(
            workspace=job.workspace,
            package_root=job.package_root,
            database_url=job.database_url,
            actor_id=job.actor_id,
            organization_id=job.organization_id,
            design_group_id=job.design_group_id,
            freecadcmd=inspection.freecad_command,
            freecadcmd_sha256=candidate.sha256,
            freecadcmd_identity=candidate.identity,
            freecadcmd_version=candidate.version,
        )

    def config_show(self) -> dict[str, object]:
        inspection = self._inspect()
        report = build_diagnostic_report(
            kind="capability",
            capability="config_inspection",
            components=inspection.components,
            participants=CapabilityRequest("config_inspection").participants,
        )
        if report["status"]["overall"] in {"setup_required", "blocked"}:
            return report
        assert inspection.selection is not None
        assert inspection.manifest is not None
        assert inspection.actor is not None
        return {
            "schema_version": "MechanicalDesignConfig/v1",
            "status": "ok",
            "workspace": {
                "path": str(inspection.selection.path),
                "source": _source_dict(inspection.selection.source),
            },
            "manifest": {
                "workspace_id": str(inspection.manifest.workspace_id),
                "actor_id": {
                    "value": inspection.actor.value,
                    "source": _source_dict(inspection.actor.source),
                },
                "artifact_root": {
                    "value": str(inspection.artifact_root),
                    "source": _source_dict(inspection.artifact_source),
                },
                "freecad_command": {
                    "value": (
                        str(inspection.freecad_command)
                        if inspection.freecad_command is not None
                        else None
                    ),
                    "source": _source_dict(inspection.freecad_source),
                },
                "default_product_family_id": inspection.manifest.default_product_family_id,
            },
            "secrets": {
                name: setting.redacted()
                for name, setting in inspection.secrets.items()
            },
        }
