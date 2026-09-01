from __future__ import annotations

import pytest

from mechanical_design_agent.approval_semantics import classify_approval


@pytest.mark.parametrize(
    "text",
    [
        "批准",
        "同意",
        "可以",
        "继续",
        "确认",
        "全部发布",
        "改了就都发布啊",
        "可以，继续",
        "我同意这个设计",
        "approve",
        "approved",
        "APPROVED",
        "yes",
        "proceed",
        "go ahead",
        "Yes, proceed.",
        "I approve this design.",
    ],
)
def test_clear_approval_phrases(text: str) -> None:
    assert classify_approval(text) == "APPROVE"


@pytest.mark.parametrize(
    "text",
    [
        "拒绝",
        "不同意",
        "不批准",
        "不发布",
        "不全部发布",
        "不都发布",
        "不可以",
        "停止",
        "reject",
        "rejected",
        "no",
        "stop",
        "not approved",
        "do not approve",
        "don't approve",
        "No, stop.",
    ],
)
def test_clear_rejection_phrases(text: str) -> None:
    assert classify_approval(text) == "REJECT"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "maybe",
        "looks interesting",
        "yes but no",
        "批准，但是先改材料",
        "approve if the handle is metal",
        "perhaps proceed",
        "可以吗",
    ],
)
def test_unclear_conditional_contradictory_or_unknown_text(text: str) -> None:
    assert classify_approval(text) == "UNCLEAR"


@pytest.mark.parametrize(
    "text",
    [
        "yesterday",
        "process",
        "approval pending",
        "stopping distance",
        "nobody approved it",
    ],
)
def test_substrings_do_not_create_false_decisions(text: str) -> None:
    assert classify_approval(text) == "UNCLEAR"


def test_unicode_and_surrounding_punctuation_are_normalized() -> None:
    assert classify_approval("  ＡＰＰＲＯＶＥＤ！ ") == "APPROVE"
    assert classify_approval("「同意」") == "APPROVE"


def test_non_string_input_is_unclear() -> None:
    assert classify_approval(None) == "UNCLEAR"
    assert classify_approval(123) == "UNCLEAR"
