from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from .secure_fs import FileIdentity, SecureFilesystemError, read_managed_file, validate_managed_path


class FreeCADExecutableTrustError(RuntimeError):
    """The reviewed FreeCAD executable changed or no longer matches its digest."""


def _pin_executable(
    path: Path,
    *,
    expected_sha256: str,
    expected_identity: FileIdentity,
) -> None:
    try:
        pinned = read_managed_file(path)
    except (OSError, SecureFilesystemError) as exc:
        raise FreeCADExecutableTrustError(
            "reviewed FreeCAD executable cannot be pinned"
        ) from exc
    if (
        pinned.sha256 != expected_sha256
        or pinned.identity != expected_identity
        or pinned.link_count != 1
    ):
        raise FreeCADExecutableTrustError(
            "reviewed FreeCAD executable identity or SHA-256 changed"
        )


def _scrubbed_environment(controlled_directory: Path) -> dict[str, str]:
    environment: dict[str, str] = {
        "HOME": str(controlled_directory),
        "TMPDIR": str(controlled_directory),
        "TMP": str(controlled_directory),
        "TEMP": str(controlled_directory),
        "USERPROFILE": str(controlled_directory),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def run_freecad_script(
    freecadcmd: Path,
    script: Path,
    arguments: Sequence[str | Path],
    *,
    timeout_seconds: int,
    expected_sha256: str | None = None,
    expected_identity: FileIdentity | None = None,
    controlled_directory: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a Python file in FreeCAD's console with an explicit argv.

    FreeCAD 1.1 treats trailing command-line paths as documents. Feeding one
    bounded exec statement to console mode avoids that ambiguity and keeps the
    script arguments out of FreeCAD's document-opening path.
    """
    if expected_sha256 is None or expected_identity is None or controlled_directory is None:
        raise FreeCADExecutableTrustError(
            "FreeCAD execution requires a reviewed SHA-256 and pinned identity"
        )
    controlled = validate_managed_path(
        controlled_directory, allow_missing_leaf=False
    ).path
    _pin_executable(
        freecadcmd,
        expected_sha256=expected_sha256,
        expected_identity=expected_identity,
    )
    command_argv = [str(script), *(str(item) for item in arguments)]
    runner = (
        "import sys, traceback\n"
        f"sys.argv = {command_argv!r}\n"
        f"_script = {str(script)!r}\n"
        "try:\n"
        "    exec(compile(open(_script, 'rb').read(), _script, 'exec'), {'__name__': '__main__'})\n"
        "except BaseException:\n"
        "    traceback.print_exc()\n"
        "    raise\n"
    )
    try:
        return subprocess.run(
            [str(freecadcmd), "-c"],
            input=f"exec({runner!r})\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=controlled,
            env=_scrubbed_environment(controlled),
        )
    finally:
        _pin_executable(
            freecadcmd,
            expected_sha256=expected_sha256,
            expected_identity=expected_identity,
        )
