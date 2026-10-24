"""One session-owned product native fixture; no verification bypass flag."""
import json
import os
from pathlib import Path
import time

import pytest

from mechanical_design_agent.analysis_process import ProcessController, ProcessRequest


@pytest.fixture(scope='session')
def native_product_session(tmp_path_factory):
    from test_analysis_geometry_live import native_settings
    config = os.environ.get('FEA_GUI_LIVE_CONFIG')
    scratch = os.environ.get('FEA_GUI_LIVE_SCRATCH')
    if not config or not scratch:
        pytest.skip('requires explicit isolated native FreeCAD configuration and private ASCII W1 root')
    from mechanical_design_agent.analysis_native import _environment, _native_path
    from mechanical_design_agent.analysis_scratch import native_scratch
    directory = tmp_path_factory.mktemp('geometry_native')
    settings = native_settings(directory / 'workspace 中文', config, Path(scratch))
    fixtures = directory / 'fixtures 中文'; fixtures.mkdir()
    with native_scratch(Path(scratch)) as owned:
        script = Path(__file__).with_name('fea_geometry_fixture.py')
        args = [_native_path(script), _native_path(owned.path)]
        statement = 'import sys;sys.dont_write_bytecode=True;import runpy;sys.argv=' + ascii(args) + ";runpy.run_path(sys.argv[0],run_name='__main__')"
        argv = (_native_path(settings['tools']['freecadcmd']['path']), '-u', _native_path(owned.path / 'user.cfg'), '-s', _native_path(owned.path / 'system.cfg'), '-c', statement)
        controller = ProcessController()
        identity = controller.start(ProcessRequest(argv=argv, cwd=fixtures, stdout_path=fixtures / 'stdout.log', stderr_path=fixtures / 'stderr.log', env=_environment(owned.path, owned.path), timeout_seconds=60))
        try:
            while (outcome := controller.poll(identity)) is None: time.sleep(.05)
        finally:
            controller.terminate(identity, reason='interrupted')
        assert outcome.reason == 'exited' and outcome.returncode == 0
        created = json.loads((owned.path / 'fixture-created.json').read_text(encoding='utf-8'))
        assert created['version'] == ['1', '1', '3']
        for name in created['files']:
            (fixtures / name).write_bytes((owned.path / name).read_bytes())
    from mechanical_design_agent.analysis_native_transaction import NativeTransaction
    with NativeTransaction(settings) as native:
        yield directory, fixtures, settings, native


@pytest.fixture
def native_transaction(native_product_session):
    return native_product_session[3]
