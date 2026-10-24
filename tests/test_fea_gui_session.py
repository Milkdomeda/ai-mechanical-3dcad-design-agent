import json
import os
from pathlib import Path
import sys
import pytest


def test_visible_gui_spec_has_no_document_argument_or_offscreen_backend(tmp_path):
    from fea_gui_session import launch_spec
    from fea_r1_runtime import canonical_path
    home=tmp_path/'home';home.mkdir();scratch=tmp_path/'scratch';scratch.mkdir()
    gui=tmp_path/'FreeCAD';gui.write_bytes(b'fixture GUI executable')
    config={'tools':{name:{'path':str(tmp_path/name)} for name in ('gmsh','ccx')}}
    argv,env=launch_spec(gui,home,scratch,config)
    expected_home=canonical_path(home);expected_scratch=canonical_path(scratch)
    assert argv==(str(gui),'-u',str(expected_home/'user.cfg'),'-s',str(expected_home/'system.cfg'))
    assert os.path.samefile(argv[2],home/'user.cfg') and os.path.samefile(argv[4],home/'system.cfg')
    assert not argv[2].startswith('\\\\?\\') and not argv[4].startswith('\\\\?\\')
    assert not any(value.lower().endswith('.fcstd') for value in argv)
    assert 'QT_QPA_PLATFORM' not in env and 'PYTHONPATH' not in env
    assert env['FREECAD_USER_TEMP']==str(expected_scratch)
    assert os.path.samefile(env['FREECAD_USER_TEMP'],scratch)
    assert env['FREECAD_USER_HOME']==env['FREECAD_USER_DATA']==str(expected_home)
    assert os.path.samefile(env['FREECAD_USER_HOME'],home)
    assert env['PYTHONDONTWRITEBYTECODE']=='1'
    assert str(home) in (home/'user.cfg').read_text() or 'gmshBinaryPath' in (home/'user.cfg').read_text()


def test_owned_ascii_scratch_lives_until_child_exit_and_is_then_cleared(tmp_path):
    from fea_gui_session import owned_gui_process
    logs=tmp_path/'logs';logs.mkdir()
    worker=tmp_path/'worker.py';worker.write_text('import os,pathlib,time; p=pathlib.Path(os.environ["FREECAD_USER_TEMP"]); (p/"alive").write_text("yes"); time.sleep(.4)')
    scratch_root=tmp_path/'cache'
    if not str(scratch_root).isascii():pytest.skip('private ASCII path required')
    observed=[]
    def build(owned):
        observed.append(owned.path)
        return (sys.executable,str(worker)),dict(os.environ,FREECAD_USER_TEMP=str(owned.path))
    def observe(identity,controller,owned):
        import time
        end=time.monotonic()+3
        while not (owned.path/'alive').exists() and time.monotonic()<end:time.sleep(.01)
        assert (owned.path/'alive').read_text()=='yes'
        assert controller.poll(identity) is None
    result=owned_gui_process(logs,scratch_root,build,observe,timeout_seconds=10)
    assert result['outcome']=='exited'
    assert not (observed[0]/'alive').exists()
    assert observed[0].is_dir() and (observed[0]/'.native-owner.lock').exists()


def test_stop_request_cannot_target_a_different_session(tmp_path):
    from fea_gui_session import stop_requested
    p=tmp_path/'stop.json';p.write_text(json.dumps({'session_id':'other','action':'stop'}))
    assert stop_requested(p,'ours') is False
    p.write_text(json.dumps({'session_id':'ours','action':'stop'}))
    assert stop_requested(p,'ours') is True
