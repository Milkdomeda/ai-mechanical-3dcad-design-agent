from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest

from mechanical_design_agent.config import DesignSettings
from mechanical_design_agent.design_lesson_workflow import DesignLessonWorkflow
from mechanical_design_agent.design_session import DesignSessionService
from mechanical_design_agent.hashing import file_sha256
from mechanical_design_agent.secure_fs import FileIdentity


def _fcstd(object_name: str | None = None) -> bytes:
    object_xml = (
        f'<Object type="Part::Feature" name="{object_name}"/>'
        if object_name
        else ""
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Document SchemaVersion="4">'
            f"<ObjectData>{object_xml}</ObjectData></Document>",
        )
    return output.getvalue()


def _service(tmp_path: Path) -> DesignSessionService:
    settings = DesignSettings(
        workspace=tmp_path,
        package_root=tmp_path,
        design_root=tmp_path / "designs",
        freecadcmd=tmp_path / "FreeCADCmd",
        freecadcmd_sha256="a" * 64,
        freecadcmd_identity=FileIdentity(1, 2),
        freecadcmd_version="1.1.3",
    )

    def seed(destination: Path) -> None:
        destination.write_bytes(_fcstd())

    service = DesignSessionService(settings, seed_creator=seed)
    service.start(
        design_id="carrier",
        title="Basketball Carrier",
        model_classification="new_design",
        requirements={"capacity": 4},
        proposal_summary="A PLA carrier",
        approval_text="同意",
    )
    return service


def _complete(service: DesignSessionService, tmp_path: Path) -> Path:
    root = tmp_path / "designs" / "carrier"
    model = root / "model.FCStd"
    model.write_bytes(_fcstd("Carrier"))
    model_sha256 = file_sha256(model)
    report = root / "validation" / "model_validation.json"
    report.write_text(
        json.dumps(
            {
                "status": "passed",
                "working_sha256": model_sha256,
                "checks": [
                    {
                        "id": "shape-validity",
                        "validator": "freecad-model-validation",
                        "status": "passed",
                        "message": "valid",
                        "mandatory": True,
                    }
                ],
                "fastener_inventory": [],
                "summary": {
                    "passed": 1,
                    "failed": 0,
                    "warnings": 0,
                    "fasteners_detected": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    markdown = root / "validation" / "model_validation.md"
    image = root / "validation" / "model_validation.png"
    markdown.write_text("# passed\n", encoding="utf-8")
    image.write_bytes(b"visual evidence")
    service.record_result(
        design_id="carrier",
        model_path=str(model),
        validation_report_path=str(report),
        evidence_paths=[str(markdown), str(image)],
    )
    return root


def _candidate() -> dict[str, object]:
    return {
        "problem": "A long upright handle can concentrate bending at its base.",
        "decision": "Use broad mirrored handle roots with generous radii.",
        "evidence": [
            "validation/model_validation.json",
            "validation/model_validation.png",
        ],
        "applicability": "Large printed carriers with a central upright handle.",
        "prevention_action": "Validate root thickness and inspect both load paths.",
        "search_terms": ["printed carrier handle root", "PLA handle bending"],
        "scope": "organization_general",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [("不批准", "REJECT"), ("也许可以", "UNCLEAR")],
)
def test_nonapproval_does_not_confirm_or_evaluate_lessons(
    tmp_path: Path, text: str, expected: str
) -> None:
    service = _service(tmp_path)
    _complete(service, tmp_path)

    result = DesignLessonWorkflow(service).confirm(
        design_id="carrier", confirmation_text=text, candidates=[]
    )

    assert result["confirmation_state"] == expected
    assert result["lesson_review_status"] == "not_evaluated"
    assert service.get("carrier")["final_confirmation"]["state"] == "not_confirmed"


def test_confirmation_requires_completed_exact_hash_validation(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="completed model"):
        DesignLessonWorkflow(service).confirm(
            design_id="carrier", confirmation_text="确认", candidates=[]
        )


def test_confirmation_without_material_lessons_finishes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _complete(service, tmp_path)

    result = DesignLessonWorkflow(service).confirm(
        design_id="carrier", confirmation_text="设计已确认", candidates=[]
    )

    assert result["confirmation_state"] == "APPROVE"
    assert result["lesson_review_status"] == "no_material_lessons"
    assert result["next_action"] == "finish"
    state = service.get("carrier")
    assert state["final_confirmation"]["model_sha256"] == state["model"]["sha256"]
    assert state["lesson_review"]["status"] == "no_material_lessons"


def test_material_lesson_creates_one_immutable_review_card(tmp_path: Path) -> None:
    service = _service(tmp_path)
    root = _complete(service, tmp_path)
    workflow = DesignLessonWorkflow(service)

    result = workflow.confirm(
        design_id="carrier",
        confirmation_text="confirmed",
        candidates=[_candidate()],
    )

    review_path = root / str(result["review_relative_path"])
    assert result["lesson_review_status"] == "review_pending"
    assert result["next_action"] == "request_lesson_publication_decision"
    assert result["review_sha256"] == file_sha256(review_path)
    assert result["review_card"]["schema_version"] == "DesignLessonReviewCard/v1"
    assert len(result["review_card"]["lessons"]) == 1

    repeated = workflow.confirm(
        design_id="carrier",
        confirmation_text="yes",
        candidates=[_candidate()],
    )
    assert repeated["review_sha256"] == result["review_sha256"]


def test_invalid_candidate_reports_fields_but_keeps_confirmation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _complete(service, tmp_path)
    candidate = _candidate()
    candidate["evidence"] = ["not-present.txt"]

    result = DesignLessonWorkflow(service).confirm(
        design_id="carrier",
        confirmation_text="确认",
        candidates=[candidate],
    )

    assert result["lesson_review_status"] == "candidate_errors"
    assert result["candidate_errors"][0]["field"] == "evidence"
    state = service.get("carrier")
    assert state["model_status"] == "completed"
    assert state["final_confirmation"]["state"] == "APPROVE"


def test_private_or_project_only_candidate_is_not_published(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _complete(service, tmp_path)
    candidate = _candidate()
    candidate["scope"] = "customer_specific"

    result = DesignLessonWorkflow(service).confirm(
        design_id="carrier",
        confirmation_text="设计已确认",
        candidates=[candidate],
    )

    assert result["lesson_review_status"] == "no_material_lessons"
    assert result["screened_candidates"][0]["reason"] == (
        "private_or_project_specific"
    )


def test_changed_model_invalidates_confirmation_and_review(tmp_path: Path) -> None:
    service = _service(tmp_path)
    root = _complete(service, tmp_path)
    DesignLessonWorkflow(service).confirm(
        design_id="carrier",
        confirmation_text="确认",
        candidates=[_candidate()],
    )

    (root / "model.FCStd").write_bytes(_fcstd("ChangedCarrier"))
    state = service.get("carrier")

    assert state["model_status"] == "needs_attention"
    assert state["final_confirmation"]["state"] == "not_confirmed"
    assert state["lesson_review"]["status"] == "invalidated"


class _Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[dict[str, object], str]] = []

    def publish_design_lesson_review(
        self,
        *,
        review_card: dict[str, object],
        review_sha256: str,
        decision_text: str,
    ) -> dict[str, object]:
        assert decision_text
        self.calls.append((review_card, review_sha256))
        if self.fail:
            raise ConnectionError("private database details")
        return {"publication_id": f"publication-{review_sha256[:12]}"}


def _pending_workflow(
    tmp_path: Path,
) -> tuple[DesignSessionService, DesignLessonWorkflow]:
    service = _service(tmp_path)
    _complete(service, tmp_path)
    workflow = DesignLessonWorkflow(service)
    workflow.confirm(
        design_id="carrier",
        confirmation_text="确认",
        candidates=[_candidate()],
    )
    return service, workflow


def test_publication_approval_publishes_the_exact_review_once(tmp_path: Path) -> None:
    service, workflow = _pending_workflow(tmp_path)
    publisher = _Publisher()

    result = workflow.decide(
        design_id="carrier", decision_text="go ahead", publisher=publisher
    )
    repeated = workflow.decide(
        design_id="carrier", decision_text="approved", publisher=publisher
    )

    assert result["status"] == "published"
    assert repeated["status"] == "published"
    assert repeated["resumed"] is True
    assert len(publisher.calls) == 1
    assert publisher.calls[0][0]["model_sha256"] == service.get("carrier")[
        "model"
    ]["sha256"]


def test_publication_rejection_is_local_and_idempotent(tmp_path: Path) -> None:
    service, workflow = _pending_workflow(tmp_path)
    publisher = _Publisher()

    result = workflow.decide(
        design_id="carrier", decision_text="reject", publisher=publisher
    )
    repeated = workflow.decide(
        design_id="carrier", decision_text="no", publisher=publisher
    )

    assert result["status"] == "declined"
    assert repeated["resumed"] is True
    assert publisher.calls == []
    assert service.get("carrier")["model_status"] == "completed"


def test_unclear_publication_decision_does_not_mutate_review(tmp_path: Path) -> None:
    service, workflow = _pending_workflow(tmp_path)

    result = workflow.decide(
        design_id="carrier", decision_text="maybe", publisher=_Publisher()
    )

    assert result["decision_state"] == "UNCLEAR"
    assert service.get("carrier")["lesson_review"]["status"] == "review_pending"


def test_database_failure_preserves_model_and_review_for_retry(tmp_path: Path) -> None:
    service, workflow = _pending_workflow(tmp_path)
    unavailable = _Publisher(fail=True)

    failed = workflow.decide(
        design_id="carrier", decision_text="批准", publisher=unavailable
    )
    publisher = _Publisher()
    retried = workflow.decide(
        design_id="carrier", decision_text="继续", publisher=publisher
    )

    assert failed["status"] == "publish_retry_required"
    assert "private database details" not in str(failed["warning"])
    assert service.get("carrier")["model_status"] == "completed"
    assert retried["status"] == "published"
    assert len(publisher.calls) == 1
