from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .hashing import file_sha256
from .models import ScanChange, ScanEntry


SUPPORTED_SUFFIXES = {".step", ".stp", ".fcstd"}
IGNORED_SUFFIXES = {".tmp", ".bak", ".part", ".swp", ".download"}


def _hidden_or_temporary(relative: Path) -> bool:
    if any(part.startswith(".") for part in relative.parts):
        return True
    lowered = relative.name.lower()
    if lowered.startswith("~$") or lowered.endswith("~"):
        return True
    return relative.suffix.lower() in IGNORED_SUFFIXES


class LibraryScanner:
    def register(self, root_path: str | Path) -> Path:
        root = Path(root_path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"CAD library root is not a directory: {root}")
        if not os.access(root, os.R_OK | os.X_OK):
            raise PermissionError(f"CAD library root is not readable: {root}")
        return root

    def inventory(self, root: Path) -> list[ScanEntry]:
        root = self.register(root)
        entries: list[ScanEntry] = []
        for candidate in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
            if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            relative = candidate.relative_to(root)
            if _hidden_or_temporary(relative):
                continue
            if len(relative.parts) < 2:
                # Every imported model must be explicitly placed in a first-level family folder.
                continue
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError:
                # A symlink may point outside the engineer-registered read-only root.
                continue
            stat = candidate.stat()
            entries.append(
                ScanEntry(
                    relative_path=relative.as_posix(),
                    absolute_path=str(resolved),
                    family_folder=relative.parts[0],
                    sha256=file_sha256(candidate),
                    size_bytes=stat.st_size,
                    modified_at_ns=stat.st_mtime_ns,
                    suffix=candidate.suffix,
                )
            )
        return entries

    def diff(
        self,
        current: Iterable[ScanEntry],
        previous: Iterable[ScanEntry | dict[str, object]],
    ) -> list[ScanChange]:
        current_list = list(current)
        previous_list = [item if isinstance(item, ScanEntry) else ScanEntry(**item) for item in previous]
        current_by_path = {item.relative_path: item for item in current_list}
        previous_by_path = {item.relative_path: item for item in previous_list}
        previous_by_sha: dict[str, list[ScanEntry]] = {}
        current_by_sha: dict[str, list[ScanEntry]] = {}
        for item in previous_list:
            previous_by_sha.setdefault(item.sha256, []).append(item)
        for item in current_list:
            current_by_sha.setdefault(item.sha256, []).append(item)

        changes: list[ScanChange] = []
        for path, entry in current_by_path.items():
            old = previous_by_path.get(path)
            if old and old.sha256 == entry.sha256:
                changes.append(ScanChange("unchanged", entry))
                continue
            if old:
                changes.append(ScanChange("modified", entry, path, "same path has new content hash"))
                continue
            prior_same = previous_by_sha.get(entry.sha256, [])
            if prior_same:
                old_path = prior_same[0].relative_path
                kind = "renamed" if old_path not in current_by_path else "duplicate"
                changes.append(ScanChange(kind, entry, old_path, "content hash already known"))
            else:
                changes.append(ScanChange("new", entry))

        for path, old in previous_by_path.items():
            if path in current_by_path:
                continue
            if any(item.relative_path != path for item in current_by_sha.get(old.sha256, [])):
                continue
            changes.append(ScanChange("missing", None, path, "previously indexed path no longer exists"))
        order = {"new": 0, "modified": 1, "renamed": 2, "duplicate": 3, "missing": 4, "unchanged": 5}
        return sorted(changes, key=lambda item: (order[item.kind], item.entry.relative_path if item.entry else item.previous_path or ""))


def scan_entry_dict(entry: ScanEntry) -> dict[str, object]:
    return asdict(entry)


def scan_change_dict(change: ScanChange) -> dict[str, object]:
    value = asdict(change)
    return value
