from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

import pytest

from mechanical_design_agent.config import LightweightDesignSettings
from mechanical_design_agent.lightweight_design import LightweightDesignService
from mechanical_design_agent.lightweight_knowledge import LightweightKnowledgeService
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


def _session(tmp_path: Path) -> LightweightDesignService:
    settings = LightweightDesignSettings(
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

    service = LightweightDesignService(settings, seed_creator=seed)
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

    knowledge = LightweightKnowledgeService(sessions, load_context)

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
    knowledge = LightweightKnowledgeService(
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

    result = LightweightKnowledgeService(sessions, unavailable).retrieve(
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

    result = LightweightKnowledgeService(sessions, load).retrieve(
        design_id="carrier",
        query="required family lesson",
        features={"requested_family_id": "required-family"},
        required=True,
    )

    assert result["blocking"] is True


def test_used_ids_must_come_from_the_current_context(tmp_path: Path) -> None:
    sessions = _session(tmp_path)
    knowledge = LightweightKnowledgeService(
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
