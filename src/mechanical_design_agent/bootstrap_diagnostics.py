from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


DiagnosticStatus = Literal["ok", "warning", "setup_required", "blocked"]


@dataclass(frozen=True)
class ComponentStatus:
    name: str
    status: DiagnosticStatus
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }


class DiagnosticGateError(RuntimeError):
    def __init__(self, response: dict[str, object]) -> None:
        super().__init__(str(response.get("message", "runtime is not ready")))
        self.response = response


def blocked_response(
    *, capability: str, code: str, message: str, diagnostics: dict[str, object]
) -> dict[str, object]:
    return {
        "schema_version": "MechanicalDesignSetupResponse/v1",
        "status": "setup_required",
        "code": code,
        "message": message,
        "capability": capability,
        "diagnostics": diagnostics,
    }


__all__ = [
    "ComponentStatus",
    "DiagnosticGateError",
    "DiagnosticStatus",
    "blocked_response",
]
