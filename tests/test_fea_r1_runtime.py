import hashlib
import json
from pathlib import Path
import pytest
import sys
import subprocess


def config(root):
    return {'schema_version':'FEAP0Configuration/v1','tools':{n:{'path':str(root/(n+'.exe')),'sha256':'a'*64,'version':v} for n,v in (('freecadcmd','1.1.3'),('gmsh','4.15.0'),('ccx','2.22'))},'timeout_seconds':300}


def test_v2_requires_explicit_pinned_manifest_and_v1_rejects_profile(tmp_path):
    from fea_native_probe import _validate_config,ProbeError
    value=config(tmp_path);value['schema_version']='FEAP0Configuration/v2'
    with pytest.raises(ProbeError):_validate_config(value)
    value['runtime']={'profile':'freecad-1.1.3-fem-faceforce-r1','manifest_path':str(tmp_path/'manifest.json'),'manifest_sha256':'b'*64}
    _validate_config(value)
    value['schema_version']='FEAP0Configuration/v1'
    with pytest.raises(ProbeError):_validate_config(value)


def test_preparation_rejects_unknown_base_before_creating_destination(tmp_path):
    from fea_r1_runtime import prepare_runtime
    source=tmp_path/'source';module=source/'Mod/Fem/femmesh/meshtools.py';module.parent.mkdir(parents=True);module.write_text('unknown native module')
    dest=tmp_path/'copy'
    with pytest.raises(ValueError,match='R1_BASE_MODULE_DIGEST'):prepare_runtime(source,dest,config(source))
    assert not dest.exists() and module.read_text()=='unknown native module'


def test_wrapper_with_unrelated_siblings_is_not_a_distribution_root(tmp_path):
    from fea_r1_runtime import prepare_runtime
    source=tmp_path/'broad-root';module=source/'FreeCAD/Mod/Fem/femmesh/meshtools.py';module.parent.mkdir(parents=True);module.write_text('unverified source')
    (source/'private.txt').write_text('unrelated content')
    with pytest.raises(ValueError,match='R1_DISTRIBUTION_ROOT'):prepare_runtime(source,tmp_path/'copy',config(source))
    assert not (tmp_path/'copy').exists()


@pytest.mark.parametrize('where',['same','nested','symlink'])
def test_unsafe_clone_target_is_rejected_without_touching_source(tmp_path,where):
    from fea_r1_runtime import prepare_runtime
    source=tmp_path/'source';source.mkdir();(source/'keep').write_text('keep')
    dest=source if where=='same' else source/'nested'
    if where=='symlink':
        dest=tmp_path/'alias'
        try:dest.symlink_to(source,target_is_directory=True)
        except OSError:pytest.skip('symlink creation requires an available OS privilege')
    with pytest.raises(ValueError):prepare_runtime(source,dest,config(source))
    assert (source/'keep').read_text()=='keep'


def runtime_fixture(tmp_path,monkeypatch):
    import fea_r1_runtime as runtime
    from fea_r1_patch import PROFILE,asset_identities
    root=tmp_path/'runtime';root.mkdir()
    module=root/'Mod/Fem/femmesh/meshtools.py';module.parent.mkdir(parents=True);module.write_bytes(b'native source fixture')
    tools={}
    for n,v in (('freecadcmd','1.1.3'),('gmsh','4.15.0'),('ccx','2.22')):
        p=root/(n+'.exe');p.write_bytes(n.encode());tools[n]={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'version':v}
    # Only replace the external approved-source trust anchor, not verification.
    # Live tests use the real pinned FreeCAD module and exact generated patch.
    monkeypatch.setattr(runtime,'approved_module_digest',lambda:hashlib.sha256(b'native source fixture').hexdigest())
    manifest={'schema_version':'FEAR1Runtime/v1','profile':PROFILE,'runtime_root':str(root),'module_path':str(module),'module_sha256':runtime.approved_module_digest(),'assets':asset_identities(),'tools':tools,'tree':runtime.tree_inventory(root),'native_identity':{'python':'3.11','qt':'6','occt':'7'}}
    p=tmp_path/'manifest.json';p.write_text(json.dumps(manifest))
    value={'schema_version':'FEAP0Configuration/v2','tools':tools,'runtime':{'profile':PROFILE,'manifest_path':str(p),'manifest_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}}
    return runtime,value,manifest,p


def test_manifest_accepts_only_unchanged_pinned_runtime(tmp_path,monkeypatch):
    runtime,value,_,_=runtime_fixture(tmp_path,monkeypatch)
    assert runtime.verify_runtime(value)['profile']=='freecad-1.1.3-fem-faceforce-r1'


@pytest.mark.parametrize('defect',['manifest','module','tool','extra_module','payload'])
def test_changed_runtime_cannot_start_a_native_probe(tmp_path,monkeypatch,defect):
    runtime,value,manifest,path=runtime_fixture(tmp_path,monkeypatch)
    if defect=='manifest':path.write_bytes(path.read_bytes()+b' ')
    if defect=='module':Path(manifest['module_path']).write_bytes(b'stale or foreign code')
    if defect=='tool':Path(value['tools']['gmsh']['path']).write_bytes(b'other executable')
    if defect=='extra_module':(Path(manifest['runtime_root'])/'unknown.py').write_text('extra module')
    if defect=='payload':monkeypatch.setattr(runtime,'asset_identities',lambda:{'changed':'payload'})
    with pytest.raises(ValueError,match='R1_'):runtime.verify_runtime(value)


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS signed-file metadata contract')
def test_clone_preserves_macos_detached_signature_metadata(tmp_path):
    from fea_r1_prepare import _clone
    source=tmp_path/'source';source.mkdir();signed=source/'packages.txt'
    signed.write_text('bounded signed text fixture\n')
    subprocess.run(['codesign','--force','--sign','-',str(signed)],check=True,capture_output=True)
    target=tmp_path/'copy';_clone(source,target)
    result=subprocess.run(['codesign','--verify','--strict',str(target/'packages.txt')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_console_disables_bytecode_before_worker_starts(tmp_path):
    import os
    from fea_native_probe import console_statement
    worker=tmp_path/'worker.py';worker.write_text('import sys; print(sys.dont_write_bytecode)\n')
    env=dict(os.environ);env.pop('PYTHONDONTWRITEBYTECODE',None)
    result=subprocess.run([sys.executable,'-c',console_statement([str(worker)])],env=env,capture_output=True,text=True,check=True)
    assert result.stdout.strip()=='True'


def loaded_fixture(tmp_path):
    import types
    source=('def get_ref_facenodes_areas():\n    return []\n'
            'def get_force_obj_face_nodeload_table():\n    return ("duplicated literal value", "duplicated literal value")\n')
    path=tmp_path/'meshtools.py';path.write_text(source)
    module=types.ModuleType('native_fixture');module.__file__=str(path)
    exec(compile(source,str(path),'exec'),module.__dict__)
    expected={'profile':'official-freecad-1.1.3','module_path':str(path),'module_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return module,expected,path


def test_loaded_code_identity_ignores_marshal_object_sharing(tmp_path):
    from fea_r1_runtime import loaded_module_receipt
    module,expected,_=loaded_fixture(tmp_path)
    code=module.get_force_obj_face_nodeload_table.__code__
    constants=tuple(tuple((' '+s)[1:] for s in value) if isinstance(value,tuple) else value for value in code.co_consts)
    module.get_force_obj_face_nodeload_table.__code__=code.replace(co_consts=constants)
    assert module.get_force_obj_face_nodeload_table()==('duplicated literal value',)*2
    assert loaded_module_receipt(expected,module)['module_sha256']==expected['module_sha256']


def test_stale_loaded_code_is_rejected_even_when_source_digest_matches(tmp_path):
    from fea_r1_runtime import loaded_module_receipt
    module,expected,path=loaded_fixture(tmp_path)
    path.write_text(path.read_text().replace('duplicated literal value','different behavior'))
    expected['module_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='R1_LOADED_CODE_MISMATCH'):loaded_module_receipt(expected,module)


def test_windows_device_prefix_does_not_create_a_different_native_module_name():
    from fea_r1_runtime import windows_path_key
    assert windows_path_key(r'\\?\D:\Runtime\Mod\Fem\meshtools.py')==windows_path_key(r'd:\runtime\mod\fem\meshtools.py')
    assert windows_path_key(r'D:\Other\meshtools.py')!=windows_path_key(r'D:\Runtime\meshtools.py')


def test_source_tool_mapping_uses_the_same_canonical_root_as_secure_fs(tmp_path):
    from fea_r1_prepare import relative_source_tool
    from mechanical_design_agent import secure_fs
    root=tmp_path/'runtime';tool=root/'bin/tool.exe';tool.parent.mkdir(parents=True);tool.write_bytes(b'tool')
    canonical=secure_fs.validate_managed_path(root,allow_missing_leaf=False).path
    assert relative_source_tool(tool,canonical)==Path('bin/tool.exe')


def test_identity_worker_uses_native_directory_presentation_and_owned_process(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import mechanical_design_agent.analysis_process as process
    from mechanical_design_agent import secure_fs
    from fea_r1_prepare import _identity
    from fea_r1_runtime import canonical_path
    root=tmp_path/'isolated';root.mkdir()
    checked=secure_fs.validate_managed_path(root,allow_missing_leaf=False).path
    seen=[]
    class Boundary:
        def start(self,request):
            seen.append(request)
            assert not request.env['FREECAD_USER_HOME'].startswith('\\\\?\\')
            assert Path(request.env['FREECAD_USER_HOME']).samefile(root/'identity')
            assert request.env['FREECAD_USER_DATA']==request.env['FREECAD_USER_TEMP']==request.env['FREECAD_USER_HOME']
            assert request.argv[2]==str(canonical_path(root/'identity')/'user.cfg')
            (request.cwd/'response.json').write_text(json.dumps({'identity':{'app_version':['1','1','3']}}))
            return object()
        def poll(self,owned):return SimpleNamespace(reason='exited',returncode=0)
        def terminate(self,owned,reason):pass
    monkeypatch.setattr(process,'ProcessController',Boundary)
    assert _identity(checked,{'freecadcmd':{'path':sys.executable}},root/'module.py','profile','a'*64)['identity']['app_version']==['1','1','3']
    assert len(seen)==1
