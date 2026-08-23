from __future__ import annotations

from contextlib import contextmanager
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mechanical_design_agent.secure_fs import (
    FileIdentity,
    SecureFilesystemError,
    remove_owned_directory_exact,
    set_managed_file_readonly,
    read_managed_file,
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX exact cleanup contract")
def test_exact_owned_cleanup_rejects_unknown_or_linked_descendants(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "attempts"
    parent.mkdir()
    attempt = parent / "owned"
    attempt.mkdir()
    receipt = attempt / ".binding-attempt.json"
    receipt.write_bytes(b"receipt")
    unknown = attempt / "unexpected.bin"
    unknown.write_bytes(b"unowned")

    with pytest.raises(SecureFilesystemError, match="unexpected inventory"):
        remove_owned_directory_exact(
            attempt,
            expected_parent=parent,
            allowed_files={".binding-attempt.json", "working.FCStd"},
            label="binding attempt",
        )
    assert receipt.read_bytes() == b"receipt"
    assert unknown.read_bytes() == b"unowned"

    unknown.unlink()
    outside = tmp_path / "outside.FCStd"
    outside.write_bytes(b"outside")
    (attempt / "working.FCStd").symlink_to(outside)
    with pytest.raises(SecureFilesystemError):
        remove_owned_directory_exact(
            attempt,
            expected_parent=parent,
            allowed_files={".binding-attempt.json", "working.FCStd"},
            label="binding attempt",
        )
    assert outside.read_bytes() == b"outside"
    assert attempt.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow chmod contract")
def test_readonly_mutation_is_pinned_no_follow_and_closes_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_posix")
    target = tmp_path / "snapshot.FCStd"
    target.write_bytes(b"snapshot")
    set_managed_file_readonly(target)
    assert target.stat().st_mode & 0o222 == 0

    outside = tmp_path / "outside.FCStd"
    outside.write_bytes(b"outside")
    linked = tmp_path / "linked.FCStd"
    linked.symlink_to(outside)
    with pytest.raises(SecureFilesystemError):
        set_managed_file_readonly(linked)
    assert outside.stat().st_mode & 0o200

    target.chmod(0o600)
    real_open = backend.os.open
    real_close = backend.os.close
    leaf_descriptors: list[int] = []
    closed: list[int] = []

    def tracked_open(path, *args, **kwargs):
        descriptor = real_open(path, *args, **kwargs)
        if path == target.name:
            leaf_descriptors.append(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(backend.os, "open", tracked_open)
    monkeypatch.setattr(backend.os, "close", tracked_close)
    monkeypatch.setattr(
        backend.os,
        "fchmod",
        lambda *_args: (_ for _ in ()).throw(OSError("injected chmod failure")),
    )
    with pytest.raises(SecureFilesystemError, match="read-only"):
        set_managed_file_readonly(target)
    assert leaf_descriptors
    assert set(leaf_descriptors) <= set(closed)


@pytest.mark.skipif(os.name != "posix", reason="POSIX hardlink evidence contract")
def test_pinned_read_reports_link_count_for_exclusive_output_proof(
    tmp_path: Path,
) -> None:
    original = tmp_path / "outside.FCStd"
    original.write_bytes(b"linked bytes")
    linked = tmp_path / "working.FCStd"
    os.link(original, linked)

    assert read_managed_file(linked).link_count == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cleanup contract")
def test_posix_enumeration_closes_a_child_descriptor_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_posix")
    child = tmp_path / "child.bin"
    child.write_bytes(b"owned descriptor")
    real_open = backend.os.open
    real_close = backend.os.close
    real_fstat = backend.os.fstat
    child_descriptors: list[int] = []
    closed: list[int] = []

    def tracked_open(path, *args, **kwargs):
        descriptor = real_open(path, *args, **kwargs)
        if path == child.name:
            child_descriptors.append(descriptor)
        return descriptor

    def fail_child_fstat(descriptor: int):
        if descriptor in child_descriptors:
            raise OSError("injected child fstat failure")
        return real_fstat(descriptor)

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(backend.os, "open", tracked_open)
    monkeypatch.setattr(backend.os, "fstat", fail_child_fstat)
    monkeypatch.setattr(backend.os, "close", tracked_close)

    try:
        with pytest.raises(SecureFilesystemError, match="enumerated safely"):
            backend.list_managed_directory(tmp_path)
        assert child_descriptors
        assert set(child_descriptors) <= set(closed)
    finally:
        for descriptor in set(child_descriptors) - set(closed):
            real_close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cleanup contract")
def test_posix_read_closes_the_leaf_descriptor_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_posix")
    child = tmp_path / "manifest.json"
    child.write_bytes(b"{}\n")
    real_open = backend.os.open
    real_close = backend.os.close
    real_fstat = backend.os.fstat
    leaf_descriptors: list[int] = []
    closed: list[int] = []

    def tracked_open(path, *args, **kwargs):
        descriptor = real_open(path, *args, **kwargs)
        if path == child.name:
            leaf_descriptors.append(descriptor)
        return descriptor

    def fail_leaf_fstat(descriptor: int):
        if descriptor in leaf_descriptors:
            raise OSError("injected leaf fstat failure")
        return real_fstat(descriptor)

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(backend.os, "open", tracked_open)
    monkeypatch.setattr(backend.os, "fstat", fail_leaf_fstat)
    monkeypatch.setattr(backend.os, "close", tracked_close)

    try:
        with pytest.raises(SecureFilesystemError, match="read safely"):
            backend.read_managed_file(child)
        assert leaf_descriptors
        assert set(leaf_descriptors) <= set(closed)
    finally:
        for descriptor in set(leaf_descriptors) - set(closed):
            real_close(descriptor)


def test_windows_enumeration_closes_a_child_handle_when_facts_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    handle = object()
    closed: list[object] = []
    api = SimpleNamespace(
        win32con=SimpleNamespace(
            FILE_ATTRIBUTE_REPARSE_POINT=0x400,
            FILE_ATTRIBUTE_DIRECTORY=0x10,
        )
    )
    entry = SimpleNamespace(
        name="child.bin",
        stat=lambda *, follow_symlinks: SimpleNamespace(st_file_attributes=0),
    )

    class _Scandir:
        def __enter__(self):
            return iter((entry,))

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

    @contextmanager
    def pinned(path: Path, *, allow_missing_leaf: bool):
        yield SimpleNamespace(path=path, identity=FileIdentity(1, 1))

    monkeypatch.setattr(backend, "load_win32_api", lambda: api)
    monkeypatch.setattr(backend, "_absolute", Path)
    monkeypatch.setattr(backend, "_pinned_path", pinned)
    monkeypatch.setattr(backend.os, "scandir", lambda path: _Scandir())
    monkeypatch.setattr(backend, "_open_handle", lambda *args, **kwargs: handle)
    monkeypatch.setattr(
        backend,
        "_handle_facts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected handle facts failure")
        ),
    )
    monkeypatch.setattr(backend, "_close_handle", closed.append)

    with pytest.raises(RuntimeError, match="injected handle facts failure"):
        backend.list_managed_directory(Path("managed"))

    assert closed == [handle]


def test_windows_read_closes_a_leaf_handle_when_facts_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    handle = object()
    closed: list[object] = []
    api = SimpleNamespace()

    @contextmanager
    def pinned(path: Path, *, allow_missing_leaf: bool):
        yield SimpleNamespace(path=path, identity=FileIdentity(1, 1))

    monkeypatch.setattr(backend, "load_win32_api", lambda: api)
    monkeypatch.setattr(backend, "_absolute", Path)
    monkeypatch.setattr(backend, "_pinned_path", pinned)
    monkeypatch.setattr(backend, "_open_handle", lambda *args, **kwargs: handle)
    monkeypatch.setattr(
        backend,
        "_handle_facts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected leaf facts failure")
        ),
    )
    monkeypatch.setattr(backend, "_close_handle", closed.append)

    with pytest.raises(RuntimeError, match="injected leaf facts failure"):
        backend.read_managed_file(Path("managed/manifest.json"))

    assert closed == [handle]
