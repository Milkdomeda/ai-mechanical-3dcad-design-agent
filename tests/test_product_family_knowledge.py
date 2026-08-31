from __future__ import annotations

from pathlib import Path

from mechanical_design_agent.product_family_knowledge import (
    ProductFamilyKnowledgeService,
)


class _Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish_product_family(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"family_id": kwargs["family_id"], "status": "active"}


def _start(service: ProductFamilyKnowledgeService) -> dict[str, object]:
    return service.start(
        {
            "onboarding_id": "carrier-family-onboarding",
            "family_id": "carrier-family",
            "family_name": "Printed Ball Carriers",
            "aliases": ["sports-ball carrier"],
            "source_paths": ["reference.FCStd"],
        }
    )


def test_family_analysis_review_and_publication_are_independent_of_design(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    service = ProductFamilyKnowledgeService(tmp_path, repository)

    started = _start(service)
    analyzed = service.analyze(
        "carrier-family-onboarding",
        {
            "assertions": [
                {
                    "subject": "handle root",
                    "predicate": "uses",
                    "object": "broad radiused transition",
                }
            ]
        },
    )
    reviewed = service.review(
        "carrier-family-onboarding", "同意", {"notes": "Reusable"}
    )
    published = service.publish("carrier-family-onboarding")

    assert started["status"] == "started"
    assert analyzed["status"] == "analyzed"
    assert reviewed["status"] == "approved"
    assert published["status"] == "active"
    assert len(repository.calls) == 1
    assert repository.calls[0]["family_id"] == "carrier-family"
    assert service.status("carrier-family-onboarding")["status"] == "published"


def test_family_review_accepts_natural_language_and_unclear_does_not_mutate(
    tmp_path: Path,
) -> None:
    service = ProductFamilyKnowledgeService(tmp_path, _Repository())
    _start(service)
    service.analyze("carrier-family-onboarding", {"assertions": []})

    unclear = service.review(
        "carrier-family-onboarding", "maybe", {"notes": "uncertain"}
    )

    assert unclear["decision_state"] == "UNCLEAR"
    assert service.status("carrier-family-onboarding")["status"] == "analyzed"


def test_family_publication_is_idempotent(tmp_path: Path) -> None:
    repository = _Repository()
    service = ProductFamilyKnowledgeService(tmp_path, repository)
    _start(service)
    service.analyze("carrier-family-onboarding", {"assertions": []})
    service.review("carrier-family-onboarding", "approved", {})

    first = service.publish("carrier-family-onboarding")
    repeated = service.publish("carrier-family-onboarding")

    assert first["family_id"] == repeated["family_id"]
    assert repeated["resumed"] is True
    assert len(repository.calls) == 1
