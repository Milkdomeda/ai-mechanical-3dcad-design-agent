from mechanical_design_agent.knowledge_matching import collect_design_terms


def test_collect_design_terms_is_stable_and_deduplicated() -> None:
    assert collect_design_terms(
        "Printed carrier", {"design_type": "carrier", "material": "PETG"}
    ) == ("carrier", "petg", "printed carrier")


def test_collect_design_terms_normalizes_unicode_and_nested_feature_values() -> None:
    assert collect_design_terms(
        " ＰＥＴＧ  Carrier ",
        {"materials": ["PETG", " ABS "], "details": {"kind": "Cradle"}},
    ) == ("abs", "cradle", "petg", "petg carrier")
