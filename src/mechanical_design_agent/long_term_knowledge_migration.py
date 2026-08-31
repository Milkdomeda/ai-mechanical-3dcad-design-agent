from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal, Mapping, Sequence

from .models import canonical_json, require_safe_id


ALLOWED_SOURCE_KEYS = frozenset(
    {
        "organizations",
        "design_groups",
        "product_families",
        "family_profiles",
        "knowledge_assertions",
        "knowledge_search_documents",
        "design_lesson_events",
    }
)


def _copy(value: object, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc


def _rows(
    source: Mapping[str, Sequence[Mapping[str, object]]], key: str
) -> list[dict[str, Any]]:
    value = source.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"source collection {key} must be a list")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise ValueError(f"source collection {key} row {index} must be an object")
        rows.append(_copy(dict(row), f"source collection {key} row {index}"))
    return rows


def _required_text(row: Mapping[str, object], key: str, label: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} {key} is required")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _textual(value: object, label: str) -> str:
    if isinstance(value, str):
        if value.strip():
            return value.strip()
        raise ValueError(f"{label} must not be blank")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        text = "; ".join(item.strip() for item in value if item.strip())
        if text:
            return text
    copied = _copy(value, label)
    if copied in (None, [], {}):
        raise ValueError(f"{label} must not be empty")
    return canonical_json(copied)


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    return sorted({item.strip() for item in value if item.strip()}, key=str.casefold)


def _safe_lesson_id(source_key: str) -> str:
    candidate = f"lesson-{source_key}"
    try:
        require_safe_id(candidate, "design lesson id")
        return candidate
    except ValueError:
        digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:24]
        return f"lesson-import-{digest}"


@dataclass(frozen=True)
class RetrievalProbe:
    query: str
    kind: Literal["product_family", "knowledge_assertion", "design_lesson"]
    expected_id: str
    organization_id: str
    design_group_id: str
    product_family_id: str | None = None


@dataclass(frozen=True)
class LongTermKnowledgeExport:
    organizations: tuple[dict[str, object], ...]
    design_groups: tuple[dict[str, object], ...]
    product_families: tuple[dict[str, object], ...]
    knowledge_assertions: tuple[dict[str, object], ...]
    design_lesson_reviews: tuple[dict[str, object], ...]
    design_lessons: tuple[dict[str, object], ...]
    source_counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "LongTermKnowledgeExport/v1",
            "organizations": _copy(list(self.organizations), "organizations"),
            "design_groups": _copy(list(self.design_groups), "design groups"),
            "product_families": _copy(
                list(self.product_families), "product families"
            ),
            "knowledge_assertions": _copy(
                list(self.knowledge_assertions), "knowledge assertions"
            ),
            "design_lesson_reviews": _copy(
                list(self.design_lesson_reviews), "design lesson reviews"
            ),
            "design_lessons": _copy(list(self.design_lessons), "design lessons"),
            "source_counts": dict(self.source_counts),
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            canonical_json(self.as_dict()).encode("utf-8")
        ).hexdigest()


def build_long_term_export(
    source: Mapping[str, Sequence[Mapping[str, object]]],
) -> LongTermKnowledgeExport:
    if not isinstance(source, Mapping):
        raise ValueError("source must be an object of row collections")
    unexpected = sorted(set(source) - ALLOWED_SOURCE_KEYS)
    missing = sorted(ALLOWED_SOURCE_KEYS - set(source))
    if unexpected:
        raise ValueError("unexpected source collections: " + ", ".join(unexpected))
    if missing:
        raise ValueError("missing source collections: " + ", ".join(missing))

    source_rows = {key: _rows(source, key) for key in sorted(ALLOWED_SOURCE_KEYS)}
    source_families = source_rows["product_families"]
    family_ids = {
        _required_text(row, "id", "product family") for row in source_families
    }
    if len(family_ids) != len(source_families):
        raise ValueError("product family IDs must be unique")

    approved_profiles: dict[str, dict[str, Any]] = {}
    for row in source_rows["family_profiles"]:
        if row.get("status") != "approved":
            continue
        family_id = _required_text(row, "family_id", "family profile")
        if family_id not in family_ids:
            raise ValueError(f"family profile references missing family {family_id}")
        revision = row.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError("approved family profile revision must be an integer")
        existing = approved_profiles.get(family_id)
        if existing is None or revision > int(existing["revision"]):
            approved_profiles[family_id] = row

    search_by_assertion: dict[str, dict[str, object]] = {}
    retrieval_by_family: dict[str, set[str]] = {
        family_id: set() for family_id in family_ids
    }
    for row in source_rows["knowledge_search_documents"]:
        assertion_id = _required_text(row, "assertion_id", "search document")
        if assertion_id in search_by_assertion:
            raise ValueError(f"duplicate search document for assertion {assertion_id}")
        terms = _string_list(row.get("exact_terms"), "search document exact_terms")
        search_text = _optional_text(row.get("search_text"))
        if search_text:
            terms = sorted({*terms, search_text}, key=str.casefold)
        family_id = _optional_text(row.get("family_id"))
        if family_id:
            if family_id not in family_ids:
                raise ValueError(f"search document references missing family {family_id}")
            retrieval_by_family[family_id].update(terms)
        search_by_assertion[assertion_id] = {
            "search_terms": terms,
            "search_text": search_text,
        }

    assertions: list[dict[str, object]] = []
    for row in source_rows["knowledge_assertions"]:
        if row.get("status") != "approved":
            continue
        assertion_id = _required_text(row, "id", "knowledge assertion")
        family_id = _optional_text(row.get("family_id"))
        if family_id and family_id not in family_ids:
            raise ValueError(
                f"knowledge assertion {assertion_id} references missing family {family_id}"
            )
        search = search_by_assertion.get(assertion_id, {})
        assertions.append(
            {
                "id": assertion_id,
                "organization_id": _required_text(
                    row, "organization_id", "knowledge assertion"
                ),
                "design_group_id": _required_text(
                    row, "design_group_id", "knowledge assertion"
                ),
                "product_family_id": family_id,
                "subject": _required_text(row, "subject_ref", "knowledge assertion"),
                "predicate": _required_text(row, "predicate", "knowledge assertion"),
                "object_value": _copy(
                    row.get("object_value"), "knowledge assertion object_value"
                ),
                "applicability": {
                    "conditions": _copy(
                        row.get("applicability") or {},
                        "knowledge assertion applicability",
                    ),
                    "non_applicable_conditions": _copy(
                        row.get("non_applicable_conditions") or [],
                        "knowledge assertion non-applicable conditions",
                    ),
                },
                "evidence": _copy(
                    row.get("evidence") or [], "knowledge assertion evidence"
                ),
                "authorization": {
                    "source_kind": _optional_text(row.get("source_kind")),
                    "risk_level": _optional_text(row.get("risk_level")),
                    "confidence": row.get("confidence"),
                    "created_by": _optional_text(row.get("created_by")),
                },
                "search_terms": list(search.get("search_terms", [])),
                "status": "approved",
                "supersedes_id": _optional_text(row.get("supersedes")),
                "created_at": _optional_text(row.get("created_at")),
            }
        )
    assertions.sort(key=lambda item: str(item["id"]))

    families: list[dict[str, object]] = []
    for row in source_families:
        family_id = _required_text(row, "id", "product family")
        aliases = set(_string_list(row.get("aliases"), "product family aliases"))
        config = row.get("config")
        if isinstance(config, Mapping):
            aliases.update(_string_list(config.get("aliases"), "family config aliases"))
        profile = approved_profiles.get(family_id)
        knowledge: dict[str, object] = {
            "retrieval_terms": sorted(retrieval_by_family[family_id], key=str.casefold)
        }
        if profile is not None:
            knowledge["approved_profile"] = _copy(
                profile.get("profile") or {}, "approved family profile"
            )
            knowledge["approved_profile_evidence"] = _copy(
                profile.get("evidence") or [], "approved family profile evidence"
            )
        families.append(
            {
                "id": family_id,
                "organization_id": _required_text(
                    row, "organization_id", "product family"
                ),
                "design_group_id": _required_text(
                    row, "design_group_id", "product family"
                ),
                "canonical_name": _required_text(
                    row, "canonical_name", "product family"
                ),
                "aliases": sorted(aliases, key=str.casefold),
                "knowledge": knowledge,
                "status": "active",
            }
        )
    families.sort(key=lambda item: str(item["id"]))

    approved_events: dict[str, dict[str, Any]] = {}
    for row in source_rows["design_lesson_events"]:
        if row.get("status") != "approved":
            continue
        lesson_key = _required_text(row, "lesson_key", "design lesson")
        revision = row.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError("approved design lesson revision must be an integer")
        existing = approved_events.get(lesson_key)
        if existing is None or revision > int(existing["revision"]):
            approved_events[lesson_key] = row

    lesson_id_by_key = {
        lesson_key: _safe_lesson_id(lesson_key) for lesson_key in approved_events
    }
    event_id_to_lesson_id = {
        _required_text(row, "id", "design lesson"): lesson_id_by_key[key]
        for key, row in approved_events.items()
    }
    lesson_reviews: list[dict[str, object]] = []
    lessons: list[dict[str, object]] = []
    for lesson_key in sorted(approved_events):
        row = approved_events[lesson_key]
        family_id = _optional_text(row.get("source_family_id"))
        if family_id and family_id not in family_ids:
            raise ValueError(f"design lesson references missing family {family_id}")
        search_terms = _string_list(row.get("search_terms"), "lesson search_terms")
        lesson = {
            "title": _required_text(row, "title", "design lesson"),
            "problem": _required_text(row, "problem", "design lesson"),
            "root_causes": _copy(
                row.get("root_causes") or [], "design lesson root causes"
            ),
            "decision": _textual(row.get("corrections"), "design lesson corrections"),
            "prevention_action": _textual(
                row.get("prevention"), "design lesson prevention"
            ),
            "applicability": _copy(
                row.get("applicability") or {}, "design lesson applicability"
            ),
            "non_applicable_conditions": _copy(
                row.get("non_applicable_conditions") or [],
                "design lesson non-applicable conditions",
            ),
            "evidence": _copy(
                row.get("evidence_manifest") or [], "design lesson evidence"
            ),
            "search_terms": search_terms,
            "scope": "product_family" if family_id else "design_group",
            "product_family_id": family_id,
            "source_lesson_key": lesson_key,
        }
        review_card = {
            "schema_version": "DesignLessonReviewCard/v1",
            "review_id": f"import-{lesson_id_by_key[lesson_key]}",
            "source_kind": "long_term_knowledge_export",
            "lessons": [lesson],
        }
        review_sha256 = hashlib.sha256(
            canonical_json(review_card).encode("utf-8")
        ).hexdigest()
        supersedes = _optional_text(row.get("supersedes"))
        supersedes_id = None
        if supersedes:
            supersedes_id = lesson_id_by_key.get(
                supersedes, event_id_to_lesson_id.get(supersedes)
            )
            if supersedes_id is None:
                raise ValueError(
                    f"design lesson {lesson_key} supersedes unknown lesson {supersedes}"
                )
        organization_id = _required_text(row, "organization_id", "design lesson")
        design_group_id = _required_text(
            row, "source_design_group_id", "design lesson"
        )
        lesson_reviews.append(
            {
                "review_sha256": review_sha256,
                "organization_id": organization_id,
                "design_group_id": design_group_id,
                "product_family_id": family_id,
                "review_card": review_card,
                "decision": "approved",
                "decision_text": _required_text(
                    row, "approval_text", "design lesson"
                ),
                "decided_at": _optional_text(row.get("approved_at")),
            }
        )
        lessons.append(
            {
                "id": lesson_id_by_key[lesson_key],
                "review_sha256": review_sha256,
                "organization_id": organization_id,
                "design_group_id": design_group_id,
                "product_family_id": family_id,
                "lesson": lesson,
                "search_terms": search_terms,
                "applicability": _copy(
                    row.get("applicability") or {}, "design lesson applicability"
                ),
                "status": "approved",
                "supersedes_id": supersedes_id,
                "created_at": _optional_text(row.get("approved_at")),
            }
        )
    lesson_reviews.sort(key=lambda item: str(item["review_sha256"]))
    lessons.sort(key=lambda item: str(item["id"]))

    organization_ids = {
        str(row["organization_id"]) for row in families + assertions + lessons
    }
    design_group_keys = {
        (str(row["design_group_id"]), str(row["organization_id"]))
        for row in families + assertions + lessons
    }
    organizations = [
        {
            "id": _required_text(row, "id", "organization"),
            "name": _required_text(row, "name", "organization"),
        }
        for row in source_rows["organizations"]
        if _required_text(row, "id", "organization") in organization_ids
    ]
    if {str(row["id"]) for row in organizations} != organization_ids:
        missing_orgs = sorted(organization_ids - {str(row["id"]) for row in organizations})
        raise ValueError("missing organizations: " + ", ".join(missing_orgs))
    organizations.sort(key=lambda item: str(item["id"]))

    design_groups = [
        {
            "id": _required_text(row, "id", "design group"),
            "organization_id": _required_text(
                row, "organization_id", "design group"
            ),
            "name": _required_text(row, "name", "design group"),
        }
        for row in source_rows["design_groups"]
        if (
            _required_text(row, "id", "design group"),
            _required_text(row, "organization_id", "design group"),
        )
        in design_group_keys
    ]
    observed_group_keys = {
        (str(row["id"]), str(row["organization_id"])) for row in design_groups
    }
    if observed_group_keys != design_group_keys:
        missing_groups = sorted(design_group_keys - observed_group_keys)
        raise ValueError(f"missing design groups: {missing_groups}")
    design_groups.sort(key=lambda item: (str(item["organization_id"]), str(item["id"])))

    return LongTermKnowledgeExport(
        organizations=tuple(organizations),
        design_groups=tuple(design_groups),
        product_families=tuple(families),
        knowledge_assertions=tuple(assertions),
        design_lesson_reviews=tuple(lesson_reviews),
        design_lessons=tuple(lessons),
        source_counts={
            "organizations": len(organizations),
            "design_groups": len(design_groups),
            "product_families": len(families),
            "knowledge_assertions": len(assertions),
            "design_lesson_reviews": len(lesson_reviews),
            "design_lessons": len(lessons),
        },
    )


def build_parity_probes(
    export: LongTermKnowledgeExport,
) -> tuple[RetrievalProbe, ...]:
    probes: set[RetrievalProbe] = set()
    for family in export.product_families:
        family_id = str(family["id"])
        organization_id = str(family["organization_id"])
        design_group_id = str(family["design_group_id"])
        terms = {
            str(family["canonical_name"]),
            *[str(value) for value in family.get("aliases", [])],
            *[
                str(value)
                for value in dict(family.get("knowledge", {})).get(
                    "retrieval_terms", []
                )
            ],
        }
        for term in terms:
            if term.strip():
                probes.add(
                    RetrievalProbe(
                        query=term.strip(),
                        kind="product_family",
                        expected_id=family_id,
                        organization_id=organization_id,
                        design_group_id=design_group_id,
                    )
                )
    for assertion in export.knowledge_assertions:
        for term in assertion.get("search_terms", []):
            if str(term).strip():
                probes.add(
                    RetrievalProbe(
                        query=str(term).strip(),
                        kind="knowledge_assertion",
                        expected_id=str(assertion["id"]),
                        organization_id=str(assertion["organization_id"]),
                        design_group_id=str(assertion["design_group_id"]),
                        product_family_id=_optional_text(
                            assertion.get("product_family_id")
                        ),
                    )
                )
    for lesson in export.design_lessons:
        for term in lesson.get("search_terms", []):
            if str(term).strip():
                probes.add(
                    RetrievalProbe(
                        query=str(term).strip(),
                        kind="design_lesson",
                        expected_id=str(lesson["id"]),
                        organization_id=str(lesson["organization_id"]),
                        design_group_id=str(lesson["design_group_id"]),
                        product_family_id=_optional_text(
                            lesson.get("product_family_id")
                        ),
                    )
                )
    return tuple(
        sorted(
            probes,
            key=lambda item: (
                item.organization_id,
                item.design_group_id,
                item.kind,
                item.expected_id,
                item.query.casefold(),
            ),
        )
    )


__all__ = [
    "ALLOWED_SOURCE_KEYS",
    "LongTermKnowledgeExport",
    "RetrievalProbe",
    "build_long_term_export",
    "build_parity_probes",
]
