"""Compatibility at the native FreeCAD boundary; secure paths stay canonical."""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import sys


def native_process_path(value: str | Path, *, platform: str | None = None) -> str:
    """Use DOS drive spelling for FreeCAD/Qt without accepting network paths.

    Win32 extended paths are valid for the secure filesystem but FreeCAD/Qt's
    resource discovery does not reliably understand that spelling. Refuse names
    whose meaning would change on removing the prefix, rather than redirecting
    them to another file. This does not modify stored paths or filesystem checks.
    """
    spelling = str(value)
    if (platform or sys.platform) != "win32":
        return spelling
    if spelling.startswith("\\\\?\\"):
        local = spelling[4:]
        if len(local) < 3 or not local[0].isascii() or not local[0].isalpha() or local[1:3] != ":\\":
            raise ValueError("FreeCAD requires a local absolute drive path")
        if any(part in {".", ".."} or part.endswith((".", " ")) for part in local[3:].split("\\") if part):
            raise ValueError("extended path cannot be represented safely for FreeCAD")
        return local
    if spelling.replace("/", "\\").startswith("\\\\"):
        raise ValueError("FreeCAD requires local paths, not network or device paths")
    return spelling


def freecad_environment(
    executable: str | Path,
    controlled_directory: str | Path,
    *,
    platform: str | None = None,
) -> dict[str, str]:
    """Isolate user state while keeping the selected Windows runtime loadable."""
    selected_platform = platform or sys.platform
    home = native_process_path(controlled_directory, platform=selected_platform)
    environment = {
        "HOME": home, "TMPDIR": home, "TMP": home, "TEMP": home,
        "USERPROFILE": home, "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    }
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    if selected_platform == "win32":
        command = PureWindowsPath(native_process_path(executable, platform=selected_platform))
        if not command.is_absolute() or not command.drive:
            raise ValueError("FreeCAD executable must use an absolute drive path")
        # Never inherit arbitrary Python/Qt installations, a current-directory
        # PATH entry, or credentials. FreeCAD carries its own runtime beside the
        # selected executable (some distributions use a nested Library/bin).
        directories = [command.parent, command.parent / "Library" / "bin"]
        system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
        if system_root:
            root = PureWindowsPath(native_process_path(system_root, platform=selected_platform))
            if not root.is_absolute() or not root.drive:
                raise ValueError("Windows system root must use an absolute drive path")
            directories.extend((root / "System32", root))
        environment["PATH"] = ";".join(dict.fromkeys(str(path) for path in directories))
        environment.update(FREECAD_USER_HOME=home, FREECAD_USER_DATA=home, FREECAD_USER_TEMP=home)
    return environment
