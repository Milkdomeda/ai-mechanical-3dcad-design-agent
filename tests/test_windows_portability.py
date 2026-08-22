from __future__ import annotations

import ast
from contextlib import nullcontext
import importlib
import os
from pathlib import Path, PureWindowsPath
import sys
import tomllib
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "mechanical_design_agent"


class _FakeWin32File:
    def __init__(self, *, drive_type: int = 3):
        self.drive_type = drive_type

    def GetDriveType(self, root: str) -> int:
        assert root
        return self.drive_type


class _FakeWin32Api:
    def __init__(self, *, drive_type: int = 3, filesystem: str = "NTFS"):
        self.win32con = SimpleNamespace(
            DRIVE_FIXED=3,
            DRIVE_REMOTE=4,
            FILE_ATTRIBUTE_REPARSE_POINT=0x400,
        )
        self.win32file = _FakeWin32File(drive_type=drive_type)
        self.win32api = SimpleNamespace(
            GetVolumeInformation=lambda root: ("", 0, 0, 0, filesystem)
        )


def test_package_has_no_unconditional_posix_only_imports() -> None:
    violations: dict[str, set[str]] = {}
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        if path.name == "secure_fs_posix.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = imported & {"fcntl", "pwd", "grp", "termios", "resource"}
        if forbidden:
            violations[path.name] = forbidden
    assert violations == {}


def test_windows_backend_rejects_network_path_spelling_before_io() -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    for value in (r"\\server\share\workspace", r"//server/share/workspace"):
        with pytest.raises(Exception) as captured:
            windows.reject_network_path_spelling(value)
        assert captured.value.code == "WINDOWS_NETWORK_PATH_BLOCKED"


def test_windows_backend_reports_missing_pywin32_structurally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")

    def unavailable(_: str):
        raise ModuleNotFoundError("simulated missing pywin32")

    monkeypatch.setattr(windows.importlib, "import_module", unavailable)
    with pytest.raises(Exception) as captured:
        windows.load_win32_api()
    assert captured.value.code == "WINDOWS_SECURE_FS_UNAVAILABLE"


def test_windows_backend_reports_missing_required_api_structurally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    monkeypatch.setattr(
        windows.importlib,
        "import_module",
        lambda name: SimpleNamespace(),
    )
    with pytest.raises(Exception) as captured:
        windows.load_win32_api()
    assert captured.value.code == "WINDOWS_SECURE_FS_UNAVAILABLE"


def test_windows_create_new_move_maps_existing_target_without_replacing() -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")

    class AlreadyExists(OSError):
        winerror = 183

    api = SimpleNamespace(
        win32con=SimpleNamespace(
            MOVEFILE_WRITE_THROUGH=0x8,
            MOVEFILE_REPLACE_EXISTING=0x1,
        ),
        win32file=SimpleNamespace(
            MoveFileEx=lambda source, target, flags: (_ for _ in ()).throw(
                AlreadyExists()
            )
        ),
    )
    with pytest.raises(FileExistsError):
        windows._move(Path("source"), Path("target"), replace=False, api=api)


@pytest.mark.parametrize(
    ("drive_type", "filesystem", "code"),
    (
        (4, "NTFS", "WINDOWS_NETWORK_PATH_BLOCKED"),
        (2, "NTFS", "WINDOWS_VOLUME_UNSUPPORTED"),
        (3, "ReFS", "WINDOWS_VOLUME_UNSUPPORTED"),
    ),
)
def test_windows_volume_policy_is_fail_closed(
    drive_type: int, filesystem: str, code: str
) -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    api = _FakeWin32Api(drive_type=drive_type, filesystem=filesystem)
    with pytest.raises(Exception) as captured:
        windows._validate_volume(PureWindowsPath("C:/workspace"), api)
    assert captured.value.code == code


def test_windows_fixed_ntfs_volume_is_accepted() -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    root = windows._validate_volume(
        PureWindowsPath("C:/workspace"), _FakeWin32Api()
    )
    assert str(root).casefold().startswith("c:")


def test_windows_reparse_attribute_is_rejected() -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    api = _FakeWin32Api()
    with pytest.raises(Exception) as captured:
        windows._assert_not_reparse(0x400, api)
    assert captured.value.code == "WINDOWS_REPARSE_POINT_BLOCKED"


def test_windows_file_identity_uses_volume_and_full_file_index() -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    information = (0, None, None, None, 17, 0, 0, 1, 2, 3)
    api = _FakeWin32Api()
    api.win32file.GetFileInformationByHandle = lambda handle: information
    identity, attributes = windows._handle_facts(object(), api)
    assert attributes == 0
    assert identity.volume == 17
    assert identity.file_index == (2 << 32) | 3


def test_windows_creation_lock_cleanup_uses_pinned_handle_without_reopening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    events: list[str] = []

    class Handle:
        def Close(self) -> None:
            events.append("close")

    handle = Handle()
    identity = secure_fs.FileIdentity(volume=17, file_index=23)

    def create_file(*args, **kwargs):
        events.append("create")
        return handle

    def set_file_information(actual_handle, information_class, delete: bool):
        assert actual_handle is handle
        assert information_class == 13
        assert delete is True
        events.append("disposition")

    api = SimpleNamespace(
        win32con=SimpleNamespace(
            GENERIC_READ=0x80000000,
            GENERIC_WRITE=0x40000000,
            DELETE=0x00010000,
            FILE_SHARE_READ=1,
            FILE_SHARE_WRITE=2,
            CREATE_NEW=1,
            FILE_ATTRIBUTE_NORMAL=0x80,
            FILE_FLAG_OPEN_REPARSE_POINT=0x00200000,
        ),
        win32file=SimpleNamespace(
            FileDispositionInfo=13,
            CreateFile=create_file,
            SetFileInformationByHandle=set_file_information,
        ),
    )
    parent = SimpleNamespace(path=Path("C:/workspace"))
    monkeypatch.setattr(windows, "load_win32_api", lambda: api)
    monkeypatch.setattr(windows, "_absolute", lambda path: Path(path))
    monkeypatch.setattr(
        windows,
        "_pinned_path",
        lambda path, *, allow_missing_leaf: nullcontext(parent),
    )
    monkeypatch.setattr(
        windows,
        "_handle_facts",
        lambda actual_handle, actual_api: (identity, 0),
    )
    monkeypatch.setattr(windows, "_assert_not_reparse", lambda *args: None)

    def sharing_violation_if_reopened(path: Path, actual_api):
        raise AssertionError(
            "creation-lock cleanup must not reopen its pinned lock path"
        )

    monkeypatch.setattr(windows, "_require_regular", sharing_violation_if_reopened)

    lock = Path("C:/workspace/.mechanical-design-init.lock")
    with windows.exclusive_creation_lock(lock):
        events.append("yield")

    assert events == ["create", "yield", "disposition", "close"]


def test_windows_owned_tree_cleanup_unlinks_inner_file_reparse_without_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows = importlib.import_module("mechanical_design_agent.secure_fs_windows")
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    parent = tmp_path / "parent"
    target = parent / "owned-attempt"
    external = tmp_path / "external.txt"
    target.mkdir(parents=True)
    external.write_text("preserve\n", encoding="utf-8")
    inner_link = target / "immutable.json"
    inner_link.symlink_to(external)

    api = SimpleNamespace(
        win32con=SimpleNamespace(
            FILE_ATTRIBUTE_REPARSE_POINT=0x400,
            FILE_ATTRIBUTE_DIRECTORY=0x10,
        )
    )

    def entry_facts(path: Path, actual_api, *, directory: bool):
        del actual_api, directory
        metadata = Path(path).lstat()
        attributes = 0
        if Path(path).is_symlink():
            attributes |= 0x400
        elif Path(path).is_dir():
            attributes |= 0x10
        return secure_fs.FileIdentity(17, metadata.st_ino), attributes

    def identity(path: Path, actual_api, *, directory: bool):
        file_identity, attributes = entry_facts(
            path,
            actual_api,
            directory=directory,
        )
        windows._assert_not_reparse(attributes, api)
        return file_identity

    def require_regular(path: Path, actual_api):
        file_identity, attributes = entry_facts(
            path,
            actual_api,
            directory=False,
        )
        windows._assert_not_reparse(attributes, api)
        return file_identity

    pinned_parent = SimpleNamespace(
        path=parent,
        identity=secure_fs.FileIdentity(17, parent.stat().st_ino),
    )
    monkeypatch.setattr(windows, "load_win32_api", lambda: api)
    monkeypatch.setattr(windows, "_absolute", lambda path: Path(path))
    monkeypatch.setattr(
        windows,
        "_pinned_path",
        lambda path, *, allow_missing_leaf: nullcontext(pinned_parent),
    )
    monkeypatch.setattr(windows, "_entry_facts", entry_facts, raising=False)
    monkeypatch.setattr(windows, "_identity", identity)
    monkeypatch.setattr(windows, "_require_regular", require_regular)
    monkeypatch.setattr(windows, "_clear_readonly", lambda *args: None)

    windows.remove_owned_tree(
        target,
        expected_parent=parent,
        label="synthetic owned attempt",
    )

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert not target.exists()


def test_pywin32_is_a_conditional_direct_runtime_dependency() -> None:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "pywin32>=312; sys_platform == 'win32'" in pyproject["project"][
        "dependencies"
    ]
    lock = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert '{ name = "pywin32", marker = "sys_platform == \'win32\'" }' in lock


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows contract")
def test_native_windows_accepts_managed_path_with_spaces_and_unicode(
    tmp_path: Path,
) -> None:
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    root = tmp_path / "Mechanical Design 空间"
    managed = secure_fs.validate_managed_path(root, allow_missing_leaf=True)
    assert managed.path.name == "Mechanical Design 空间"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows contract")
def test_native_windows_requires_second_fixed_ntfs_volume() -> None:
    raw = os.environ.get("MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT", "").strip()
    assert raw, "native Windows acceptance requires an explicit second NTFS root"
    secure_fs = importlib.import_module("mechanical_design_agent.secure_fs")
    managed = secure_fs.validate_managed_path(Path(raw), allow_missing_leaf=False)
    assert managed.path.is_absolute()
