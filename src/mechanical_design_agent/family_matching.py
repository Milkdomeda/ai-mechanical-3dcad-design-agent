from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


_APPROVED_PRODUCT_STATUSES = frozenset({"approved", "confirmed", "active"})


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def _contains_phrase(query: str, phrase: object) -> bool:
    normalized = _normalize(phrase)
    if not normalized:
        return False
    return f" {normalized} " in f" {query} "


def _family_id(family: Mapping[str, Any]) -> str:
    return str(family.get("family_id") or family.get("id") or "").strip()


def _candidate(
    family_id: str,
    match_kind: str,
    evidence: Iterable[str],
) -> dict[str, Any]:
    return {
        "family_id": family_id,
        "match_kind": match_kind,
        "evidence": list(evidence),
    }


def _result(
    status: str,
    *,
    binding_family_id: str | None,
    candidates: list[dict[str, Any]],
    specialized_knowledge_authorized: bool,
    next_action: str,
) -> dict[str, Any]:
    return {
        "schema_version": "ProductFamilyMatch/v1",
        "status": status,
        "binding_family_id": binding_family_id,
        "candidates": candidates,
        "specialized_knowledge_authorized": specialized_knowledge_authorized,
        "next_action": next_action,
    }


def _exact_candidates(
    normalized_query: str,
    inventory: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for family_id, family in inventory.items():
        canonical_name = str(family.get("canonical_name") or "").strip()
        aliases = [
            str(value).strip()
            for value in family.get("aliases", [])
            if str(value).strip()
        ]
        if _contains_phrase(normalized_query, family_id):
            candidates.append(
                _candidate(
                    family_id,
                    "explicit_family_id",
                    ["request contains the exact family ID"],
                )
            )
            continue
        if canonical_name and _contains_phrase(normalized_query, canonical_name):
            candidates.append(
                _candidate(
                    family_id,
                    "canonical_name",
                    [f"request contains canonical family name: {canonical_name}"],
                )
            )
            continue
        matching_aliases = [
            alias for alias in aliases if _contains_phrase(normalized_query, alias)
        ]
        if matching_aliases:
            candidates.append(
                _candidate(
                    family_id,
                    "approved_alias",
                    [f"request contains approved family alias: {matching_aliases[0]}"],
                )
            )
            continue

        product_matches: list[str] = []
        for product in family.get("products", []):
            if not isinstance(product, Mapping):
                continue
            if str(product.get("status") or "").casefold() not in _APPROVED_PRODUCT_STATUSES:
                continue
            identifiers = [product.get("canonical_name"), *product.get("aliases", [])]
            product_matches.extend(
                str(value).strip()
                for value in identifiers
                if str(value or "").strip()
                and _contains_phrase(normalized_query, value)
            )
        if product_matches:
            candidates.append(
                _candidate(
                    family_id,
                    "approved_product_identifier",
                    [f"request contains approved product identifier: {product_matches[0]}"],
                )
            )
    return sorted(candidates, key=lambda item: item["family_id"])


def match_product_family(
    *,
    query: str,
    families: Iterable[Mapping[str, Any]],
    bound_family_id: str | None = None,
    source_family_id: str | None = None,
    explicit_family_id: str | None = None,
    design_features: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an auditable family match without authorizing semantic guesses."""

    inventory = {
        family_id: dict(family)
        for family in families
        if (family_id := _family_id(family))
    }
    bound = str(bound_family_id or "").strip() or None
    source = str(source_family_id or "").strip() or None
    explicit = str(explicit_family_id or "").strip() or None
    normalized_query = _normalize(query)
    exact = _exact_candidates(normalized_query, inventory)

    authoritative_ids = {value for value in (bound, source, explicit) if value}
    if len(authoritative_ids) > 1:
        candidates = [
            _candidate(
                value,
                (
                    "existing_job_binding"
                    if value == bound
                    else "source_model_binding"
                    if value == source
                    else "explicit_family_id"
                ),
                [
                    "active Design Job is already bound to this family"
                    if value == bound
                    else "source model has an approved family binding"
                    if value == source
                    else "user supplied this exact family ID"
                ],
            )
            for value in sorted(authoritative_ids)
            if value in inventory
        ]
        return _result(
            "conflict",
            binding_family_id=bound,
            candidates=candidates,
            specialized_knowledge_authorized=False,
            next_action="ask_user",
        )

    if authoritative_ids:
        family_id = next(iter(authoritative_ids))
        if family_id not in inventory:
            return _result(
                "conflict",
                binding_family_id=bound,
                candidates=[],
                specialized_knowledge_authorized=False,
                next_action="ask_user",
            )
        conflicting_exact = [
            candidate
            for candidate in exact
            if str(candidate["family_id"]) != family_id
        ]
        if conflicting_exact:
            relationship_kind = (
                "existing_job_binding"
                if bound
                else "source_model_binding"
                if source
                else "explicit_family_id"
            )
            relationship_evidence = (
                "active Design Job is already bound to this family"
                if bound
                else "source model has an approved family binding"
                if source
                else "user supplied this exact family ID"
            )
            return _result(
                "conflict",
                binding_family_id=bound,
                candidates=[
                    _candidate(family_id, relationship_kind, [relationship_evidence]),
                    *conflicting_exact,
                ],
                specialized_knowledge_authorized=False,
                next_action="ask_user",
            )
        if bound:
            kind = "existing_job_binding"
            evidence = ["active Design Job is already bound to this family"]
        elif source:
            kind = "source_model_binding"
            evidence = ["source model has an approved family binding"]
        else:
            kind = "explicit_family_id"
            evidence = ["user supplied this exact family ID"]
        return _result(
            "authoritative_match",
            binding_family_id=family_id,
            candidates=[_candidate(family_id, kind, evidence)],
            specialized_knowledge_authorized=True,
            next_action="bind_family",
        )

    feature_values: list[str] = []
    for value in (design_features or {}).values():
        if isinstance(value, str):
            feature_values.append(value)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            feature_values.extend(str(item) for item in value if isinstance(item, str))
    semantic_text = _normalize(" ".join([query, *feature_values]))
    semantic: list[dict[str, Any]] = []
    for family_id, family in inventory.items():
        if any(str(candidate["family_id"]) == family_id for candidate in exact):
            continue

        descriptor_matches = [
            str(descriptor).strip()
            for descriptor in family.get("discovery_descriptors", [])
            if str(descriptor).strip()
            and _contains_phrase(semantic_text, descriptor)
        ]
        if descriptor_matches:
            semantic.append(
                _candidate(
                    family_id,
                    "semantic_candidate",
                    [f"discovery descriptor matches request: {value}" for value in descriptor_matches],
                )
            )

    if len(exact) == 1:
        family_id = str(exact[0]["family_id"])
        return _result(
            "authoritative_match",
            binding_family_id=family_id,
            candidates=exact,
            specialized_knowledge_authorized=True,
            next_action="bind_family",
        )
    if len(exact) > 1:
        return _result(
            "conflict",
            binding_family_id=None,
            candidates=exact,
            specialized_knowledge_authorized=False,
            next_action="ask_user",
        )

    semantic.sort(key=lambda item: (-len(item["evidence"]), item["family_id"]))
    if semantic:
        return _result(
            "confirmation_required",
            binding_family_id=None,
            candidates=semantic,
            specialized_knowledge_authorized=False,
            next_action="ask_user",
        )
    return _result(
        "unbound_no_match",
        binding_family_id=None,
        candidates=[],
        specialized_knowledge_authorized=False,
        next_action="continue_unbound",
    )
