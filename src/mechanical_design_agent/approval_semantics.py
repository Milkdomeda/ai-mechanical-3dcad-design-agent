from __future__ import annotations

import re
import unicodedata


APPROVE = "APPROVE"
REJECT = "REJECT"
UNCLEAR = "UNCLEAR"

_ENGLISH_APPROVE = (
    "go ahead",
    "confirmed",
    "confirm",
    "approved",
    "approve",
    "proceed",
    "yes",
)
_CHINESE_APPROVE = (
    "全部发布",
    "都发布",
    "批准",
    "同意",
    "可以",
    "继续",
    "确认",
)

_ENGLISH_REJECT = (
    "do not approve",
    "don t approve",
    "not approved",
    "not approve",
    "rejected",
    "reject",
    "stop",
    "no",
)
_CHINESE_REJECT = (
    "不全部发布",
    "不都发布",
    "不发布",
    "不同意",
    "不批准",
    "不可以",
    "不要",
    "拒绝",
    "停止",
)

_ENGLISH_UNCLEAR = (
    "maybe",
    "perhaps",
    "if",
    "however",
    "but",
    "nobody approved",
)
_CHINESE_UNCLEAR = ("如果", "也许", "可能", "但是", "不过", "但", "吗")


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith(("P", "S")):
            characters.append(" ")
        else:
            characters.append(character)
    return " ".join("".join(characters).split())


def _english_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(phrase).replace(r"\ ", r"\s+")
        for phrase in sorted(phrases, key=len, reverse=True)
    )
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


_ENGLISH_APPROVE_PATTERN = _english_pattern(_ENGLISH_APPROVE)
_ENGLISH_REJECT_PATTERN = _english_pattern(_ENGLISH_REJECT)
_ENGLISH_UNCLEAR_PATTERN = _english_pattern(_ENGLISH_UNCLEAR)


def classify_approval(text: object) -> str:
    """Classify concise Chinese or English design-direction feedback.

    The classifier is deliberately conservative. It recognizes a small reviewed
    vocabulary, rejects explicit negation before considering positive words,
    and returns UNCLEAR for conditional or contradictory text.
    """

    if not isinstance(text, str):
        return UNCLEAR
    normalized = _normalize(text)
    if not normalized:
        return UNCLEAR

    english_reject = bool(_ENGLISH_REJECT_PATTERN.search(normalized))
    chinese_rejects = [cue for cue in _CHINESE_REJECT if cue in normalized]
    has_reject = english_reject or bool(chinese_rejects)

    residual = _ENGLISH_REJECT_PATTERN.sub(" ", normalized)
    for cue in chinese_rejects:
        residual = residual.replace(cue, " ")
    residual = " ".join(residual.split())

    has_approve = bool(_ENGLISH_APPROVE_PATTERN.search(residual)) or any(
        cue in residual for cue in _CHINESE_APPROVE
    )
    has_unclear = bool(_ENGLISH_UNCLEAR_PATTERN.search(normalized)) or any(
        cue in normalized for cue in _CHINESE_UNCLEAR
    )

    if has_reject and has_approve:
        return UNCLEAR
    if has_reject:
        return REJECT
    if has_approve and has_unclear:
        return UNCLEAR
    if has_approve:
        return APPROVE
    return UNCLEAR
