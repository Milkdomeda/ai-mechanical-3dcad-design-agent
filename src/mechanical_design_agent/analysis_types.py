"""Shared JSON boundary and stable, serializable analysis errors."""
from __future__ import annotations

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type JsonObject = dict[str, Json]


class AnalysisError(ValueError):
    def __init__(self, code: str, message: str, *, details: JsonObject | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> JsonObject:
        return {"status": "blocked", "code": self.code, "message": str(self), "details": self.details}
