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
