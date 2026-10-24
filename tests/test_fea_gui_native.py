from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('case,expected',[('axial_bar',100),('cantilever',10),('multiface',40),('curved_multiface',40),('r1_mesh_4',40)])
def test_native_automation_only_uses_reviewed_fixed_fixture_loads(case,expected):
    from fea_gui_native import fixture_spec
    spec=fixture_spec(case)
    assert spec['force_N']==expected
    assert spec['mesh_size_mm']==4
    assert spec['name']==('curved_multiface' if case=='r1_mesh_4' else case)


@pytest.mark.parametrize('case',['customer_file','../axial_bar','Axial_Bar',None,True,{}])
def test_arbitrary_names_or_sources_are_not_a_gui_restore_entry(case):
    from fea_gui_native import fixture_spec
    with pytest.raises(ValueError):fixture_spec(case)


def test_native_worker_import_needs_no_host_package_or_gui_until_run():
    tests=Path(__file__).resolve().parent
    code=f"import sys;sys.path.insert(0,{str(tests)!r});from fea_gui_native import fixture_spec;assert fixture_spec('axial_bar')['force_N']==100"
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',code],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_gui_native_files_use_w1_even_when_the_delivery_path_is_chinese(tmp_path):
    from fea_gui_native import native_case_workspace
    scratch=tmp_path/'private-native'
    if not str(scratch).isascii():pytest.skip('this fixture needs an ASCII parent')
    native=native_case_workspace(scratch,'axial_bar')
    assert native.parent==scratch and str(native).isascii()
    with pytest.raises(ValueError):native_case_workspace(tmp_path/'中文','axial_bar')
