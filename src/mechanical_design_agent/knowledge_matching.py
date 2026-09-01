from __future__ import annotations

from collections.abc import Mapping
import unicodedata


def normalize_search_term(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("search term must be a string")
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _feature_values(value: object) -> list[str]:
    if isinstance(value, str):
        normalized = normalize_search_term(value)
        return [normalized] if normalized else []
    if isinstance(value, Mapping):
        values: list[str] = []
        for key in sorted(value, key=str):
            values.extend(_feature_values(value[key]))
        return values
    if isinstance(value, (list, tuple, set, frozenset)):
        values = []
        for item in value:
            values.extend(_feature_values(item))
        return values
    if isinstance(value, (bool, int, float)):
        return [normalize_search_term(str(value))]
    return []


def collect_design_terms(
    query: str, features: Mapping[str, object]
) -> tuple[str, ...]:
    if not isinstance(features, Mapping):
        raise ValueError("design features must be an object")
    terms = set(_feature_values(features))
    normalized_query = normalize_search_term(query)
    if normalized_query:
        terms.add(normalized_query)
    return tuple(sorted(terms))


def applicability_matches(
    applicability: Mapping[str, object], features: Mapping[str, object]
) -> bool:
    if not isinstance(applicability, Mapping):
        raise ValueError("applicability must be an object")
    if not isinstance(features, Mapping):
        raise ValueError("design features must be an object")
    conditions = applicability.get("conditions", {})
    if not isinstance(conditions, Mapping):
        raise ValueError("applicability.conditions must be an object")
    for key, expected in conditions.items():
        if key not in features:
            return False
        actual = features[key]
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


__all__ = [
    "applicability_matches",
    "collect_design_terms",
    "normalize_search_term",
]
