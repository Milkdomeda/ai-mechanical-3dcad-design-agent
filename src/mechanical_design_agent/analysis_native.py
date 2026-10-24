"""Packaged fixed native phases, private W1 scratch and owned processes."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

from . import secure_fs
from .analysis_configuration import load_analysis_configuration
from .analysis_process import ProcessController, ProcessRequest
from .analysis_scratch import native_scratch, verify_native_temp
from .analysis_types import AnalysisError
from .fcstd_security import inspect_fcstd_bytes
from .package_resources import freecad_scripts_directory


def verify_settings(settings):
    """Recheck the complete isolated inventory without executing any source."""
    runtime = SimpleNamespace(environ={'MECH_DESIGN_FEA_CONFIG': settings['configuration_path']}, _manifest=lambda: SimpleNamespace(workspace=Path(settings['workspace'])))
    actual = load_analysis_configuration(runtime)
    if actual != settings:
        raise AnalysisError('FEA_RUNTIME_CHANGED', 'configured native runtime changed during analysis')


def _read(path):
    located = secure_fs.validate_managed_path(path, allow_missing_leaf=False)
    data = secure_fs.read_managed_file(located.path)
    if data.identity != located.identity or data.link_count != 1:
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'native file identity or link count changed')
    return data


def _native_path(path):
    # Qt/FreeCAD resource discovery cannot use the Windows extended prefix.
    value = str(path)
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


def _environment(home, scratch):
    env = {key: value for key, value in os.environ.items() if key in {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT'}}
    env.update(HOME=_native_path(home), USERPROFILE=_native_path(home), FREECAD_USER_HOME=_native_path(home), FREECAD_USER_DATA=_native_path(home), FREECAD_USER_TEMP=_native_path(scratch), TMPDIR=_native_path(scratch), TEMP=_native_path(scratch), TMP=_native_path(scratch), PATH=os.defpath, PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', QT_QPA_PLATFORM='offscreen')
    return env


def run_geometry_phase(source, contract, settings, output_dir, *, timeout_seconds=120):
    from .analysis_geometry import validate_geometry_manifest, validate_geometry_request
    contract = validate_geometry_request(contract)
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 600:
        raise AnalysisError('ANALYSIS_BUDGET_INVALID', 'native geometry timeout must be bounded')
    verify_settings(settings)
    original = _read(source)
    if original.sha256 != contract['source_sha256']:
        raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'geometry source differs from the captured revision')
    inspect_fcstd_bytes(original.content)
    output = secure_fs.validate_managed_path(output_dir, allow_missing_leaf=True)
    if output.identity is not None:
        raise AnalysisError('ANALYSIS_OUTPUT_EXISTS', 'a new immutable native phase directory is required')
    directory = secure_fs.ensure_managed_directory(output.path, parents=False, exist_ok=False).path
    with freecad_scripts_directory() as resources, native_scratch(Path(settings['native_scratch_root'])) as owned:
        script = _read(resources / 'analysis_geometry.py')
        worker = owned.path / 'geometry_worker.py'
        copied_source = owned.path / 'source.FCStd'
        secure_fs.atomic_publish_new(worker, script.content)
        secure_fs.atomic_publish_new(copied_source, original.content)
        home = secure_fs.ensure_managed_directory(owned.path / 'home', parents=False, exist_ok=False).path
        for name in ('user.cfg', 'system.cfg'):
            secure_fs.atomic_publish_new(home / name, b'<FCParameters><FCParamGroup Name="Root"/></FCParameters>')
        request_path, response = owned.path / 'request.json', owned.path / 'response.json'
        request_id = uuid.uuid4().hex
        request = {'request_id': request_id, 'source_path': _native_path(copied_source), 'expected_temp': _native_path(owned.path), 'contract': contract}
        encoded = json.dumps(request, ensure_ascii=True, allow_nan=False, sort_keys=True).encode('utf-8')
        secure_fs.atomic_publish_new(request_path, encoded)
        args = [_native_path(worker), _native_path(request_path), _native_path(response)]
        statement = 'import sys; sys.dont_write_bytecode=True; import runpy; sys.argv=' + ascii(args) + "; runpy.run_path(sys.argv[0],run_name='__main__')"
        executable = _native_path(Path(settings['tools']['freecadcmd']['path']))
        argv = (executable, '-u', _native_path(home / 'user.cfg'), '-s', _native_path(home / 'system.cfg'), '-c', statement)
        controller = ProcessController()
        identity = controller.start(ProcessRequest(argv=argv, cwd=directory, stdout_path=directory / 'stdout.log', stderr_path=directory / 'stderr.log', env=_environment(home, owned.path), timeout_seconds=timeout_seconds))
        try:
            while (outcome := controller.poll(identity)) is None:
                time.sleep(.05)
        finally:
            controller.terminate(identity, reason='interrupted')
        secure_fs.atomic_publish_new(directory / 'process.json', json.dumps({**asdict(outcome), 'source_sha256': original.sha256, 'script_sha256': script.sha256}, sort_keys=True).encode())
        verify_settings(settings)
        after = _read(source)
        if after.identity != original.identity or after.sha256 != original.sha256 or _read(worker).sha256 != script.sha256 or _read(request_path).content != encoded or _read(copied_source).sha256 != original.sha256:
            raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'source or fixed native phase assets changed during execution')
        if outcome.reason != 'exited' or outcome.returncode != 0:
            raise AnalysisError('ANALYSIS_NATIVE_FAILED', 'native geometry process did not exit normally; see phase logs')
        receipt = _read(response)
        secure_fs.atomic_publish_new(directory / 'native.json', receipt.content)
        try:
            result = json.loads(receipt.content.decode('utf-8'))
            expected = {'schema_version': 'AnalysisNativePhase/v1', 'phase': 'geometry', 'request_id': request_id, 'request_sha256': _read(request_path).sha256, 'script_sha256': script.sha256, 'app_version': ['1', '1', '3'], 'status': 'passed'}
            if any(result.get(key) != value for key, value in expected.items()):
                raise ValueError('phase identity or result failed')
            verify_native_temp(result.get('native_temp_path'), owned)
            geometry = validate_geometry_manifest(result['geometry'])
            for reference in geometry['artifacts'].values():
                data = _read(owned.path / reference['relative_path'])
                if data.sha256 != reference['sha256']:
                    raise ValueError('artifact hash changed')
                if reference['relative_path'].endswith('.FCStd'):
                    inspect_fcstd_bytes(data.content)
                secure_fs.atomic_publish_new(directory / reference['relative_path'], data.content)
        except (ValueError, KeyError, TypeError) as exc:
            raise AnalysisError('ANALYSIS_NATIVE_RECEIPT_INVALID', 'native geometry receipt failed its identity, topology or artifact checks; see native.json') from exc
    return geometry
