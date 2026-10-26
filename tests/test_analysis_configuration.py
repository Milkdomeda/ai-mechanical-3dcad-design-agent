import hashlib
import json
from pathlib import Path

import pytest

from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


def runtime(tmp_path,environ=None):
    initialize_workspace(workspace=tmp_path,actor_id='engineer',organization_id='org',design_group_id='group',dry_run=False)
    return BootstrapRuntime.from_process(cwd=tmp_path,environ=environ or {},workspace=tmp_path)


def test_missing_fea_config_is_read_only_and_does_not_change_cad_status(tmp_path):
    from mechanical_design_agent.analysis_configuration import analysis_capabilities
    selected=runtime(tmp_path);before=selected.status(include_freecad=False)
    files={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=analysis_capabilities(selected)
    assert result['status']=='setup_required' and result['code']=='FEA_CONFIG_MISSING'
    assert before==selected.status(include_freecad=False)
    assert files=={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def configured(tmp_path,monkeypatch,*,relative=False):
    from mechanical_design_agent import analysis_configuration as module
    selected=runtime(tmp_path,{'MECH_DESIGN_FEA_CONFIG':'config/有限元 设置.json'} if relative else {})
    root=tmp_path/'native';root.mkdir();tools={}
    for name,version in [('freecadcmd','1.1.3'),('freecadgui','1.1.3'),('gmsh','4.15.0'),('ccx','2.23')]:
        path=root/name;path.write_bytes(('synthetic '+name).encode())
        tools[name]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'version':version}
    source=root/'meshtools.py';source.write_bytes(b'# fixed synthetic native module\n')
    sha=hashlib.sha256(source.read_bytes()).hexdigest();monkeypatch.setattr(module,'_R1_MODULE_SHA256',sha)
    native={'schema_version':'FEAR1Runtime/v1','profile':'freecad-1.1.3-fem-faceforce-r1','runtime_root':str(root),'module_path':str(source),'module_sha256':sha,'source_commit':'145529fe741292ff0b3977a01195bf0247425794','source_unchanged':True,'tools':{k:v for k,v in tools.items() if k!='freecadgui'},'native_identity':{'app_version':['1','1','3']},'tree':{p.relative_to(root).as_posix():{'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in root.iterdir()}}
    manifest=tmp_path/'runtime-manifest.json';manifest.write_text(json.dumps(native),encoding='utf-8')
    config={'schema_version':'FEAConfiguration/v1','tools':tools,'runtime':{'profile':native['profile'],'manifest_path':str(manifest),'manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()},'native_scratch_root':str(tmp_path/'private-cache'),'software_opengl':False}
    path=tmp_path/'config'/('有限元 设置.json' if relative else 'fea.json');path.write_text(json.dumps(config),encoding='utf-8')
    return selected,path,tools


def test_unicode_relative_configuration_uses_workspace_and_pins_files_without_executing(tmp_path,monkeypatch):
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    selected,path,tools=configured(tmp_path,monkeypatch,relative=True)
    result=load_analysis_configuration(selected)
    assert Path(result['configuration_path']).samefile(path)
    assert result['tools']['ccx']['sha256']==tools['ccx']['sha256']
    assert result['verification_scope']=='configured file identities; no native process or GUI probe executed'
    assert not (tmp_path/'private-cache').exists()


def test_changed_solver_digest_blocks_only_analysis(tmp_path,monkeypatch):
    from mechanical_design_agent.analysis_configuration import analysis_capabilities
    selected,path,tools=configured(tmp_path,monkeypatch)
    before=selected.status(include_freecad=False)
    Path(tools['ccx']['path']).write_bytes(b'changed')
    result=analysis_capabilities(selected)
    assert result['status']=='setup_required' and result['code']=='FEA_TOOL_DIGEST_CHANGED'
    assert selected.status(include_freecad=False)==before


def test_runtime_analysis_settings_are_lazy_and_package_resource_based(tmp_path,monkeypatch):
    from mechanical_design_agent.package_resources import analysis_resources_directory
    selected,path,tools=configured(tmp_path,monkeypatch)
    assert selected.analysis_settings()['configuration_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    with analysis_resources_directory() as root:
        assert (root/'schemas/spec.schema.json').is_file()


def test_changed_runtime_source_cannot_hide_behind_the_original_tool_hash(tmp_path,monkeypatch):
    from mechanical_design_agent.analysis_configuration import analysis_capabilities
    selected,path,tools=configured(tmp_path,monkeypatch)
    (tmp_path/'native/meshtools.py').write_bytes(b'changed native module')
    result=analysis_capabilities(selected)
    assert result['status']=='setup_required' and result['code']=='FEA_RUNTIME_CHANGED'


def test_fea_capabilities_on_uninitialized_workspace_are_actionable_and_read_only(tmp_path):
    from mechanical_design_agent.analysis_configuration import analysis_capabilities
    selected=BootstrapRuntime.from_process(cwd=tmp_path,environ={})
    result=analysis_capabilities(selected)
    assert result['status']=='setup_required' and result['code']=='WORKSPACE_NOT_INITIALIZED'
    assert list(tmp_path.iterdir())==[]


def test_invalid_utf8_configuration_is_a_controlled_fea_error(tmp_path):
    from mechanical_design_agent.analysis_configuration import analysis_capabilities
    selected=runtime(tmp_path)
    (tmp_path/'config/fea.json').write_bytes(b'\xff')
    result=analysis_capabilities(selected)
    assert result['status']=='setup_required' and result['code']=='FEA_CONFIG_INVALID'


def test_full_inventory_verification_has_cooperative_cancellation(tmp_path, monkeypatch):
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.analysis_types import AnalysisError
    selected, path, tools = configured(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in (tmp_path/'native').iterdir()}
    checkpoints = []
    def cancel():
        checkpoints.append(True)
        if len(checkpoints) == 5:
            raise AnalysisError('ANALYSIS_CANCELLED', 'Synthetic cancellation during inventory verification.')
    with pytest.raises(AnalysisError) as error:
        load_analysis_configuration(selected, checkpoint=cancel)
    assert error.value.code == 'ANALYSIS_CANCELLED'
    assert before == {p: p.read_bytes() for p in (tmp_path/'native').iterdir()}
    assert not (tmp_path/'private-cache').exists()


def test_r2_requires_the_exact_second_module_even_with_rebuilt_manifest(tmp_path,monkeypatch):
    from mechanical_design_agent import analysis_runtime_patch
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.analysis_types import AnalysisError
    selected,path,_ = configured(tmp_path,monkeypatch)
    manifest=tmp_path/'runtime-manifest.json'
    data=json.loads(manifest.read_text())
    root=tmp_path/'native'
    module=root/'Mod/Fem/femmesh/meshtools.py';module.parent.mkdir(parents=True)
    (root/'meshtools.py').rename(module)
    output=root/'Mod/Fem/femsolver/calculix/write_step_output.py';output.parent.mkdir(parents=True)
    output.write_bytes(b'# synthetic reviewed output\n')
    sha=hashlib.sha256(output.read_bytes()).hexdigest()
    monkeypatch.setattr(analysis_runtime_patch,'PATCHED_SHA256',sha)
    data.update(schema_version='FEAR2Runtime/v1',profile=analysis_runtime_patch.PROFILE,module_path=str(module),output_module_path=str(output),output_module_sha256=sha,loaded_identity={'output':{'sha256':sha}})
    def publish():
        data['tree']={p.relative_to(root).as_posix():{'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in root.rglob('*') if p.is_file()}
        manifest.write_text(json.dumps(data))
        config=json.loads(path.read_text());config['runtime'].update(profile=analysis_runtime_patch.PROFILE,manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest());path.write_text(json.dumps(config))
    publish()
    assert load_analysis_configuration(selected)['runtime']['output_module_sha256']==sha
    output.write_bytes(b'# unreviewed source, with otherwise self-consistent inventory\n')
    publish()
    with pytest.raises(AnalysisError) as error: load_analysis_configuration(selected)
    assert error.value.code=='FEA_RUNTIME_CHANGED'
