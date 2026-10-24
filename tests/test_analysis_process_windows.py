from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_analysis_process import CHILD, alive, wait_for


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Job Object contract")
def test_worker_owns_windows_job_not_mcp(tmp_path: Path):
    # The launcher simulates an MCP that exits. Its worker owns the Job.
    worker = tmp_path / "worker.py"
    worker.write_text("import os,time\nfrom pathlib import Path\nfrom mechanical_design_agent.analysis_process import ProcessController,ProcessRequest\n"
        f"root=Path({str(tmp_path)!r})\nc=ProcessController()\n"
        f"c.start(ProcessRequest(argv=({sys.executable!r},{str(CHILD)!r},'tree',str(root)),cwd=root,stdout_path=root/'out',stderr_path=root/'err',env=dict(os.environ)))\n"
        "while not (root/'exit-worker').exists(): time.sleep(.03)\nos._exit(0)\n", encoding="utf-8")
    launcher = subprocess.run([sys.executable, "-c", f"import subprocess; subprocess.Popen([{sys.executable!r},{str(worker)!r}],creationflags=8,close_fds=True)"], timeout=10)
    assert launcher.returncode == 0
    wait_for(lambda: (tmp_path / "grandchild.pid").exists())
    pids = [int((tmp_path / (role + ".pid")).read_text()) for role in ("tree", "leaf", "grandchild")]
    try:
        assert all(alive(pid) for pid in pids)
    finally:
        (tmp_path / "exit-worker").touch()
    wait_for(lambda: all(not alive(pid) for pid in pids))
