from __future__ import annotations

import os
from pathlib import Path

from .hashing import file_sha256
from .secure_fs import (
    SecureFilesystemError,
    ensure_managed_directory,
    ingest_cas_file,
    relative_managed_path,
    validate_external_read_path,
    validate_managed_path,
    verify_cas_file,
)


class ArtifactChecksumMismatchError(IOError):
    """A canonical CAS object's bytes no longer match its immutable digest."""


class ArtifactStore:
    """Content-addressed workspace store. Source library files are never changed."""

    def __init__(self, root: Path):
        candidate = Path(os.path.abspath(root))
        self.root = ensure_managed_directory(
            candidate,
            parents=True,
            exist_ok=True,
        ).path

    def path_for(self, sha256: str, suffix: str = "") -> Path:
        safe_suffix = suffix if suffix.startswith(".") and len(suffix) <= 12 else ""
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}{safe_suffix.lower()}"

    @staticmethod
    def _validate_digest(expected_sha256: str) -> str:
        normalized = expected_sha256.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("expected artifact SHA-256 is invalid")
        return normalized

    @staticmethod
    def _checksum_error(exc: SecureFilesystemError) -> None:
        if exc.code == "ARTIFACT_CHECKSUM_MISMATCH":
            raise ArtifactChecksumMismatchError(
                "content-addressed artifact checksum mismatch"
            ) from exc
        if exc.code == "WINDOWS_REPARSE_POINT_BLOCKED":
            raise SecureFilesystemError(
                "ARTIFACT_TARGET_INVALID",
                "content-addressed artifact must be a stable regular file",
            ) from exc
        raise exc

    def verify_file(self, path: Path, expected_sha256: str) -> dict[str, object]:
        """Verify one canonical immutable CAS object through the platform facade."""
        expected = self._validate_digest(expected_sha256)
        source_path = Path(os.path.abspath(path))
        expected_path = self.path_for(expected, source_path.suffix)
        if source_path != expected_path:
            raise ValueError("artifact path is not canonical for the expected SHA-256")
        try:
            actual_sha256, size_bytes = verify_cas_file(source_path, expected)
        except SecureFilesystemError as exc:
            self._checksum_error(exc)
            raise AssertionError("unreachable")
        return {
            "sha256": actual_sha256,
            "size_bytes": size_bytes,
            "storage_path": str(source_path),
            "suffix": source_path.suffix,
        }

    @staticmethod
    def _source_path(source: Path, allowed_root: Path | None) -> Path:
        if allowed_root is None:
            resolved = validate_external_read_path(source)
        else:
            lexical_source = Path(os.path.abspath(source))
            try:
                resolved = validate_managed_path(
                    lexical_source,
                    allow_missing_leaf=False,
                ).path
                relative_managed_path(resolved, allowed_root)
            except SecureFilesystemError as exc:
                raise SecureFilesystemError(
                    exc.code,
                    "artifact source is missing, unstable, or outside the allowed workspace",
                ) from exc
            except ValueError as exc:
                raise ValueError(
                    "artifact source must remain inside the allowed workspace"
                ) from exc
        if not resolved.is_file():
            raise ValueError(f"artifact source is not a regular file: {resolved}")
        return resolved

    def ingest_file(
        self,
        source: Path,
        *,
        allowed_root: Path | None = None,
    ) -> dict[str, object]:
        source_path = self._source_path(source, allowed_root)
        digest = file_sha256(source_path)
        target = self.path_for(digest, source_path.suffix)
        try:
            actual_sha256, size_bytes = ingest_cas_file(
                source_path,
                target,
                digest,
                allowed_source_root=allowed_root,
            )
        except SecureFilesystemError as exc:
            self._checksum_error(exc)
            raise AssertionError("unreachable")
        return {
            "sha256": actual_sha256,
            "size_bytes": size_bytes,
            "source_path": str(source_path),
            "storage_path": str(target),
            "suffix": source_path.suffix,
        }
