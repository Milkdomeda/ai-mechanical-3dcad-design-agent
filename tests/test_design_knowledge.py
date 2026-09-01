from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

import pytest

from mechanical_design_agent.config import DesignSettings
from mechanical_design_agent.design_knowledge import DesignKnowledgeService
from mechanical_design_agent.design_session import DesignSessionService
from mechanical_design_agent.knowledge_repository import KnowledgeScope
from mechanical_design_agent.knowledge_service import KnowledgeService
from mechanical_design_agent.secure_fs import FileIdentity


def _empty_fcstd() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Document.xml",
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Document SchemaVersion="4"><ObjectData/></Document>',
        )
    return output.getvalue()


def _session(tmp_path: Path) -> DesignSessionService:
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
        destination.write_bytes(_empty_fcstd())

    service = DesignSessionService(settings, seed_creator=seed)
    service.start(
        design_id="carrier",
        title="Carrier",
        model_classification="new_design",
        requirements={"capacity": 4},
        proposal_summary="Printed carrier",
        approval_text="yes",
    )
    return service


def test_matching_knowledge_is_returned_and_selected_ids_are_recorded(
    tmp_path: Path,
) -> None:
    sessions = _session(tmp_path)

    def load_context(query: str, features: dict[str, object]) -> dict[str, object]:
        assert query == "basketball cradle"
        assert features == {"material": "PLA"}
        return {
            "schema_version": "DesignContext/v2",
            "approved_facts": [{"assertion_id": "fact-1"}],
            "hard_constraints": [],
            "preferences": [],
            "specialized_knowledge": [],
            "approved_design_lessons": [
                {"design_lesson_ref": "lesson-1", "assertions": []}
            ],
            "similar_models": [],
        }

    knowledge = DesignKnowledgeService(sessions, load_context)

    result = knowledge.retrieve(
        design_id="carrier",
        query="basketball cradle",
        features={"material": "PLA"},
        used_ids=["fact-1"],
    )

    assert result["status"] == "completed_matches"
    assert result["blocking"] is False
    assert result["available_ids"] == ["fact-1", "lesson-1"]
    state = sessions.get("carrier")
    assert state["knowledge"]["used_ids"] == ["fact-1"]


def test_no_match_is_nonblocking_by_default(tmp_path: Path) -> None:
    sessions = _session(tmp_path)
    knowledge = DesignKnowledgeService(
        sessions,
        lambda _query, _features: {
            "schema_version": "DesignContext/v2",
            "approved_facts": [],
            "hard_constraints": [],
            "preferences": [],
            "specialized_knowledge": [],
            "approved_design_lessons": [],
            "similar_models": [],
        },
    )

    result = knowledge.retrieve(
        design_id="carrier", query="no match", features={}
    )

    assert result["status"] == "completed_no_match"
    assert result["blocking"] is False
    assert sessions.get("carrier")["knowledge"]["status"] == "completed_no_match"


def test_unavailable_backend_warns_and_continues_when_optional(tmp_path: Path) -> None:
    sessions = _session(tmp_path)

    def unavailable(_query: str, _features: dict[str, object]) -> dict[str, object]:
        raise ConnectionError("secret database address")

    result = DesignKnowledgeService(sessions, unavailable).retrieve(
        design_id="carrier", query="optional", features={}
    )

    assert result["status"] == "unavailable"
    assert result["blocking"] is False
    assert "secret database address" not in str(result["warning"])
    assert sessions.get("carrier")["knowledge"]["status"] == "unavailable"


@pytest.mark.parametrize("mode", ["unavailable", "no-match"])
def test_required_knowledge_blocks_when_unavailable_or_unresolved(
    tmp_path: Path, mode: str
) -> None:
    sessions = _session(tmp_path)

    def load(_query: str, _features: dict[str, object]) -> dict[str, object]:
        if mode == "unavailable":
            raise RuntimeError("offline")
        return {
            "schema_version": "DesignContext/v2",
            "approved_facts": [],
            "hard_constraints": [],
            "preferences": [],
            "specialized_knowledge": [],
            "approved_design_lessons": [],
            "similar_models": [],
        }

    result = DesignKnowledgeService(sessions, load).retrieve(
        design_id="carrier",
        query="required family lesson",
        features={"requested_family_id": "required-family"},
        required=True,
    )

    assert result["blocking"] is True


def test_used_ids_must_come_from_the_current_context(tmp_path: Path) -> None:
    sessions = _session(tmp_path)
    knowledge = DesignKnowledgeService(
        sessions,
        lambda _query, _features: {
            "schema_version": "DesignContext/v2",
            "approved_facts": [{"assertion_id": "fact-1"}],
            "hard_constraints": [],
            "preferences": [],
            "specialized_knowledge": [],
            "approved_design_lessons": [],
            "similar_models": [],
        },
    )

    with pytest.raises(ValueError, match="not present"):
        knowledge.retrieve(
            design_id="carrier",
            query="query",
            features={},
            used_ids=["unknown"],
        )


class _ContextRepository:
    scope = KnowledgeScope("org-1", "group-1")

    def match_product_family(self, **kwargs):
        self.match_request = kwargs
        return {
            "id": "PF-PILOT-001",
            "knowledge_id": "PF-PILOT-001",
            "kind": "product_family",
            "canonical_name": "Printed Ball Carrier",
            "profile": {"mechanism": "spherical cradle"},
            "status": "active",
            "match_kind": "exact_term",
        }

    def search(self, **kwargs):
        self.search_request = kwargs
        return {
            "families": [],
            "assertions": [
                {
                    "id": "assertion-carrier-only",
                    "assertion_id": "assertion-carrier-only",
                    "kind": "knowledge_assertion",
                    "status": "active",
                    "applicability": {
                        "conditions": {"design_type": "carrier"}
                    },
                },
                {
                    "id": "assertion-general",
                    "assertion_id": "assertion-general",
                    "kind": "knowledge_assertion",
                    "status": "active",
                    "applicability": {"summary": "all printed designs"},
                },
            ],
            "lessons": [
                {
                    "id": "lesson-cradle",
                    "design_lesson_ref": "lesson-cradle",
                    "kind": "design_lesson",
                    "status": "active",
                    "applicability": {
                        "conditions": {"material": ["PETG", "ABS"]}
                    },
                }
            ],
        }


class _UnavailableProjection:
    def __getattr__(self, name):
        raise AssertionError(f"design context accessed projection: {name}")


def test_context_build_uses_features_and_populates_all_knowledge(
    tmp_path: Path,
) -> None:
    repository = _ContextRepository()
    service = KnowledgeService(repository, _UnavailableProjection(), tmp_path)

    context = service.design_context_build(
        organization_id="org-1",
        design_group_id="group-1",
        requested_family_id=None,
        design_features={"design_type": "carrier", "material": "PETG"},
        lesson_query="spherical cradle",
    )

    assert context["specialized_knowledge"][0]["id"] == "PF-PILOT-001"
    assert context["approved_facts"]
    assert context["approved_design_lessons"]
    assert all(item["status"] == "active" for item in context["approved_facts"])
    assert repository.match_request["design_features"]["material"] == "PETG"


def test_context_excludes_inapplicable_assertion(tmp_path: Path) -> None:
    repository = _ContextRepository()
    service = KnowledgeService(repository, _UnavailableProjection(), tmp_path)

    context = service.design_context_build(
        organization_id="org-1",
        design_group_id="group-1",
        requested_family_id="PF-PILOT-001",
        design_features={"design_type": "shaft", "material": "PETG"},
        lesson_query="carrier",
    )

    assert "assertion-carrier-only" not in {
        row["assertion_id"] for row in context["approved_facts"]
    }


def test_context_scope_must_match_repository(tmp_path: Path) -> None:
    service = KnowledgeService(
        _ContextRepository(), _UnavailableProjection(), tmp_path
    )
    with pytest.raises(ValueError, match="scope"):
        service.design_context_build(
            organization_id="other-org",
            design_group_id="group-1",
            requested_family_id=None,
            design_features={},
            lesson_query="carrier",
        )
