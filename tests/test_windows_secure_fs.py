from __future__ import annotations

import hashlib
import importlib
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="native Windows secure filesystem contract"
)


@pytest.fixture
def native_ntfs_roots(tmp_path: Path):
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    raw_second = os.environ.get("MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT", "").strip()
    assert raw_second, "native Windows acceptance requires an explicit second NTFS root"

    system_parent = secure_fs.validate_managed_path(
        tmp_path, allow_missing_leaf=False
    )
    second_parent = secure_fs.validate_managed_path(
        Path(raw_second), allow_missing_leaf=False
    )
    assert system_parent.identity is not None
    assert second_parent.identity is not None
    assert system_parent.identity.volume != second_parent.identity.volume, (
        "MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT must be on a distinct fixed NTFS "
        "volume"
    )

    roots: list[Path] = []
    for parent, label in (
        (system_parent.path, "system"),
        (second_parent.path, "second"),
    ):
        root = parent / f"mechanical-design-w1-{label}-{uuid.uuid4().hex} 空间"
        roots.append(
            secure_fs.ensure_managed_directory(
                root, parents=False, exist_ok=False
            ).path
        )
    try:
        yield tuple(roots)
    finally:
        for root in roots:
            secure_fs.remove_owned_tree(
                root,
                expected_parent=root.parent,
                label="native Windows W1 fixture",
            )


def _cas_worker(source: str, destination: str, digest: str, queue) -> None:
    try:
        secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
        queue.put(
            (
                "ok",
                secure_fs.ingest_cas_file(
                    Path(source),
                    Path(destination),
                    digest,
                    allowed_source_root=Path(source).parent,
                ),
            )
        )
    except BaseException as exc:  # pragma: no cover - reported to parent process
        queue.put(("error", repr(exc)))


def _hold_lock(path: str, ready: multiprocessing.synchronize.Event) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    with secure_fs.exclusive_file_lock(Path(path)):
        ready.set()
        time.sleep(1.0)


def test_native_windows_paths_are_canonical_identity_aware_and_unicode(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for root in native_ntfs_roots:
        managed = secure_fs.validate_managed_path(root, allow_missing_leaf=False)
        alias = secure_fs.validate_managed_path(
            Path(str(root).swapcase()), allow_missing_leaf=False
        )
        assert managed.identity == alias.identity
        assert str(managed.path).startswith("\\\\?\\")
        assert managed.path.name.endswith("空间")

        missing = secure_fs.validate_managed_path(
            root / "missing" / "nested" / "workspace",
            allow_missing_leaf=True,
        )
        assert missing.identity is None
        assert missing.path.name == "workspace"


def test_native_windows_create_replace_cas_and_cleanup_on_both_volumes(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for root in native_ntfs_roots:
        created = root / "created.json"
        secure_fs.atomic_publish_new(created, b'{"version":1}\n')
        assert created.read_bytes() == b'{"version":1}\n'
        with pytest.raises(FileExistsError):
            secure_fs.atomic_publish_new(created, b"replacement forbidden")

        secure_fs.atomic_replace(created, b'{"version":2}\n')
        assert created.read_bytes() == b'{"version":2}\n'

        initialization_lock = root / ".mechanical-design-init.lock"
        with secure_fs.exclusive_creation_lock(initialization_lock):
            assert initialization_lock.is_file()
            with pytest.raises(FileExistsError):
                with secure_fs.exclusive_creation_lock(initialization_lock):
                    pytest.fail("conflicting initializer entered the critical section")
        assert not initialization_lock.exists()

        source = root / "source.bin"
        source.write_bytes(b"windows-cas-fixture")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = root / "cas" / digest[:2] / digest[2:4] / digest
        actual, size = secure_fs.ingest_cas_file(
            source,
            destination,
            digest,
            allowed_source_root=root,
        )
        assert (actual, size) == (digest, len(b"windows-cas-fixture"))
        assert secure_fs.verify_cas_file(destination, digest) == (actual, size)
        os.chmod(destination, 0o666)
        destination.write_bytes(b"legacy corrupt writable object")
        repaired = secure_fs.ingest_cas_file(
            source,
            destination,
            digest,
            allowed_source_root=root,
        )
        assert repaired == (digest, len(b"windows-cas-fixture"))
        assert secure_fs.verify_cas_file(destination, digest) == repaired

        os.chmod(destination, 0o666)
        destination.write_bytes(b"immutable corrupt object")
        os.chmod(destination, 0o444)
        with pytest.raises(secure_fs.SecureFilesystemError) as captured:
            secure_fs.ingest_cas_file(
                source,
                destination,
                digest,
                allowed_source_root=root,
            )
        assert captured.value.code == "ARTIFACT_CHECKSUM_MISMATCH"
        assert destination.read_bytes() == b"immutable corrupt object"

        cleanup = secure_fs.ensure_managed_directory(
            root / "owned-cleanup", parents=False, exist_ok=False
        ).path
        (cleanup / "readonly.bin").write_bytes(b"owned")
        os.chmod(cleanup / "readonly.bin", 0o444)
        secure_fs.remove_owned_tree(
            cleanup, expected_parent=root, label="native cleanup regression"
        )
        assert not cleanup.exists()


def test_native_windows_concurrent_cas_publishers_converge_without_temporaries(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    context = multiprocessing.get_context("spawn")
    for root in native_ntfs_roots:
        source = root / "concurrent-source.bin"
        source.write_bytes(b"concurrent Windows CAS")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = root / "concurrent-cas" / digest[:2] / digest[2:4] / digest
        queue = context.Queue()
        processes = [
            context.Process(
                target=_cas_worker,
                args=(str(source), str(destination), digest, queue),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        expected = ("ok", (digest, len(b"concurrent Windows CAS")))
        assert results == [expected, expected]
        assert secure_fs.verify_cas_file(destination, digest)[0] == digest
        assert list((root / "concurrent-cas").rglob(".artifact.*")) == []


@pytest.mark.parametrize("boundary", ("copy", "flush", "publish", "readonly"))
def test_native_windows_cas_failure_cleans_owned_temporary(
    native_ntfs_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for index, root in enumerate(native_ntfs_roots):
        source = root / f"failure-{boundary}-{index}.bin"
        source.write_bytes(f"failure-{boundary}".encode())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = root / f"failure-{boundary}" / digest[:2] / digest[2:4] / digest

        with monkeypatch.context() as scoped:
            if boundary == "copy":
                scoped.setattr(
                    backend,
                    "_digest_handle",
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("injected copy failure")
                    ),
                )
            elif boundary == "flush":
                scoped.setattr(
                    backend.os,
                    "fsync",
                    lambda descriptor: (_ for _ in ()).throw(
                        OSError("injected flush failure")
                    ),
                )
            elif boundary == "publish":
                scoped.setattr(
                    backend,
                    "_move",
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("injected publish failure")
                    ),
                )
            else:
                api = backend.load_win32_api()
                scoped.setattr(
                    api.win32file,
                    "SetFileAttributes",
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("injected readonly failure")
                    ),
                )
            with pytest.raises(OSError, match=f"injected {boundary} failure"):
                secure_fs.ingest_cas_file(
                    source,
                    destination,
                    digest,
                    allowed_source_root=root,
                )
        assert list(root.rglob(".artifact.*")) == []


def test_native_windows_cas_reopen_and_cleanup_failures_are_not_hidden(
    native_ntfs_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for index, root in enumerate(native_ntfs_roots):
        source = root / f"reopen-{index}.bin"
        source.write_bytes(b"reopen boundary")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = root / "reopen" / digest[:2] / digest[2:4] / digest
        original_verify = backend.verify_cas_file

        with monkeypatch.context() as scoped:
            scoped.setattr(
                backend,
                "verify_cas_file",
                lambda path, expected: (_ for _ in ()).throw(
                    OSError("injected reopen failure")
                ),
            )
            with pytest.raises(OSError, match="injected reopen failure"):
                secure_fs.ingest_cas_file(
                    source,
                    destination,
                    digest,
                    allowed_source_root=root,
                )
        assert destination.exists()
        assert original_verify(destination, digest)[0] == digest

        cleanup_source = root / f"cleanup-{index}.bin"
        cleanup_source.write_bytes(b"cleanup boundary")
        cleanup_digest = hashlib.sha256(cleanup_source.read_bytes()).hexdigest()
        cleanup_destination = (
            root / "cleanup" / cleanup_digest[:2] / cleanup_digest[2:4] / cleanup_digest
        )

        def fail_publish_then_cleanup(*args, **kwargs):
            raise OSError("injected publish body failure")

        with monkeypatch.context() as scoped:
            api = backend.load_win32_api()
            scoped.setattr(api.win32file, "SetFileAttributes", lambda *args: None)
            scoped.setattr(backend, "_move", fail_publish_then_cleanup)
            scoped.setattr(
                backend,
                "_clear_readonly",
                lambda path, api: (_ for _ in ()).throw(
                    OSError("injected cleanup failure")
                ),
            )
            with pytest.raises(OSError, match="injected cleanup failure"):
                secure_fs.ingest_cas_file(
                    cleanup_source,
                    cleanup_destination,
                    cleanup_digest,
                    allowed_source_root=root,
                )
        assert list(root.rglob(".artifact.*")) == []


def test_native_windows_rejects_final_and_intermediate_junctions(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for root in native_ntfs_roots:
        target = root / "junction-target"
        target.mkdir()
        junction = root / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        try:
            for candidate in (junction, junction / "nested"):
                with pytest.raises(Exception) as captured:
                    secure_fs.validate_managed_path(
                        candidate, allow_missing_leaf=candidate != junction
                    )
                assert captured.value.code == "WINDOWS_REPARSE_POINT_BLOCKED"
        finally:
            junction.rmdir()


def test_native_windows_atomic_replace_detects_identity_swap(
    native_ntfs_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for root in native_ntfs_roots:
        target = root / "identity.json"
        target.write_bytes(b"original")
        displaced = root / "displaced.json"
        original = backend._require_regular
        calls = 0

        def swap_on_recheck(path: Path, api):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.replace(displaced)
                path.write_bytes(b"attacker replacement")
            return original(path, api)

        with monkeypatch.context() as scoped:
            scoped.setattr(backend, "_require_regular", swap_on_recheck)
            with pytest.raises(Exception) as captured:
                secure_fs.atomic_replace(target, b"new managed bytes")
        assert captured.value.code == "WINDOWS_PATH_IDENTITY_CHANGED"
        assert target.read_bytes() == b"attacker replacement"


def test_native_windows_cleanup_rejects_junction_and_preserves_external_tree(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for root in native_ntfs_roots:
        external = root / "external-victim"
        external.mkdir()
        victim = external / "victim.txt"
        victim.write_text("preserve", encoding="utf-8")
        owned = root / "owned-with-junction"
        owned.mkdir()
        junction = owned / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        with pytest.raises(Exception) as captured:
            secure_fs.remove_owned_tree(
                owned, expected_parent=root, label="junction cleanup regression"
            )
        assert captured.value.code == "WINDOWS_REPARSE_POINT_BLOCKED"
        assert victim.read_text(encoding="utf-8") == "preserve"
        junction.rmdir()
        secure_fs.remove_owned_tree(
            owned, expected_parent=root, label="junction cleanup fixture"
        )


def test_native_windows_lock_serializes_processes_on_both_volumes(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    context = multiprocessing.get_context("spawn")
    for root in native_ntfs_roots:
        lock = root / ".working-copy.lock"
        ready = context.Event()
        process = context.Process(target=_hold_lock, args=(str(lock), ready))
        process.start()
        assert ready.wait(timeout=10)
        started = time.monotonic()
        with secure_fs.exclusive_file_lock(lock):
            elapsed = time.monotonic() - started
        process.join(timeout=10)
        assert process.exitcode == 0
        assert elapsed >= 0.75


def test_native_windows_job_containment_rejects_junctions(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    jobs = importlib.import_module("mechanical_design_agent.jobs")
    for root in native_ntfs_roots:
        job_root = root / "JOB-20260823-001-unicode-空间"
        job_root.mkdir()
        analysis = job_root / "analysis"
        analysis.mkdir()
        external = root / "external-job-victim"
        external.mkdir()
        junction = analysis / "escape"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        try:
            with pytest.raises(jobs.JobFailure) as captured:
                jobs.managed_job_path(
                    job_root=job_root,
                    relative_path="analysis/escape/model.FCStd",
                    allow_missing_leaf=True,
                )
            assert captured.value.code == "JOB_PATH_UNSAFE"
        finally:
            junction.rmdir()


def test_native_windows_pinned_managed_reads_and_enumeration_reject_reparse_points(
    native_ntfs_roots: tuple[Path, Path],
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    for root in native_ntfs_roots:
        managed = root / "pinned"
        managed.mkdir()
        payload = managed / "manifest.json"
        payload.write_bytes(b'{"schema_version":"test"}\n')
        read = secure_fs.read_managed_file(payload)
        assert read.content == b'{"schema_version":"test"}\n'
        assert read.sha256 == hashlib.sha256(read.content).hexdigest()
        assert read.identity is not None
        assert [entry.name for entry in secure_fs.list_managed_directory(managed)] == [
            "manifest.json"
        ]

        external = root / "pinned-external"
        external.mkdir()
        junction = managed / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        try:
            with pytest.raises(Exception) as captured:
                secure_fs.list_managed_directory(managed)
            assert captured.value.code == "WINDOWS_REPARSE_POINT_BLOCKED"
        finally:
            junction.rmdir()
