from pathlib import Path
import re
import yaml


def test_native_gui_workflow_keeps_windows_server_and_windows_11_arm_distinct():
    path=Path(__file__).resolve().parents[1]/'.github/workflows/fea-gui-automation.yml'
    config=yaml.safe_load(path.read_text(encoding='utf-8'))
    assert config['permissions']=={'contents':'read'}
    job=config['jobs']['windows-gui'];matrix=job['strategy']['matrix']
    assert set(matrix['runner'])=={'windows-2025','windows-11-arm'}
    assert matrix['python_arch']==['x64']
    assert matrix['scope']=="${{ fromJSON(inputs.acceptance_scope == 'native' && '[\"native\"]' || inputs.acceptance_scope == 'installed' && '[\"installed\"]' || '[\"installed\",\"native\"]') }}"
    commands='\n'.join(step.get('run','') for step in job['steps'])
    assert 'fea_gui_automation.py' in commands and '--native-scratch-root' in commands
    assert 'fea_r1_prepare.py' in commands
    assert 'QT_QPA_PLATFORM' not in commands
    assert not any(step.get('continue-on-error') for step in job['steps'])
    for step in job['steps']:
        if 'uses' in step:assert re.search(r'@[0-9a-f]{40}$',step['uses'])


def test_manual_scope_is_explicit_and_all_original_gates_keep_their_group():
    # BaseLoader preserves the YAML 'on' key rather than YAML 1.1's boolean.
    config=yaml.load((Path(__file__).resolve().parents[1]/'.github/workflows/fea-gui-automation.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    scope=config['on']['workflow_dispatch']['inputs']['acceptance_scope']
    assert scope=={'description':'Explicit acceptance group to repeat; pushes always run all groups','type':'choice','required':'true','default':'all','options':['all','native','installed']}
    assert config['on']['push']['branches']==['codex/fea-deployable-static']
    job=config['jobs']['windows-gui'];assert job['timeout-minutes']=='120'
    groups={step['name']:step.get('if') for step in job['steps'] if 'name' in step}
    for name in ('Install isolated runtime and qualify through installed wheel','Verify installed default detached executor without checkout imports'):
        assert groups[name]=="matrix.scope == 'installed'"
    for name in ('Prepare isolated reviewed R1 runtime','Run unchanged numerical P0 checks','Materialize separate official bundled OpenGL GUI test environment','Verify product geometry mesh and FEM writer in one pinned transaction','Run five native GUI cases and independent-process round trip','Prepare separately owned reviewed R2 output runtime','Verify R2 fields balance and two-case three-level convergence'):
        assert groups[name]=="matrix.scope == 'native'"
    assert groups['Prewarm clean-install tests and run complete offline suite'] is None
    assert groups['Upload synthetic checks and platform/runtime identities']=='always()'


def test_offline_ci_uses_prewarmed_cache_in_same_step_and_real_second_volume():
    config=yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/fea-gui-automation.yml').read_text(encoding='utf-8'))
    commands='\n'.join(step.get('run','') for step in config['jobs']['windows-gui']['steps'])
    assert '$env:UV_CACHE_DIR = $cache' in commands
    assert commands.index('$env:UV_CACHE_DIR = $cache')<commands.index('uv build --wheel')
    assert 'create vdisk' in commands and 'format fs=ntfs' in commands
    assert 'DriveType=3' in commands


def test_installed_native_acceptance_uses_wheel_and_excludes_claim_secrets():
    config=yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/fea-gui-automation.yml').read_text(encoding='utf-8'))
    steps=config['jobs']['windows-gui']['steps']
    setup=next(step['run'] for step in steps if step.get('name')=='Install isolated runtime and qualify through installed wheel')
    assert '-I -B -m mechanical_design_agent.cli analysis setup' in setup
    assert 'analysis doctor --workspace $workspace --qualify' in setup
    assert 'fea_r2_prepare.py' not in setup
    run=next(step['run'] for step in steps if step.get('name')=='Verify installed default detached executor without checkout imports')
    assert 'Set-Location $env:RUNNER_TEMP' in run and '& $python -I -B $harness' in run
    upload=next(step['with']['path'] for step in steps if step.get('uses','').startswith('actions/upload-artifact@'))
    assert '!${{ runner.temp }}/Installed FEA workspace 中文/data/analysis-scheduler/claims/**' in upload
