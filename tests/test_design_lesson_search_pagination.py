from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import unittest
import uuid

from mechanical_design_agent.context import DesignContextBuilder
from mechanical_design_agent.repository import PostgresRepository


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class _SearchConnection:
    def __init__(self, candidate_count: int = 51) -> None:
        now = datetime(2026, 8, 10, tzinfo=timezone.utc)
        self.candidates = [
            {
                "id": uuid.UUID(int=index + 1),
                "organization_id": "org",
                "lesson_key": f"DL-PAGE-{index:03d}",
                "revision": 1,
                "status": "approved",
                "approved_at": now - timedelta(seconds=index),
            }
            for index in range(candidate_count)
        ]
        self.queries: list[str] = []

    def execute(self, query: str, parameters=()):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if normalized.startswith("SELECT l.* FROM design_lesson_events l"):
            return _Rows(self.candidates)
        if "FROM design_lesson_assertions l JOIN knowledge_assertions a" in normalized:
            return _Rows()
        if normalized.startswith("SELECT lesson_event_id,change_set_id"):
            return _Rows()
        raise AssertionError(f"unexpected query: {normalized}")


class _PagedContextRepository:
    def __init__(self) -> None:
        self.page_calls: list[dict] = []
        self.first_page = [
            self._lesson(index, required_conditions=["not-satisfied"])
            for index in range(50)
        ]
        self.second_page = [self._lesson(50, required_conditions=[])]

    @staticmethod
    def _lesson(index: int, *, required_conditions: list[str]) -> dict:
        return {
            "id": f"lesson-{index:03d}",
            "lesson_key": f"DL-CONTEXT-{index:03d}",
            "revision": 1,
            "status": "approved",
            "source_family_id": "family-a",
            "title": f"Lesson {index}",
            "problem": {"summary": "Alignment issue", "failure_modes": []},
            "root_causes": ["Alignment was not verified"],
            "corrections": ["Verify alignment"],
            "prevention": {"required_checks": ["Alignment check"]},
            "applicability": {
                "component_classes": ["shaft-support"],
                "interface_types": ["coaxial-interface"],
                "design_stages": [],
                "required_conditions": required_conditions,
            },
            "non_applicable_conditions": [],
            "search_terms": [],
            "assertions": [],
        }

    def get_design_group(self, design_group_id: str) -> dict:
        return {"id": design_group_id, "organization_id": "org"}

    def get_family(self, family_id: str) -> dict:
        return {
            "id": family_id,
            "organization_id": "org",
            "design_group_id": "group-a",
        }

    def approved_assertions(self, **_kwargs) -> list[dict]:
        return []

    def search_approved_design_lesson_page(self, **kwargs) -> dict:
        self.page_calls.append(kwargs)
        if kwargs.get("cursor") is None:
            return {"items": self.first_page, "next_cursor": "page-two"}
        if kwargs.get("cursor") == "page-two":
            return {"items": self.second_page, "next_cursor": None}
        raise AssertionError(f"unexpected cursor: {kwargs.get('cursor')}")

    def approved_family_profile(self, _family_id: str):
        return None

    def excluded_specialized_count(self, *_args):
        return []


class DesignLessonSearchPaginationTests(unittest.TestCase):
    def test_repository_page_batch_hydrates_fifty_candidates_in_three_queries(self) -> None:
        connection = _SearchConnection()
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection

        page = repository.search_approved_design_lesson_page(
            organization_id="org",
            query="",
            page_size=50,
        )

        self.assertEqual(len(page["items"]), 50)
        self.assertIsInstance(page["next_cursor"], str)
        self.assertEqual(len(connection.queries), 3)

    def test_repository_cursor_cannot_be_replayed_across_query_or_scope(self) -> None:
        connection = _SearchConnection()
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        cursor = repository.search_approved_design_lesson_page(
            organization_id="org",
            query="",
            page_size=50,
        )["next_cursor"]

        with self.assertRaisesRegex(ValueError, "cursor does not match"):
            repository.search_approved_design_lesson_page(
                organization_id="other-org",
                query="",
                page_size=50,
                cursor=cursor,
            )
        with self.assertRaisesRegex(ValueError, "cursor does not match"):
            repository.search_approved_design_lesson_page(
                organization_id="org",
                query="different",
                page_size=50,
                cursor=cursor,
            )

    def test_repository_cursor_does_not_disclose_raw_lesson_identity(self) -> None:
        connection = _SearchConnection()
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        page = repository.search_approved_design_lesson_page(
            organization_id="org",
            query="",
            page_size=50,
        )
        cursor = page["next_cursor"]
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        )

        self.assertNotIn(str(connection.candidates[49]["id"]), payload.values())
        self.assertNotIn("id", payload)

    def test_context_scans_later_pages_and_caps_excluded_candidates(self) -> None:
        repository = _PagedContextRepository()

        context = DesignContextBuilder(repository).build(
            organization_id="org",
            design_group_id="group-a",
            requested_family_id="family-a",
            explicit_family_authorization=True,
            design_features={
                "component_classes": ["shaft-support"],
                "interface_types": ["coaxial-interface"],
                "satisfied_conditions": [],
            },
        )

        self.assertEqual(
            [item["lesson_id"] for item in context["approved_design_lessons"]],
            ["DL-CONTEXT-050"],
        )
        self.assertEqual(len(context["excluded_design_lessons"]), 50)
        self.assertEqual(
            [(call["page_size"], call.get("cursor")) for call in repository.page_calls],
            [(50, None), (50, "page-two")],
        )


if __name__ == "__main__":
    unittest.main()
