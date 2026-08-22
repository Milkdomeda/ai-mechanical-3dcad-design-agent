from __future__ import annotations

import unittest
from pathlib import Path


class RuntimeBoundaryTests(unittest.TestCase):
    def test_runtime_has_no_external_llm_or_blender_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        runtime_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for base in (root / "src", root / "freecad")
            for path in base.rglob("*.py")
        ).lower()
        for forbidden in (
            "import openai",
            "from openai",
            "anthropic",
            "gemini",
            "ollama",
            "import blender",
            "mechanical-assembly-extension",
            "wr-",
        ):
            self.assertNotIn(forbidden, runtime_text)

        project = (root / "pyproject.toml").read_text(encoding="utf-8").lower()
        for forbidden in ("openai", "anthropic", "blender"):
            self.assertNotIn(forbidden, project)


if __name__ == "__main__":
    unittest.main()
