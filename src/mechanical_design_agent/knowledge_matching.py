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


__all__ = ["collect_design_terms", "normalize_search_term"]
