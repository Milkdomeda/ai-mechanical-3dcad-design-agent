from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mechanical_design_agent.library import LibraryScanner


class LibraryScannerTests(unittest.TestCase):
    def test_inventory_requires_first_level_family_and_ignores_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            family = root / "Family-A"
            family.mkdir()
            (family / "one.step").write_bytes(b"step-one")
            (family / ".hidden.step").write_bytes(b"hidden")
            (root / "root.step").write_bytes(b"not-in-family")
            (family / "derived.stl").write_bytes(b"mesh")

            entries = LibraryScanner().inventory(root)

            self.assertEqual([item.relative_path for item in entries], ["Family-A/one.step"])
            self.assertEqual(entries[0].family_folder, "Family-A")

    def test_diff_classifies_modified_rename_duplicate_and_missing(self) -> None:
        scanner = LibraryScanner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            family = root / "Family-A"
            family.mkdir()
            original = family / "original.step"
            original.write_bytes(b"same")
            previous = scanner.inventory(root)

            renamed = family / "renamed.step"
            original.rename(renamed)
            current = scanner.inventory(root)
            self.assertEqual([item.kind for item in scanner.diff(current, previous)], ["renamed"])

            duplicate = family / "duplicate.step"
            duplicate.write_bytes(b"same")
            duplicated = scanner.inventory(root)
            kinds = [item.kind for item in scanner.diff(duplicated, current)]
            self.assertEqual(kinds, ["duplicate", "unchanged"])

            renamed.write_bytes(b"changed")
            modified = scanner.inventory(root)
            kinds = [item.kind for item in scanner.diff(modified, duplicated)]
            self.assertIn("modified", kinds)
            self.assertIn("unchanged", kinds)


if __name__ == "__main__":
    unittest.main()
