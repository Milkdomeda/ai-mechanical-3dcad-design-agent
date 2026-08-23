from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast


DiagnosticStatus = Literal["ok", "warning", "setup_required", "blocked"]

STATUS_SEVERITY: Mapping[DiagnosticStatus, int] = MappingProxyType(
    {
        "ok": 0,
        "warning": 1,
        "setup_required": 2,
        "blocked": 3,
    }
)

CANONICAL_COMPONENTS = (
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

STATUS_PARTICIPANTS = (
    "workspace_selection",
    "workspace_manifest",
    "managed_config_integrity",
    "actor_identity",
    "artifact_root",
    "package_resources",
)

DOCTOR_PARTICIPANTS = CANONICAL_COMPONENTS

CAPABILITY_COMPONENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "bootstrap_init": (
            "workspace_selection",
            "managed_config_integrity",
        ),
        "config_inspection": (
            "workspace_selection",
            "workspace_manifest",
            "managed_config_integrity",
            "actor_identity",
            "package_resources",
        ),
        "postgres_migration": ("postgresql", "package_resources"),
        "database_bootstrap": (
            "workspace_selection",
            "workspace_manifest",
            "managed_config_integrity",
            "postgresql",
            "neo4j",
            "package_resources",
        ),
        "family_create_or_manage": (
            "workspace_selection",
            "workspace_manifest",
            "actor_identity",
            "postgresql",
        ),
        "design_job_workspace": (
            "workspace_selection",
            "workspace_manifest",
            "actor_identity",
            "postgresql",
            "product_family",
            "package_resources",
        ),
        "library_ingest": (
            "workspace_selection",
            "workspace_manifest",
            "actor_identity",
            "postgresql",
            "product_family",
            "freecadcmd",
            "artifact_root",
            "package_resources",
        ),
        "design_knowledge": (
            "workspace_selection",
            "workspace_manifest",
            "actor_identity",
            "postgresql",
            "neo4j",
        ),
        "cad_working_copy": (
            "workspace_selection",
            "workspace_manifest",
            "actor_identity",
            "artifact_root",
            "postgresql",
            "freecadcmd",
            "package_resources",
        ),
        "model_validation": (
            "workspace_selection",
            "workspace_manifest",
            "artifact_root",
            "freecadcmd",
            "package_resources",
        ),
        "artifact_registration": (
            "workspace_selection",
            "workspace_manifest",
            "actor_identity",
            "artifact_root",
            "postgresql",
        ),
        "projection": ("postgresql", "neo4j", "package_resources"),
        "standard_part_provider_list": ("package_resources",),
        "standard_part_config_inspection": (
            "workspace_selection",
            "workspace_manifest",
            "package_resources",
            "standard_part_sources",
        ),
        "standard_part_config_update": (
            "workspace_selection",
            "workspace_manifest",
            "managed_config_integrity",
            "package_resources",
            "standard_part_sources",
        ),
        "standard_part_catalog_write_or_reuse": (
            "workspace_selection",
            "workspace_manifest",
            "actor_identity",
            "postgresql",
            "standard_part_sources",
        ),
    }
)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ComponentDiagnostic:
    name: str
    status: DiagnosticStatus
    code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUS_SEVERITY:
            raise ValueError(f"unknown diagnostic status: {self.status}")
        if not self.code or not self.message:
            raise ValueError("diagnostic code and message must not be empty")
        frozen = _freeze(self.details)
        if not isinstance(frozen, Mapping):
            raise ValueError("diagnostic details must be a mapping")
        object.__setattr__(self, "details", frozen)

    def as_dict(self, *, affects_overall: bool) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "affects_overall": affects_overall,
            "code": self.code,
            "message": self.message,
            "details": _thaw(self.details),
        }


@dataclass(frozen=True)
class CapabilityRequest:
    capability: str
    additional_components: tuple[str, ...] = ()

    @property
    def participants(self) -> tuple[str, ...]:
        if self.capability not in CAPABILITY_COMPONENTS:
            raise ValueError(f"unknown capability: {self.capability}")
        base = CAPABILITY_COMPONENTS[self.capability]
        additions = self.additional_components
        if len(set(additions)) != len(additions):
            raise ValueError("conditional capability components must be unique")
        unknown = set(additions) - set(CANONICAL_COMPONENTS)
        if unknown:
            raise ValueError(f"unknown conditional components: {sorted(unknown)}")
        duplicates = set(additions) & set(base)
        if duplicates:
            raise ValueError(
                f"conditional components already participate: {sorted(duplicates)}"
            )
        return (*base, *additions)


def _validate_components(
    components: Mapping[str, ComponentDiagnostic],
) -> None:
    actual = set(components)
    expected = set(CANONICAL_COMPONENTS)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(
            f"diagnostic components must be canonical; missing={missing}, unknown={unknown}"
        )
    for name, diagnostic in components.items():
        if diagnostic.name != name:
            raise ValueError(
                f"diagnostic component key/name mismatch: {name}/{diagnostic.name}"
            )


def _validate_participants(participants: Sequence[str]) -> tuple[str, ...]:
    result = tuple(participants)
    if not result:
        raise ValueError("diagnostic participant set must not be empty")
    if len(set(result)) != len(result):
        raise ValueError("diagnostic participants must be unique")
    unknown = set(result) - set(CANONICAL_COMPONENTS)
    if unknown:
        raise ValueError(f"unknown diagnostic participants: {sorted(unknown)}")
    return result


def _next_steps(
    components: Mapping[str, ComponentDiagnostic],
    participants: tuple[str, ...],
) -> list[str]:
    steps: list[str] = []
    for name in CANONICAL_COMPONENTS:
        if name not in participants:
            continue
        value = components[name].details.get("next_steps", ())
        if not isinstance(value, tuple):
            continue
        for step in value:
            if isinstance(step, str) and step not in steps:
                steps.append(step)
    return steps


def build_diagnostic_report(
    *,
    kind: str,
    components: Mapping[str, ComponentDiagnostic],
    participants: Sequence[str],
    capability: str | None = None,
) -> dict[str, object]:
    if kind not in {"status", "doctor", "capability"}:
        raise ValueError(f"unknown diagnostic kind: {kind}")
    if (kind == "capability") != (capability is not None):
        raise ValueError("capability is required only for capability diagnostics")
    _validate_components(components)
    participating = _validate_participants(participants)
    overall = max(
        (components[name].status for name in participating),
        key=lambda status: STATUS_SEVERITY[status],
    )
    return {
        "schema_version": "MechanicalDesignDiagnostics/v1",
        "kind": kind,
        "capability": capability,
        "status": {"overall": overall},
        "components": [
            components[name].as_dict(affects_overall=name in participating)
            for name in CANONICAL_COMPONENTS
        ],
        "next_steps": _next_steps(components, participating),
    }


def exit_code_for_status(status: DiagnosticStatus | str) -> int:
    if status not in STATUS_SEVERITY:
        raise ValueError(f"unknown diagnostic status: {status}")
    return STATUS_SEVERITY[cast(DiagnosticStatus, status)]


def guard_response(
    report: Mapping[str, object],
    *,
    capability: str,
) -> dict[str, object]:
    status_value = report.get("status")
    if not isinstance(status_value, Mapping):
        raise ValueError("diagnostic report status must be a mapping")
    overall = status_value.get("overall")
    if overall not in {"setup_required", "blocked"}:
        raise ValueError("only setup-required or blocked reports can guard an operation")
    components = report.get("components")
    if not isinstance(components, list):
        raise ValueError("diagnostic report components must be a list")
    highest = next(
        (
            item
            for item in components
            if isinstance(item, Mapping)
            and item.get("affects_overall") is True
            and item.get("status") == overall
        ),
        None,
    )
    if highest is None:
        raise ValueError("diagnostic report has no highest-severity participant")
    next_steps = report.get("next_steps", [])
    if not isinstance(next_steps, list):
        raise ValueError("diagnostic report next_steps must be a list")
    return {
        "schema_version": "MechanicalDesignSetupResponse/v1",
        "status": overall,
        "code": highest.get("code"),
        "message": highest.get("message"),
        "capability": capability,
        "next_steps": list(next_steps),
        "diagnostics": dict(report),
    }


class DiagnosticGateError(RuntimeError):
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.status = str(response.get("status", "blocked"))
        self.code = str(response.get("code", "DIAGNOSTIC_GUARD_FAILED"))
        super().__init__(f"{self.code}: {response.get('message', 'operation blocked')}")
