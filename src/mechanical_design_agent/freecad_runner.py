from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from .secure_fs import FileIdentity, SecureFilesystemError, read_managed_file, validate_managed_path


class FreeCADExecutableTrustError(RuntimeError):
    """The reviewed FreeCAD executable changed or no longer matches its digest."""


_FREECAD_PROGRESS_LABELS = frozenset(
    {
        "Importing project files......\n",
        "Postprocessing......\n",
        "Recompute......\n",
    }
)
_FREECAD_PROGRESS_VALUE = re.compile(
    r"\t{6}\((0|[1-9][0-9]?|100) %\)\t\n\Z"
)
_FREECAD_PROGRESS_CLEAR = "\t" * 8 + "\n"


def _strip_freecad_progress_output(value: str) -> str:
    """Remove only complete, monotonic FreeCAD 1.1 console progress blocks."""
    lines = value.splitlines(keepends=True)
    preserved: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index] not in _FREECAD_PROGRESS_LABELS:
            preserved.append(lines[index])
            index += 1
            continue
        start = index
        index += 1
        percentages: list[int] = []
        while index < len(lines):
            match = _FREECAD_PROGRESS_VALUE.fullmatch(lines[index])
            if match is None:
                break
            percentages.append(int(match.group(1)))
            index += 1
        complete = (
            bool(percentages)
            and percentages[-1] == 100
            and all(left <= right for left, right in zip(percentages, percentages[1:]))
            and index < len(lines)
            and lines[index] == _FREECAD_PROGRESS_CLEAR
        )
        if complete:
            index += 1
            continue
        preserved.extend(lines[start:index])
    return "".join(preserved)


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

    FreeCAD 1.1 treats trailing command-line paths as documents. Passing one
    bounded exec statement as the console command avoids that ambiguity, keeps
    script arguments out of the document-opening path, and avoids interactive
    REPL prompts or banners contaminating the evidence channels.
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
    console_command = f"exec({runner!r})"
    try:
        completed = subprocess.run(
            [str(freecadcmd), "-c", console_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=controlled,
            env=_scrubbed_environment(controlled),
        )
        return subprocess.CompletedProcess(
            args=completed.args,
            returncode=completed.returncode,
            stdout=(
                _strip_freecad_progress_output(completed.stdout)
                if isinstance(completed.stdout, str)
                else completed.stdout
            ),
            stderr=completed.stderr,
        )
    finally:
        _pin_executable(
            freecadcmd,
            expected_sha256=expected_sha256,
            expected_identity=expected_identity,
        )
