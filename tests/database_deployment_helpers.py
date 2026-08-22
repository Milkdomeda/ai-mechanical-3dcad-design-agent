from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET


PROJECT_NAME = re.compile(r"md3dcad-[0-9a-f]{32}")
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_CLEAN_ENVIRONMENT_KEYS = frozenset(
    {
        "PYTHONPATH",
        "MECH_DESIGN_WORKSPACE",
        "MECH_DESIGN_ENV_FILE",
        "MECH_DESIGN_ACTOR_ID",
        "MECH_DESIGN_DATABASE_URL",
        "MECH_DESIGN_NEO4J_URI",
        "MECH_DESIGN_NEO4J_USER",
        "MECH_DESIGN_NEO4J_PASSWORD",
        "MECH_DESIGN_FREECADCMD",
        "MECH_DESIGN_ARTIFACT_ROOT",
        "MECH_DESIGN_PRODUCT_FAMILY_ID",
        "MECH_DESIGN_FAMILY_CONFIG",
        "MECH_DESIGN_DOCKER_DATABASE_LIVE",
        "MECH_DESIGN_WINDOWS_DB_LIVE_TESTS",
        "MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_LIVE_TESTS",
        "MECH_DESIGN_FREECAD_GUI_MCP_LIVE_TESTS",
    }
)


class DeploymentGateError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        stage: str,
        indicator: str,
        timeout_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.indicator = indicator
        self.timeout_seconds = timeout_seconds
        details = [code, f"stage={stage}", f"indicator={indicator}"]
        if timeout_seconds is not None:
            details.append(f"timeout_seconds={timeout_seconds}")
        super().__init__(" ".join(details))


@dataclass(frozen=True)
class AttemptLayout:
    project: str
    root: Path
    deployment: Path
    workspace: Path
    build: Path
    raw: Path
    safe_evidence: Path
    env_file: Path


@dataclass(frozen=True)
class CheckedResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DockerProjectInventory:
    containers: tuple[str, ...]
    networks: tuple[str, ...]
    volumes: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.containers or self.networks or self.volumes)

    def by_kind(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return (
            ("container", self.containers),
            ("network", self.networks),
            ("volume", self.volumes),
        )


def pytest_junit_fingerprint(path: Path) -> dict[str, object]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {
            "tests": 0,
            "failures": 0,
            "errors": 1,
            "skipped": 0,
            "failed_nodes": ("junit_unavailable",),
        }

    failed_nodes: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        classname = case.attrib.get("classname", "unknown")
        name = case.attrib.get("name", "unknown")
        failed_nodes.append(f"{classname}::{name}")

    def _count(name: str) -> int:
        return int(root.attrib.get(name, "0"))

    return {
        "tests": _count("tests"),
        "failures": _count("failures"),
        "errors": _count("errors"),
        "skipped": _count("skipped"),
        "failed_nodes": tuple(failed_nodes),
    }


def installed_script_path(
    executable: Path,
    name: str,
    *,
    os_name: str = os.name,
) -> Path:
    suffix = ".exe" if os_name == "nt" else ""
    return executable.parent / f"{name}{suffix}"


def neo4j_empty_state_query() -> str:
    return (
        "MATCH (n) WITH count(n) AS nodes "
        "OPTIONAL MATCH ()-[r]->() RETURN nodes,count(r) AS relationships"
    )


def supported_live_platform(platform_name: str) -> bool:
    return platform_name in {"darwin", "win32"}


def docker_platform_architecture(machine: str) -> str | None:
    return {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine.lower())


def live_evidence_filename(platform_name: str) -> str:
    names = {
        "darwin": "macos-d3-database-deployment-safe.json",
        "win32": "windows-d3-database-deployment-safe.json",
    }
    try:
        return names[platform_name]
    except KeyError:
        raise ValueError("unsupported live platform") from None


def _unsafe_attempt_base(path: Path) -> bool:
    expanded = path.expanduser()
    return expanded.is_symlink()


def create_attempt_layout(
    base: Path,
    *,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> AttemptLayout:
    if _unsafe_attempt_base(base):
        raise DeploymentGateError(
            "ATTEMPT_BASE_UNSAFE",
            stage="ATTEMPT_LAYOUT",
            indicator="symlink_or_reparse",
        )
    try:
        canonical_base = base.expanduser().resolve(strict=True)
    except OSError as exc:
        raise DeploymentGateError(
            "ATTEMPT_BASE_UNSAFE",
            stage="ATTEMPT_LAYOUT",
            indicator=type(exc).__name__,
        ) from None
    if not canonical_base.is_dir():
        raise DeploymentGateError(
            "ATTEMPT_BASE_UNSAFE",
            stage="ATTEMPT_LAYOUT",
            indicator="not_directory",
        )
    project = f"md3dcad-{uuid_factory().hex}"
    root = canonical_base / project
    try:
        root.mkdir(mode=0o700)
        directories = {
            name: root / name
            for name in ("deployment", "workspace", "build", "raw", "safe-evidence")
        }
        for directory in directories.values():
            directory.mkdir(mode=0o700)
    except OSError as exc:
        raise DeploymentGateError(
            "ATTEMPT_LAYOUT_CREATE_FAILED",
            stage="ATTEMPT_LAYOUT",
            indicator=type(exc).__name__,
        ) from None
    return AttemptLayout(
        project=project,
        root=root,
        deployment=directories["deployment"],
        workspace=directories["workspace"],
        build=directories["build"],
        raw=directories["raw"],
        safe_evidence=directories["safe-evidence"],
        env_file=root / "database.env",
    )


def clean_deployment_environment(
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    canonical_root = root.resolve()
    environment = dict(os.environ if environ is None else environ)
    original_home = environment.get("HOME", "").strip()
    prepared_cache = environment.get("UV_CACHE_DIR", "").strip()
    docker_config = environment.get("DOCKER_CONFIG", "").strip()
    if not docker_config and original_home:
        docker_config = str(Path(original_home).expanduser() / ".docker")
    for key in _CLEAN_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    home = canonical_root / "home"
    cache = (
        Path(prepared_cache).expanduser().resolve()
        if prepared_cache
        else canonical_root / "uv-cache"
    )
    home.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    environment["HOME"] = str(home)
    environment["UV_CACHE_DIR"] = str(cache)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    if docker_config:
        environment["DOCKER_CONFIG"] = docker_config
    return environment


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materialize_candidate_inputs(
    source_root: Path,
    layout: AttemptLayout,
) -> dict[str, str]:
    source = source_root.resolve(strict=True)
    deployment = layout.deployment.resolve(strict=True)
    if any(deployment.iterdir()):
        raise DeploymentGateError(
            "CANDIDATE_DEPLOYMENT_NOT_EMPTY",
            stage="CANDIDATE_MATERIALIZATION",
            indicator="preexisting_files",
        )
    selected = {
        "compose_sha256": (source / "compose.yaml", deployment / "compose.yaml"),
        "env_example_sha256": (
            source / ".env.example",
            deployment / ".env.example",
        ),
    }
    evidence: dict[str, str] = {}
    try:
        for key, (candidate, destination) in selected.items():
            canonical = candidate.resolve(strict=True)
            if candidate.is_symlink() or not canonical.is_file():
                raise OSError("candidate input is not a regular file")
            shutil.copyfile(canonical, destination)
            if destination.read_bytes() != canonical.read_bytes():
                raise OSError("candidate input copy mismatch")
            evidence[key] = _sha256(destination)
    except OSError as exc:
        raise DeploymentGateError(
            "CANDIDATE_MATERIALIZATION_FAILED",
            stage="CANDIDATE_MATERIALIZATION",
            indicator=type(exc).__name__,
        ) from None
    return evidence


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stage: str,
    timeout_seconds: int,
    runner: Callable[..., object] = subprocess.run,
    junit_path: Path | None = None,
) -> CheckedResult:
    try:
        result = runner(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise DeploymentGateError(
            "SUBPROCESS_TIMEOUT",
            stage=stage,
            indicator="timeout",
            timeout_seconds=timeout_seconds,
        ) from None
    except OSError as exc:
        raise DeploymentGateError(
            "SUBPROCESS_START_FAILED",
            stage=stage,
            indicator=type(exc).__name__,
            timeout_seconds=timeout_seconds,
        ) from None
    returncode = int(getattr(result, "returncode"))
    stdout_value = getattr(result, "stdout", b"") or b""
    stderr_value = getattr(result, "stderr", b"") or b""
    stdout = (
        stdout_value.decode("utf-8", errors="replace")
        if isinstance(stdout_value, bytes)
        else str(stdout_value)
    )
    stderr = (
        stderr_value.decode("utf-8", errors="replace")
        if isinstance(stderr_value, bytes)
        else str(stderr_value)
    )
    if returncode != 0:
        indicator = f"returncode_{returncode}"
        if junit_path is not None:
            fingerprint = pytest_junit_fingerprint(junit_path)
            nodes = ",".join(fingerprint["failed_nodes"])
            indicator = (
                f"pytest_tests_{fingerprint['tests']}_failures_"
                f"{fingerprint['failures']}_errors_{fingerprint['errors']}_"
                f"skipped_{fingerprint['skipped']}_nodes_{nodes}"
            )
        raise DeploymentGateError(
            "SUBPROCESS_FAILED",
            stage=stage,
            indicator=indicator,
            timeout_seconds=timeout_seconds,
        )
    return CheckedResult(returncode=returncode, stdout=stdout, stderr=stderr)


class DockerComposeBackend:
    def __init__(
        self,
        *,
        compose_file: Path,
        env_file: Path,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> None:
        self.compose_file = compose_file.resolve(strict=True)
        self.env_file = env_file.resolve(strict=True)
        self.cwd = cwd.resolve(strict=True)
        self.environment = dict(environment)

    def _compose_command(self, project: str, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.compose_file),
            "-p",
            project,
            *arguments,
        ]

    def _list(self, command: list[str], stage: str) -> tuple[str, ...]:
        result = run_checked(
            command,
            cwd=self.cwd,
            environment=self.environment,
            stage=stage,
            timeout_seconds=60,
        )
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def list_project_resources(self, project: str) -> DockerProjectInventory:
        label = f"label={COMPOSE_PROJECT_LABEL}={project}"
        return DockerProjectInventory(
            containers=self._list(
                ["docker", "ps", "-aq", "--filter", label],
                "DOCKER_LIST_CONTAINERS",
            ),
            networks=self._list(
                ["docker", "network", "ls", "-q", "--filter", label],
                "DOCKER_LIST_NETWORKS",
            ),
            volumes=self._list(
                ["docker", "volume", "ls", "-q", "--filter", label],
                "DOCKER_LIST_VOLUMES",
            ),
        )

    def labels_for(self, kind: str, resource_id: str) -> dict[str, str]:
        command = ["docker", "inspect", "--format", "{{json .Config.Labels}}"]
        if kind == "network":
            command = [
                "docker",
                "network",
                "inspect",
                "--format",
                "{{json .Labels}}",
            ]
        elif kind == "volume":
            command = [
                "docker",
                "volume",
                "inspect",
                "--format",
                "{{json .Labels}}",
            ]
        result = run_checked(
            [*command, resource_id],
            cwd=self.cwd,
            environment=self.environment,
            stage="DOCKER_INSPECT_OWNERSHIP",
            timeout_seconds=60,
        )
        labels = json.loads(result.stdout)
        if not isinstance(labels, dict):
            raise DeploymentGateError(
                "DOCKER_OWNERSHIP_UNPROVEN",
                stage="DOCKER_INSPECT_OWNERSHIP",
                indicator="labels_invalid",
            )
        return {str(key): str(value) for key, value in labels.items()}

    def up(self, project: str) -> None:
        run_checked(
            self._compose_command(project, "up", "-d", "--wait", "--wait-timeout", "600"),
            cwd=self.cwd,
            environment=self.environment,
            stage="DOCKER_COMPOSE_UP",
            timeout_seconds=900,
        )

    def down(self, project: str) -> None:
        run_checked(
            self._compose_command(project, "down", "-v", "--remove-orphans"),
            cwd=self.cwd,
            environment=self.environment,
            stage="DOCKER_COMPOSE_DOWN",
            timeout_seconds=300,
        )


def _require_project_name(project: str) -> None:
    if PROJECT_NAME.fullmatch(project) is None:
        raise DeploymentGateError(
            "DOCKER_PROJECT_ID_INVALID",
            stage="DOCKER_PROJECT_PREFLIGHT",
            indicator="project_name_invalid",
        )


def _verify_ownership(
    backend: object,
    inventory: DockerProjectInventory,
    project: str,
) -> None:
    for kind, identifiers in inventory.by_kind():
        for resource_id in identifiers:
            labels = backend.labels_for(kind, resource_id)
            if labels.get(COMPOSE_PROJECT_LABEL) != project:
                raise DeploymentGateError(
                    "DOCKER_OWNERSHIP_UNPROVEN",
                    stage="DOCKER_OWNERSHIP",
                    indicator=f"{kind}_label_mismatch",
                )


@contextmanager
def managed_compose_project(backend: object, project: str) -> Iterator[None]:
    _require_project_name(project)
    initial = backend.list_project_resources(project)
    if not initial.empty:
        raise DeploymentGateError(
            "DOCKER_PROJECT_NOT_EMPTY",
            stage="DOCKER_PROJECT_PREFLIGHT",
            indicator="preexisting_resources",
        )
    try:
        try:
            backend.up(project)
        except Exception:
            raise
        active = backend.list_project_resources(project)
        if active.empty:
            raise DeploymentGateError(
                "DOCKER_PROJECT_START_UNPROVEN",
                stage="DOCKER_COMPOSE_UP",
                indicator="no_owned_resources",
            )
        _verify_ownership(backend, active, project)
        yield
    except BaseException:
        raise
    finally:
        current = backend.list_project_resources(project)
        if not current.empty:
            try:
                _verify_ownership(backend, current, project)
            except DeploymentGateError:
                raise
            try:
                backend.down(project)
            except Exception:
                raise DeploymentGateError(
                    "DOCKER_CLEANUP_FAILED",
                    stage="DOCKER_COMPOSE_DOWN",
                    indicator="down_failed",
                ) from None
            remaining = backend.list_project_resources(project)
            if not remaining.empty:
                raise DeploymentGateError(
                    "DOCKER_CLEANUP_FAILED",
                    stage="DOCKER_COMPOSE_DOWN",
                    indicator="resources_remain",
                )
