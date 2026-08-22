from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


def run_freecad_script(
    freecadcmd: Path,
    script: Path,
    arguments: Sequence[str | Path],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Execute a Python file in FreeCAD's console with an explicit argv.

    FreeCAD 1.1 treats trailing command-line paths as documents. Feeding one
    bounded exec statement to console mode avoids that ambiguity and keeps the
    script arguments out of FreeCAD's document-opening path.
    """
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
    return subprocess.run(
        [str(freecadcmd), "-c"],
        input=f"exec({runner!r})\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
