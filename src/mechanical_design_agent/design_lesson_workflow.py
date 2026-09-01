from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .approval_semantics import APPROVE, REJECT, classify_approval
from .design_session import DesignSessionService
from .hashing import file_sha256
from .models import canonical_json
from .secure_fs import (
    atomic_publish_new,
    ensure_managed_directory,
    read_managed_file,
    set_managed_file_readonly,
    validate_managed_path,
)


_REVIEW_SCHEMA = "DesignLessonReviewCard/v1"
_PUBLICATION_SCOPES = frozenset(
    {"organization_general", "design_group", "product_family"}
)
_EXCLUDED_SCOPES = frozenset({"project_only", "customer_specific"})
_REQUIRED_TEXT_FIELDS = (
    "problem",
    "decision",
    "applicability",
    "prevention_action",
)


def _copy_json(value: object, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc


class DesignLessonWorkflow:
    """Run Design Lesson evaluation immediately after final confirmation."""

    def __init__(self, sessions: DesignSessionService) -> None:
        self.sessions = sessions

    def confirm(
        self,
        *,
        design_id: str,
        confirmation_text: str,
        candidates: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        confirmation = self.sessions.confirm(
            design_id=design_id,
            confirmation_text=confirmation_text,
        )
        if confirmation["confirmation_state"] != APPROVE:
            return {
                **confirmation,
                "schema_version": "DesignConfirmationAndLearningResult/v1",
                "lesson_review_status": "not_evaluated",
            }

        context = self.sessions.confirmation_context(design_id)
        accepted, screened, errors = self._evaluate_candidates(candidates, context)
        model_sha256 = str(context["model_sha256"])
        if errors:
            self.sessions.record_lesson_review(
                design_id=design_id,
                model_sha256=model_sha256,
                status="candidate_errors",
                warning="candidate validation requires correction",
            )
            return {
                **confirmation,
                "schema_version": "DesignConfirmationAndLearningResult/v1",
                "lesson_review_status": "candidate_errors",
                "candidate_errors": errors,
                "screened_candidates": screened,
                "next_action": "correct_lesson_candidates",
            }

        if not accepted:
            self.sessions.record_lesson_review(
                design_id=design_id,
                model_sha256=model_sha256,
                status="no_material_lessons",
            )
            return {
                **confirmation,
                "schema_version": "DesignConfirmationAndLearningResult/v1",
                "lesson_review_status": "no_material_lessons",
                "screened_candidates": screened,
                "next_action": "finish",
            }

        card, relative_path, card_sha256 = self._prepare_review_card(
            context=context,
            lessons=accepted,
            screened=screened,
        )
        self.sessions.record_lesson_review(
            design_id=design_id,
            model_sha256=model_sha256,
            status="review_pending",
            review_relative_path=relative_path,
            review_sha256=card_sha256,
        )
        return {
            **confirmation,
            "schema_version": "DesignConfirmationAndLearningResult/v1",
            "lesson_review_status": "review_pending",
            "review_card": card,
            "review_relative_path": relative_path,
            "review_sha256": card_sha256,
            "next_action": "request_lesson_publication_decision",
        }

    def decide(
        self,
        *,
        design_id: str,
        decision_text: str,
        publisher: object,
        selected_lesson_numbers: Sequence[int] | None = None,
    ) -> dict[str, object]:
        """Publish or decline the exact review card currently bound to a design."""
        decision = classify_approval(decision_text)
        state = self.sessions.get(design_id)
        review = state["lesson_review"]
        if decision not in {APPROVE, REJECT}:
            return {
                "schema_version": "DesignLessonDecisionResult/v1",
                "design_id": design_id,
                "decision_state": decision,
                "status": review["status"],
                "next_action": "clarify_publication_decision",
            }
        if selected_lesson_numbers and decision != APPROVE:
            raise ValueError("lesson selection is valid only with publication approval")
        if review["status"] == "published" and decision == APPROVE:
            return {
                "schema_version": "DesignLessonDecisionResult/v1",
                "design_id": design_id,
                "decision_state": APPROVE,
                "status": "published",
                "publication_id": review.get("publication_id"),
                "resumed": True,
                "next_action": "finish",
            }
        if review["status"] == "declined" and decision == REJECT:
            return {
                "schema_version": "DesignLessonDecisionResult/v1",
                "design_id": design_id,
                "decision_state": REJECT,
                "status": "declined",
                "resumed": True,
                "next_action": "finish",
            }
        if review["status"] not in {"review_pending", "publish_retry_required"}:
            raise ValueError("there is no publishable review card for this design")
        relative_path = review.get("review_relative_path")
        expected_sha256 = review.get("review_sha256")
        context = self.sessions.confirmation_context(design_id)
        review_path = Path(str(context["design_root"])) / str(relative_path)
        read = read_managed_file(review_path)
        if read.sha256 != expected_sha256:
            raise ValueError("review card changed after it was displayed")
        try:
            card = json.loads(read.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("review card is not valid UTF-8 JSON") from exc
        if (
            not isinstance(card, dict)
            or card.get("schema_version") != _REVIEW_SCHEMA
            or card.get("model_sha256") != context["model_sha256"]
        ):
            raise ValueError("review card identity is invalid or stale")

        if decision == REJECT:
            self.sessions.record_lesson_review(
                design_id=design_id,
                model_sha256=str(context["model_sha256"]),
                status="declined",
                review_relative_path=str(relative_path),
                review_sha256=str(expected_sha256),
            )
            return {
                "schema_version": "DesignLessonDecisionResult/v1",
                "design_id": design_id,
                "decision_state": REJECT,
                "status": "declined",
                "next_action": "finish",
            }

        if selected_lesson_numbers:
            existing_selection = card.get("selection")
            if isinstance(existing_selection, Mapping):
                existing_numbers = existing_selection.get("lesson_numbers")
                if existing_numbers != list(selected_lesson_numbers):
                    raise ValueError(
                        "a different lesson selection is already bound to this review"
                    )
            else:
                card, relative_path, expected_sha256 = self._prepare_selected_review_card(
                    context=context,
                    source_card=card,
                    source_sha256=str(expected_sha256),
                    selected_lesson_numbers=selected_lesson_numbers,
                )
                self.sessions.record_lesson_review(
                    design_id=design_id,
                    model_sha256=str(context["model_sha256"]),
                    status="review_pending",
                    review_relative_path=relative_path,
                    review_sha256=expected_sha256,
                )

        publish = getattr(publisher, "publish_design_lesson_review", None)
        if not callable(publish):
            raise ValueError("knowledge publisher does not support Design Lessons")
        try:
            published = publish(
                review_card=card,
                review_sha256=str(expected_sha256),
                decision_text=decision_text,
            )
        except Exception as exc:
            warning = f"knowledge publication unavailable ({type(exc).__name__})"
            self.sessions.record_lesson_review(
                design_id=design_id,
                model_sha256=str(context["model_sha256"]),
                status="publish_retry_required",
                review_relative_path=str(relative_path),
                review_sha256=str(expected_sha256),
                warning=warning,
            )
            return {
                "schema_version": "DesignLessonDecisionResult/v1",
                "design_id": design_id,
                "decision_state": APPROVE,
                "status": "publish_retry_required",
                "warning": warning,
                "next_action": "retry_publication",
            }
        if not isinstance(published, Mapping):
            raise ValueError("knowledge publisher returned an invalid result")
        publication_id = str(
            published.get("publication_id")
            or published.get("review_sha256")
            or expected_sha256
        )
        self.sessions.record_lesson_review(
            design_id=design_id,
            model_sha256=str(context["model_sha256"]),
            status="published",
            review_relative_path=str(relative_path),
            review_sha256=str(expected_sha256),
            publication_id=publication_id,
        )
        return {
            "schema_version": "DesignLessonDecisionResult/v1",
            "design_id": design_id,
            "decision_state": APPROVE,
            "status": "published",
            "publication_id": publication_id,
            "resumed": bool(published.get("resumed", False)),
            "next_action": "finish",
        }

    @staticmethod
    def _prepare_selected_review_card(
        *,
        context: Mapping[str, object],
        source_card: Mapping[str, object],
        source_sha256: str,
        selected_lesson_numbers: Sequence[int],
    ) -> tuple[dict[str, object], str, str]:
        lessons = source_card.get("lessons")
        if not isinstance(lessons, list) or not lessons:
            raise ValueError("review card has no selectable lessons")
        normalized: list[int] = []
        for number in selected_lesson_numbers:
            if isinstance(number, bool) or not isinstance(number, int):
                raise ValueError("selected lesson numbers must be integers")
            if number < 1 or number > len(lessons):
                raise ValueError("selected lesson number is outside the review card")
            if number not in normalized:
                normalized.append(number)
        if not normalized:
            raise ValueError("at least one lesson must be selected")

        selected_card = _copy_json(dict(source_card), "source review card")
        selected_card["review_id"] = (
            f"{source_card['review_id']}-selected-"
            + "-".join(str(number) for number in normalized)
        )
        selected_card["lessons"] = [lessons[number - 1] for number in normalized]
        selected_card["selection"] = {
            "source_review_sha256": source_sha256,
            "lesson_numbers": normalized,
        }
        card_bytes = canonical_json(selected_card).encode("utf-8")
        card_sha256 = hashlib.sha256(card_bytes).hexdigest()
        design_root = validate_managed_path(
            Path(str(context["design_root"])), allow_missing_leaf=False
        ).path
        review_root = validate_managed_path(
            design_root / "lesson-review", allow_missing_leaf=False
        ).path
        review_path = review_root / (
            "review-selected-" + "-".join(str(number) for number in normalized) + ".json"
        )
        if review_path.exists():
            existing = read_managed_file(review_path)
            if existing.content != card_bytes or existing.sha256 != card_sha256:
                raise ValueError("an immutable selected review card already exists")
        else:
            atomic_publish_new(review_path, card_bytes)
            set_managed_file_readonly(review_path)
        return (
            selected_card,
            review_path.relative_to(design_root).as_posix(),
            card_sha256,
        )

    @staticmethod
    def _evaluate_candidates(
        candidates: Sequence[Mapping[str, object]],
        context: Mapping[str, object],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValueError("candidates must be a list")
        allowed_evidence = {
            str(item["relative_path"])
            for item in context["evidence"]  # type: ignore[union-attr]
        }
        accepted: list[dict[str, object]] = []
        screened: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        for index, raw in enumerate(candidates):
            if not isinstance(raw, Mapping):
                errors.append(
                    {"index": index, "field": "$", "message": "must be an object"}
                )
                continue
            candidate = _copy_json(dict(raw), f"candidate {index}")
            candidate_errors: list[dict[str, object]] = []
            for field in _REQUIRED_TEXT_FIELDS:
                if not isinstance(candidate.get(field), str) or not str(
                    candidate[field]
                ).strip():
                    candidate_errors.append(
                        {"index": index, "field": field, "message": "is required"}
                    )
            evidence = candidate.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(value, str) and value.strip() for value in evidence
            ):
                candidate_errors.append(
                    {
                        "index": index,
                        "field": "evidence",
                        "message": "must contain referenced evidence paths",
                    }
                )
            elif unknown := sorted(set(evidence) - allowed_evidence):
                candidate_errors.append(
                    {
                        "index": index,
                        "field": "evidence",
                        "message": "contains unknown paths: " + ", ".join(unknown),
                    }
                )
            search_terms = candidate.get("search_terms")
            if not isinstance(search_terms, list) or not search_terms or not all(
                isinstance(value, str) and value.strip() for value in search_terms
            ):
                candidate_errors.append(
                    {
                        "index": index,
                        "field": "search_terms",
                        "message": "must contain nonblank strings",
                    }
                )
            scope = candidate.get("scope", "organization_general")
            if scope not in _PUBLICATION_SCOPES | _EXCLUDED_SCOPES:
                candidate_errors.append(
                    {"index": index, "field": "scope", "message": "is invalid"}
                )
            if not isinstance(candidate.get("reusable", True), bool):
                candidate_errors.append(
                    {"index": index, "field": "reusable", "message": "must be boolean"}
                )
            if candidate_errors:
                errors.extend(candidate_errors)
                continue
            if (
                scope in _EXCLUDED_SCOPES
                or candidate.get("contains_private_details") is True
                or candidate.get("reusable", True) is False
            ):
                screened.append(
                    {
                        "index": index,
                        "reason": (
                            "private_or_project_specific"
                            if scope in _EXCLUDED_SCOPES
                            or candidate.get("contains_private_details") is True
                            else "not_reusable"
                        ),
                    }
                )
                continue
            accepted.append(
                {
                    "problem": candidate["problem"].strip(),
                    "decision": candidate["decision"].strip(),
                    "evidence": list(dict.fromkeys(candidate["evidence"])),
                    "applicability": candidate["applicability"].strip(),
                    "prevention_action": candidate["prevention_action"].strip(),
                    "search_terms": list(dict.fromkeys(candidate["search_terms"])),
                    "scope": scope,
                    "product_family_id": candidate.get("product_family_id"),
                }
            )
        return accepted, screened, errors

    @staticmethod
    def _prepare_review_card(
        *,
        context: Mapping[str, object],
        lessons: Sequence[Mapping[str, object]],
        screened: Sequence[Mapping[str, object]],
    ) -> tuple[dict[str, object], str, str]:
        design_root = validate_managed_path(
            Path(str(context["design_root"])), allow_missing_leaf=False
        ).path
        review_root = ensure_managed_directory(
            design_root / "lesson-review", parents=False, exist_ok=True
        ).path
        model_sha256 = str(context["model_sha256"])
        if len(model_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in model_sha256
        ):
            raise ValueError("review-card model SHA-256 is invalid")
        review_path = review_root / f"review-{model_sha256}.json"
        review_id = (
            f"review-{context['design_id']}-{model_sha256[:12]}"
        )
        card: dict[str, object] = {
            "schema_version": _REVIEW_SCHEMA,
            "review_id": review_id,
            "design_id": context["design_id"],
            "design_title": context["title"],
            "model_sha256": context["model_sha256"],
            "validation_report_sha256": context["validation_report_sha256"],
            "evidence": context["evidence"],
            "lessons": list(lessons),
            "screening": list(screened),
        }
        card_bytes = canonical_json(card).encode("utf-8")
        card_sha256 = hashlib.sha256(card_bytes).hexdigest()
        if review_path.exists():
            existing = read_managed_file(review_path)
            if existing.content != card_bytes or existing.sha256 != card_sha256:
                raise ValueError("an immutable review card already exists for this design")
        else:
            atomic_publish_new(review_path, card_bytes)
            set_managed_file_readonly(review_path)
        if file_sha256(review_path) != card_sha256:
            raise ValueError("review card integrity check failed")
        return card, review_path.relative_to(design_root).as_posix(), card_sha256


__all__ = ["DesignLessonWorkflow"]
