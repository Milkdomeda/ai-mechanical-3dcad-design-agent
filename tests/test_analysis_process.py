from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest

from mechanical_design_agent.analysis_process import ProcessController, ProcessRequest


CHILD = Path(__file__).with_name("fea_process_child.py")


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("Owned process fixture did not reach expected state")


def alive(pid):
    if os.name == "nt":
        import pywintypes
        import win32api
        import win32process
        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                return win32process.GetExitCodeProcess(handle) == 259
            finally:
                handle.Close()
        except pywintypes.error as exc:
            if exc.winerror in {87, 1168}:
                return False
            raise
    try:
        os.kill(pid, 0)
        # A terminated unreaped descendant is no longer executing.
        state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")
    except ProcessLookupError:
        return False


def request(tmp_path, argv=None, timeout=30):
    return ProcessRequest(argv=tuple(argv or [sys.executable, str(CHILD), "tree", str(tmp_path)]), cwd=tmp_path, stdout_path=tmp_path / "stdout.log", stderr_path=tmp_path / "stderr.log", env=dict(os.environ), timeout_seconds=timeout)


def test_cancel_kills_owned_child_and_grandchild(tmp_path):
    controller = ProcessController()
    identity = controller.start(request(tmp_path))
    try:
        wait_for(lambda: (tmp_path / "grandchild.pid").exists())
        pids = [int((tmp_path / (role + ".pid")).read_text()) for role in ("tree", "leaf", "grandchild")]
        assert all(alive(pid) for pid in pids)
        outcome = controller.terminate(identity, reason="cancelled")
        assert outcome.reason == "cancelled"
        wait_for(lambda: all(not alive(pid) for pid in pids))
        assert controller.terminate(identity, reason="cancelled") == outcome
    finally:
        controller.terminate(identity, reason="cancelled")


def test_reused_pid_is_never_killed(tmp_path):
    controller = ProcessController()
    identity = controller.start(request(tmp_path))
    try:
        forged = replace(identity, worker_uuid="not-the-owned-worker")
        outcome = controller.terminate(forged, reason="cancelled")
        assert outcome.reason == "identity_mismatch"
        assert controller.poll(identity) is None
    finally:
        controller.terminate(identity, reason="cancelled")


def test_memory_observation_includes_only_the_living_owners_children(tmp_path):
    controller=ProcessController()
    baseline=controller.observed_memory_bytes()
    code="import pathlib,time;block=bytearray(64*1024*1024);block[::4096]=b'x'*(len(block)//4096);pathlib.Path('ready').write_text('ready');time.sleep(30)"
    identity=controller.start(request(tmp_path,argv=[sys.executable,'-I','-B','-c',code]))
    try:
        wait_for(lambda:(tmp_path/'ready').exists())
        measured=controller.observed_memory_bytes()
        assert measured['bytes']>baseline['bytes']+32*1024*1024
        assert measured['measurement']
    finally:controller.terminate(identity,reason='cancelled')
    assert not controller._children


def test_timeout_kills_entire_tree(tmp_path):
    controller = ProcessController()
    identity = controller.start(request(tmp_path, timeout=1.5))
    try:
        wait_for(lambda: (tmp_path / "grandchild.pid").exists())
        pids = [int((tmp_path / (role + ".pid")).read_text()) for role in ("tree", "leaf", "grandchild")]
        outcome = wait_for(lambda: controller.poll(identity))
        assert outcome.reason == "timed_out"
        wait_for(lambda: all(not alive(pid) for pid in pids))
    finally:
        controller.terminate(identity, reason="cancelled")


def test_success_preserves_exit_status_and_unicode_logs(tmp_path):
    controller = ProcessController()
    identity = controller.start(request(tmp_path, [sys.executable, "-c", "import sys; print('静力学'); sys.exit(7)"]))
    outcome = wait_for(lambda: controller.poll(identity))
    assert outcome.reason == "exited"
    assert outcome.returncode == 7
    assert "静力学" in (tmp_path / "stdout.log").read_text(encoding="utf-8")


def test_python_logs_are_utf8_even_with_conflicting_parent_encoding(tmp_path):
    controller = ProcessController()
    configured = request(tmp_path, [sys.executable, "-c", "print('静力学')"])
    configured.env["PYTHONIOENCODING"] = "ascii"
    identity = controller.start(configured)
    outcome = wait_for(lambda: controller.poll(identity))
    assert outcome.returncode == 0
    assert (tmp_path / "stdout.log").read_text(encoding="utf-8").strip() == "静力学"


def test_log_budget_stops_owned_process_and_retains_raw_bytes(tmp_path):
    controller = ProcessController()
    script = "import sys,time; print('静力学' * 3000, flush=True); time.sleep(20)"
    configured = replace(request(tmp_path, [sys.executable, '-c', script]), max_log_bytes=4096)
    identity = controller.start(configured)
    try:
        outcome = wait_for(lambda: controller.poll(identity))
        assert outcome.reason == 'budget_exceeded'
        assert (tmp_path / 'stdout.log').stat().st_size > 4096
        assert '静力学' in (tmp_path / 'stdout.log').read_text(encoding='utf-8')
    finally:
        controller.terminate(identity, reason='cancelled')


@pytest.mark.parametrize('limit', [0, -1, True, 1.5, 2**41])
def test_invalid_log_budget_never_starts_a_child(tmp_path, limit):
    with pytest.raises(ValueError, match='log budget'):
        ProcessController().start(replace(request(tmp_path), max_log_bytes=limit))
    assert not (tmp_path / 'stdout.log').exists()


def test_owner_exit_cleans_process_tree(tmp_path):
    owner = tmp_path / "owner.py"
    owner.write_text("from pathlib import Path\nimport os,time\nfrom mechanical_design_agent.analysis_process import ProcessController,ProcessRequest\n"
                     f"root=Path({str(tmp_path)!r})\ncontroller=ProcessController()\n"
                     f"controller.start(ProcessRequest(argv=({sys.executable!r},{str(CHILD)!r},'tree',str(root)),cwd=root,stdout_path=root/'out',stderr_path=root/'err',env=dict(os.environ)))\n"
                     "while not (root/'grandchild.pid').exists(): time.sleep(.03)\nos._exit(0)\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(owner)], timeout=10)
    assert result.returncode == 0
    pids = [int((tmp_path / (role + ".pid")).read_text()) for role in ("tree", "leaf", "grandchild")]
    wait_for(lambda: all(not alive(pid) for pid in pids))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX guardian receipt contract')
def test_receipt_write_error_still_cleans_descendants(tmp_path):
    controller = ProcessController()
    script = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); Path('descendant.pid').write_text(str(p.pid)); time.sleep(.5)"
    identity = controller.start(request(tmp_path, [sys.executable, '-c', script]))
    pid = None
    try:
        wait_for(lambda: (tmp_path / 'descendant.pid').exists())
        pid = int((tmp_path / 'descendant.pid').read_text())
        (tmp_path / ('.process-' + identity.worker_uuid + '.json')).mkdir()
        wait_for(lambda: controller.poll(identity))
        wait_for(lambda: not alive(pid), timeout=2)
    finally:
        controller.terminate(identity, reason='cancelled')
        if pid and alive(pid):
            os.kill(pid, 9)
