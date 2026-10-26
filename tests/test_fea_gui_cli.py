import json
from pathlib import Path
import subprocess
import sys
from test_fea_gui_evidence import source_fixture


def test_cli_prepares_then_keeps_missing_manual_evidence_blocked(tmp_path):
    manifest,_=source_fixture(tmp_path)
    cli=Path(__file__).resolve().parents[1]/'scripts/fea_gui_acceptance.py'
    result=subprocess.run([sys.executable,str(cli),'prepare','--manifest',str(manifest),'--workspace',str(tmp_path/'GUI 中文')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    session=json.loads(result.stdout)['session']
    actions=tmp_path/'actions.json';actions.write_text('[]')
    result=subprocess.run([sys.executable,str(cli),'check','--session',session,'--case','axial_bar','--actions',str(actions)],capture_output=True,text=True)
    assert result.returncode==3,result.stderr
    report=json.loads(result.stdout)
    assert report['status']=='blocked' and report['overall_p0_status']=='blocked'
    directory=Path(session).parent/'comparison'
    assert len(list(directory.glob('*.json')))==len(list(directory.glob('*.md')))==1
