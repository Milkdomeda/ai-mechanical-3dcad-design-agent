"""One pinned W1 transaction for fixed native phases, without repeated scans.

Full runtime verification occurs before and after a transaction. Each phase
independently pins its request, copied inputs and fixed package source. A
cancelled/failed verification never becomes a completed integrity receipt.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path
import time
import uuid
import sys

from . import secure_fs
from .analysis_native import _environment, _native_path, _read, verify_settings
from .analysis_process import ProcessController, ProcessRequest
from .analysis_scratch import native_scratch, verify_native_temp
from .analysis_types import AnalysisError
from .package_resources import freecad_scripts_directory

_PHASES = {'geometry': ('analysis_geometry.py', ()), 'mesh': ('analysis_mesh.py', ('analysis_mesh_math.py',)), 'build': ('analysis_build.py', ()), 'extract': ('analysis_extract_results.py', ())}
_INPUTS = {'geometry': {'source.FCStd'}, 'mesh': {'geometry.brep'}, 'build': {'geometry.brep', 'mesh.unv'}, 'extract': {'mesh.unv', 'Mesh.frd', 'Mesh.dat'}}


class NativeTransaction:
    def __init__(self, settings, *, controller=None, checkpoint=None):
        self.settings = settings
        self.controller = controller or ProcessController()
        self.checkpoint = checkpoint or (lambda: None)
        self._stack = ExitStack()
        self.owned = None
        self._failed = None
        self._sealed = False

    def __enter__(self):
        if self.owned is not None:
            raise AnalysisError('ANALYSIS_NATIVE_TRANSACTION_ACTIVE', 'a native transaction cannot be entered twice concurrently')
        self.checkpoint()
        verify_settings(self.settings, checkpoint=self.checkpoint)
        self._failed, self._sealed = None, False
        try:
            self.resources = self._stack.enter_context(freecad_scripts_directory())
            self.owned = self._stack.enter_context(native_scratch(Path(self.settings['native_scratch_root'])))
        except BaseException:
            self._stack.close()
            raise
        return self

    def __exit__(self, kind, value, traceback):
        try:
            try:
                if not self._sealed: self.seal()
            except BaseException as exc:
                self._stack.__exit__(type(exc), exc, exc.__traceback__)
                raise
            if kind is None and self._failed is not None:
                kind, value, traceback = self._failed
            return self._stack.__exit__(kind, value, traceback)
        finally:
            self.owned = None

    def seal(self):
        """Complete integrity verification before publishing authoritative state."""
        if self.owned is None:
            raise AnalysisError('ANALYSIS_NATIVE_PHASE_INVALID', 'only a living owned transaction may be sealed')
        if not self._sealed:
            verify_settings(self.settings, checkpoint=self.checkpoint)
            self._sealed = True

    def run(self, phase, contract, inputs, output_dir, *, timeout_seconds, max_log_bytes):
        try:
            return self._run(phase, contract, inputs, output_dir, timeout_seconds=timeout_seconds, max_log_bytes=max_log_bytes)
        except BaseException:
            self._failed = sys.exc_info()
            raise

    def _run(self, phase, contract, inputs, output_dir, *, timeout_seconds, max_log_bytes):
        if self.owned is None or self._sealed or phase not in _PHASES or set(inputs) != _INPUTS[phase]:
            raise AnalysisError('ANALYSIS_NATIVE_PHASE_INVALID', 'only fixed package phases and their exact input names may run')
        self.checkpoint()
        output = secure_fs.validate_managed_path(output_dir, allow_missing_leaf=True)
        if output.identity is not None:
            raise AnalysisError('ANALYSIS_OUTPUT_EXISTS', 'each native phase needs a new immutable output directory')
        output = secure_fs.ensure_managed_directory(output.path, parents=False, exist_ok=False).path
        work = secure_fs.ensure_managed_directory(self.owned.path / uuid.uuid4().hex, parents=False, exist_ok=False).path
        home = secure_fs.ensure_managed_directory(work / 'home', parents=False, exist_ok=False).path
        for name in ('user.cfg', 'system.cfg'):
            secure_fs.atomic_publish_new(home / name, b'<FCParameters><FCParamGroup Name="Root"/></FCParameters>')
        script, helpers = _PHASES[phase]
        assets = {}
        for name in (script, *helpers):
            asset = _read(self.resources / name)
            secure_fs.atomic_publish_new(work / name, asset.content)
            assets[name] = asset.sha256
        copied = {}
        for name, path in inputs.items():
            data = _read(path)
            secure_fs.atomic_publish_new(work / name, data.content)
            copied[name] = data.sha256
        request_id = uuid.uuid4().hex
        request = {'request_id': request_id, 'expected_temp': _native_path(self.owned.path), 'inputs': {name: {'path': _native_path(work / name), 'sha256': sha} for name, sha in copied.items()}, 'contract': contract}
        if phase in {'geometry', 'mesh'}:
            request = {'request_id': request_id, 'expected_temp': _native_path(self.owned.path), 'source_path': _native_path(work / next(iter(inputs))), 'contract': contract}
        encoded = json.dumps(request, ensure_ascii=True, allow_nan=False, sort_keys=True).encode('utf-8')
        secure_fs.atomic_publish_new(work / 'request.json', encoded)
        args = [_native_path(work / script), _native_path(work / 'request.json'), _native_path(work / 'response.json')]
        statement = 'import sys;sys.dont_write_bytecode=True;import runpy;from pathlib import Path;sys.argv=' + ascii(args) + ";sys.path.insert(0,str(Path(sys.argv[0]).parent));runpy.run_path(sys.argv[0],run_name='__main__')"
        argv = (_native_path(Path(self.settings['tools']['freecadcmd']['path'])), '-u', _native_path(home / 'user.cfg'), '-s', _native_path(home / 'system.cfg'), '-c', statement)
        identity = self.controller.start(ProcessRequest(argv=argv, cwd=work, stdout_path=work / 'stdout.log', stderr_path=work / 'stderr.log', env=_environment(home, self.owned.path), timeout_seconds=timeout_seconds, max_log_bytes=max_log_bytes))
        outcome = None
        reason = 'interrupted'
        try:
            while True:
                self.checkpoint()
                outcome = self.controller.poll(identity)
                if outcome is not None: break
                time.sleep(.05)
        except AnalysisError as exc:
            reason = {'ANALYSIS_CANCELLED': 'cancelled', 'ANALYSIS_TIMED_OUT': 'timed_out', 'ANALYSIS_BUDGET_EXCEEDED': 'budget_exceeded'}.get(exc.code, 'interrupted')
            raise
        finally:
            stopped = self.controller.terminate(identity, reason=reason)
            if outcome is None: outcome = stopped
            for name in ('stdout.log', 'stderr.log', 'response.json'):
                if (work / name).exists():
                    secure_fs.atomic_publish_new(output / ('native.json' if name == 'response.json' else name), _read(work / name).content)
            secure_fs.atomic_publish_new(output / 'process.json', json.dumps({**asdict(outcome), 'phase': phase, 'request_id': request_id, 'request_sha256': _read(work / 'request.json').sha256, 'script_hashes': assets, 'input_hashes': copied}, sort_keys=True).encode())
        self.checkpoint()
        if any(_read(work / name).sha256 != sha for name, sha in {**assets, **copied}.items()) or _read(work / 'request.json').content != encoded:
            raise AnalysisError('ANALYSIS_NATIVE_INPUT_CHANGED', 'fixed phase source, request or copied input changed during execution')
        if outcome.reason != 'exited' or outcome.returncode != 0:
            raise AnalysisError('ANALYSIS_NATIVE_FAILED', 'native phase did not exit normally; inspect retained process logs')
        result = json.loads(_read(work / 'response.json').content.decode('utf-8'))
        expected = {'schema_version': 'AnalysisNativePhase/v1', 'phase': phase, 'request_id': request_id, 'request_sha256': _read(work / 'request.json').sha256, 'script_sha256': assets[script], 'app_version': ['1', '1', '3'], 'status': 'passed'}
        if any(result.get(key) != value for key, value in expected.items()):
            raise AnalysisError('ANALYSIS_NATIVE_RECEIPT_INVALID', 'native phase identity or execution failed; inspect native.json')
        verify_native_temp(result.get('native_temp_path'), self.owned)
        artifacts = result[phase]['artifacts'] if phase in {'geometry', 'mesh'} else result['artifacts']
        names = set()
        for reference in artifacts.values():
            name = reference['relative_path']
            if name in names or type(name) is not str or Path(name).name != name or '\\' in name or ':' in name or name in ('.', '..'):
                raise AnalysisError('ANALYSIS_NATIVE_ARTIFACT_INVALID', 'native artifacts must be distinct local regular files')
            names.add(name)
            data = _read(work / name)
            if data.sha256 != reference['sha256']:
                raise AnalysisError('ANALYSIS_NATIVE_ARTIFACT_INVALID', 'native output hash changed before publication')
            secure_fs.atomic_publish_new(output / name, data.content)
        return result
