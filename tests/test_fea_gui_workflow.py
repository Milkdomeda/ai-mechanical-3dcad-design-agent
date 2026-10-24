from pathlib import Path
import re
import yaml


def test_native_gui_workflow_keeps_windows_server_and_windows_11_arm_distinct():
    path=Path(__file__).resolve().parents[1]/'.github/workflows/fea-gui-automation.yml'
    config=yaml.safe_load(path.read_text(encoding='utf-8'))
    assert config['permissions']=={'contents':'read'}
    job=config['jobs']['windows-gui'];matrix=job['strategy']['matrix']['include']
    assert {row['runner'] for row in matrix}=={'windows-2025','windows-11-arm'}
    assert all(row['python_arch']=='x64' for row in matrix)
    commands='\n'.join(step.get('run','') for step in job['steps'])
    assert 'fea_gui_automation.py' in commands and '--native-scratch-root' in commands
    assert 'fea_r1_prepare.py' in commands
    assert 'QT_QPA_PLATFORM' not in commands
    assert not any(step.get('continue-on-error') for step in job['steps'])
    for step in job['steps']:
        if 'uses' in step:assert re.search(r'@[0-9a-f]{40}$',step['uses'])
