import pytest

from mechanical_design_agent.knowledge_matching import (
    applicability_matches,
    collect_design_terms,
)


def test_collect_design_terms_is_stable_and_deduplicated() -> None:
    assert collect_design_terms(
        "Printed carrier", {"design_type": "carrier", "material": "PETG"}
    ) == ("carrier", "petg", "printed carrier")


def test_collect_design_terms_normalizes_unicode_and_nested_feature_values() -> None:
    assert collect_design_terms(
        " ＰＥＴＧ  Carrier ",
        {"materials": ["PETG", " ABS "], "details": {"kind": "Cradle"}},
    ) == ("abs", "cradle", "petg", "petg carrier")


def test_applicability_requires_declared_feature_values() -> None:
    applicability = {
        "conditions": {"design_type": "carrier", "material": ["PETG", "ABS"]}
    }
    assert applicability_matches(
        applicability, {"design_type": "carrier", "material": "PETG"}
    )
    assert not applicability_matches(
        applicability, {"design_type": "shaft", "material": "PETG"}
    )


def test_descriptive_applicability_is_not_treated_as_a_hidden_filter() -> None:
    assert applicability_matches(
        {"summary": "printed carriers"}, {"design_type": "carrier"}
    )


def test_invalid_conditions_structure_fails_closed() -> None:
    with pytest.raises(ValueError, match="applicability.conditions"):
        applicability_matches({"conditions": "carrier"}, {"design_type": "carrier"})
