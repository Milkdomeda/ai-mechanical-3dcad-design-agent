import json
from pathlib import Path
import subprocess
import sys

from test_fea_gui_input import INPUT
from test_fea_gui_checks import native_snapshot


def test_gui_inventory_is_bound_to_parsed_native_input_not_its_own_claim(tmp_path):
    from fea_gui_automation import assess_case
    reference=tmp_path/'reference.inp';exported=tmp_path/'exported.inp'
    reference.write_text(INPUT);exported.write_text(INPUT)
    value=native_snapshot();value.update(case_id='axial_bar',source_unchanged=True,recompute_errors=[],closed_without_saving=True)
    assert assess_case(value,reference,exported)['status']=='passed'
    value['mesh_inventory']['nodes']=11;value['result_inventory']['nodes']=11
    assert assess_case(value,reference,exported)['status']=='failed'


def test_gui_automation_cli_is_explicit_and_rejects_arbitrary_source_restore():
    script=Path(__file__).resolve().parents[1]/'scripts'/'fea_gui_automation.py'
    help_run=subprocess.run([sys.executable,str(script),'--help'],capture_output=True,text=True)
    assert help_run.returncode==0,help_run.stderr
    assert '--native-scratch-root' in help_run.stdout
    invalid=subprocess.run([sys.executable,str(script),'--source','customer.FCStd'],capture_output=True,text=True)
    assert invalid.returncode==2
    assert 'Traceback' not in invalid.stderr
