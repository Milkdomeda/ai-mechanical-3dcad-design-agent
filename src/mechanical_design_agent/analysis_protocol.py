"""Strict package-owned schemas and canonical, complete semantic hashes.

No schema URI or reference can fetch a resource from a network or local file.
Native FreeCAD workers do not import this module or the host environment.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import math

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from .analysis_types import AnalysisError, JsonObject
from .analysis_units import canonical_decimal, validate_normalized_quantity

_NAMES = frozenset({"prepare-request", "spec", "session", "run", "region", "result", "gui-request", "gui-receipt", "manifest", "completion", "configuration", "geometry-request", "geometry", "region-selectors", "region-bindings", "load-ledger"})
_PREFIX = "urn:mechanical-design:analysis:"
_MAX_BYTES = 4 * 1024 * 1024


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisError("ANALYSIS_JSON_DUPLICATE", "duplicate JSON key", details={"key": key})
        result[key] = value
    return result


def _constant(value):
    raise AnalysisError("ANALYSIS_JSON_NONFINITE", "non-finite JSON values are forbidden")


def _check_json(value, depth=0):
    if depth > 64:
        raise AnalysisError("ANALYSIS_JSON_LIMIT", "record nesting is too deep")
    kind = type(value)
    if kind is dict:
        if any(type(key) is not str for key in value):
            raise AnalysisError("ANALYSIS_JSON_INVALID", "JSON keys must be strings")
        for child in value.values():
            _check_json(child, depth + 1)
    elif kind is list:
        for child in value:
            _check_json(child, depth + 1)
    elif kind is float:
        if not math.isfinite(value):
            _constant(value)
    elif kind is int:
        if value.bit_length() > 512:
            raise AnalysisError("ANALYSIS_JSON_LIMIT", "JSON integer is too large")
    elif kind not in (str, bool, type(None)):
        raise AnalysisError("ANALYSIS_JSON_INVALID", "record contains a non-JSON value")


def _deny_retrieval(uri):
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=1)
def _schemas():
    root = files("mechanical_design_agent").joinpath("resources", "analysis", "schemas")
    schemas = {}
    for name in sorted(_NAMES | {"common"}):
        schema = json.loads(root.joinpath(name + ".schema.json").read_text(encoding="utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if schema.get("$id") != _PREFIX + name + ":v1":
            raise AnalysisError("ANALYSIS_SCHEMA_INVALID", "package schema identity mismatch")
        Draft202012Validator.check_schema(schema)
        schemas[name] = schema
    identities = {schema["$id"] for schema in schemas.values()}
    def check_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref" and (not isinstance(child, str) or (not child.startswith("#") and child.partition("#")[0] not in identities)):
                    raise AnalysisError("ANALYSIS_SCHEMA_REFERENCE_BLOCKED", "only registered package schema references are allowed")
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)
    for schema in schemas.values():
        check_refs(schema)
    registry = Registry(retrieve=_deny_retrieval).with_resources((schema["$id"], Resource.from_contents(schema)) for schema in schemas.values())
    return schemas, registry


def schema_inventory() -> JsonObject:
    schemas, _ = _schemas()
    return {name: deepcopy(schemas[name]) for name in sorted(_NAMES)}


def _validate(record, schema_name):
    if schema_name not in _NAMES:
        raise AnalysisError("ANALYSIS_SCHEMA_UNKNOWN", "select a registered analysis schema")
    _check_json(record)
    if type(record) is not dict:
        raise AnalysisError("ANALYSIS_JSON_INVALID", "analysis record must be a JSON object")
    schemas, registry = _schemas()
    validator = Draft202012Validator(schemas[schema_name], registry=registry, format_checker=FormatChecker())
    error = next(validator.iter_errors(record), None)
    if error is not None:
        raise AnalysisError("ANALYSIS_SCHEMA_INVALID", "analysis record does not match its versioned schema", details={"path": "/".join(map(str, error.absolute_path)), "rule": str(error.validator)})
    _check_quantities(record)


def _check_quantities(value):
    if isinstance(value, dict):
        is_quantity = "value" in value and "unit" in value
        if is_quantity:
            validate_normalized_quantity(value)
        for key, child in value.items():
            if not (is_quantity and key == "input"):
                _check_quantities(child)
    elif isinstance(value, list):
        for child in value:
            _check_quantities(child)


def parse_record(text: str, *, schema_name: str) -> JsonObject:
    if schema_name not in _NAMES:
        raise AnalysisError("ANALYSIS_SCHEMA_UNKNOWN", "select a registered analysis schema")
    if type(text) is not str or len(text.encode("utf-8")) > _MAX_BYTES:
        raise AnalysisError("ANALYSIS_JSON_LIMIT", "analysis JSON exceeds the supported size")
    try:
        record = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, AnalysisError):
            raise
        raise AnalysisError("ANALYSIS_JSON_INVALID", "invalid analysis JSON") from exc
    _validate(record, schema_name)
    return record


def _normalize(value):
    if isinstance(value, dict):
        result = {key: _normalize(child) for key, child in value.items()}
        if "value" in result and "unit" in result:
            result["value"] = canonical_decimal(result["value"])
        return result
    if isinstance(value, list):
        return [_normalize(child) for child in value]
    return value


def canonical_record(record: JsonObject, *, schema_name: str) -> bytes:
    _validate(record, schema_name)
    normalized = _normalize(record)
    _validate(normalized, schema_name)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_BYTES:
        raise AnalysisError("ANALYSIS_JSON_LIMIT", "analysis JSON exceeds the supported size")
    return encoded


def record_sha256(record: JsonObject, *, schema_name: str) -> str:
    return hashlib.sha256(canonical_record(record, schema_name=schema_name)).hexdigest()
