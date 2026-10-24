"""Prepare a new isolated dependency copy; never modify source installation."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
from fea_r1_patch import ASSETS,BASE_SHA256,PROFILE,SOURCE_COMMIT,canonical_baseline,asset_identities,digest,patched_source,patch_bytes
from fea_r1_runtime import tree_inventory


def _signature(root, *, sign=False):
    argv=['codesign','--force','--sign','-','--preserve-metadata=identifier,entitlements,flags,runtime',str(root)] if sign else ['codesign','--verify','--strict',str(root)]
    result=subprocess.run(argv,capture_output=True,text=True,timeout=90)
    if result.returncode:raise ValueError('R1_ISOLATED_SIGNATURE: '+result.stderr[-1500:])
    return {'argv':argv,'returncode':result.returncode,'stderr':result.stderr}


def _sealed_files(source):
    sealed=set()
    for signature in source.rglob('CodeResources'):
        if signature.parent.name!='_CodeSignature' or signature.is_symlink():continue
        facts=plistlib.loads(signature.read_bytes())
        for key in set(facts.get('files',{}))|set(facts.get('files2',{})):
            sealed.add((signature.parent.parent/key).relative_to(source).as_posix())
    return sealed


def _clone(source,target):
    sealed=_sealed_files(source) if sys.platform=='darwin' else set()
    excluded=[]
    def ignore(parent,names):
        ignored=[]
        for name in names:
            path=Path(parent)/name;relative=path.relative_to(source).as_posix()
            cache=name=='__pycache__' or name.endswith(('.pyc','.pyo'))
            included=relative in sealed or any(v.startswith(relative+'/') for v in sealed) if cache else True
            if cache and not included:ignored.append(name);excluded.append(relative)
        return ignored
    if sys.platform=='darwin':
        # copy2/copytree omit macOS detached code signatures/resource metadata.
        # ditto preserves those attributes, including existing safety metadata.
        subprocess.run(['/usr/bin/ditto','--rsrc','--extattr',str(source),str(target)],check=True,capture_output=True,timeout=300)
        for path in sorted(target.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            relative=path.relative_to(target).as_posix()
            if path.name.endswith(('.pyc','.pyo')) and relative not in sealed:
                path.unlink();excluded.append(relative)
            elif path.name=='__pycache__' and not path.is_symlink() and not any(path.iterdir()):path.rmdir()
    else:shutil.copytree(source,target,symlinks=True,ignore=ignore)
    return excluded


def _identity(root,tools,module,profile,module_digest):
    from mechanical_design_agent.analysis_process import ProcessController,ProcessRequest
    from fea_native_probe import console_statement
    directory=root/'identity';directory.mkdir()
    request=directory/'request.json';response=directory/'response.json'
    request.write_text(json.dumps({'module_path':str(module),'module_sha256':module_digest,'profile':profile}),encoding='utf-8')
    worker=Path(__file__).with_name('fea_r1_identity.py')
    env={k:v for k,v in os.environ.items() if k in {'SYSTEMROOT','WINDIR','COMSPEC','PATHEXT'}}
    env.update(HOME=str(directory),USERPROFILE=str(directory),FREECAD_USER_HOME=str(directory),FREECAD_USER_DATA=str(directory),TMPDIR=str(directory),TEMP=str(directory),TMP=str(directory),FREECAD_USER_TEMP=str(directory),PATH=os.defpath,QT_QPA_PLATFORM='offscreen',PYTHONDONTWRITEBYTECODE='1')
    argv=(tools['freecadcmd']['path'],'-u',str(directory/'user.cfg'),'-s',str(directory/'system.cfg'),'-c',console_statement([str(worker),str(request),str(response)]))
    controller=ProcessController();owned=controller.start(ProcessRequest(argv=argv,cwd=directory,env=env,stdout_path=directory/'stdout.log',stderr_path=directory/'stderr.log',timeout_seconds=90))
    try:
        while (outcome:=controller.poll(owned)) is None:time.sleep(.05)
    finally:controller.terminate(owned,reason='interrupted')
    if outcome.reason!='exited' or outcome.returncode or not response.is_file():raise ValueError('R1_IDENTITY_WORKER_FAILED')
    result=json.loads(response.read_text(encoding='utf-8'))
    if 'error' in result:raise ValueError('R1_IDENTITY_WORKER_FAILED: '+result['error'])
    if '.'.join(result['identity']['app_version'][:3])!='1.1.3':raise ValueError('R1_NATIVE_VERSION')
    return result


def _readonly(root):
    if os.name!='posix':return
    for path in root.rglob('*'):
        if not path.is_symlink():path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


def relative_source_tool(path,source):
    from mechanical_design_agent import secure_fs
    canonical=secure_fs.validate_managed_path(Path(path),allow_missing_leaf=False).path
    root=secure_fs.validate_managed_path(Path(source),allow_missing_leaf=False).path
    if not canonical.is_relative_to(root):raise ValueError('R1_BASE_TOOL_OUTSIDE_ROOT')
    return canonical.relative_to(root)


def prepare_runtime(source_root,destination,base_config,*,source_archive_sha256=None,baseline=False):
    from mechanical_design_agent import secure_fs
    from fea_native_probe import _validate_config,verify_tool
    source=secure_fs.validate_managed_path(Path(source_root),allow_missing_leaf=False).path
    target=secure_fs.validate_managed_path(Path(destination),allow_missing_leaf=True).path
    repository=Path(__file__).resolve().parents[1]
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target) or target.is_relative_to(repository):raise ValueError('R1_UNSAFE_CLONE_TARGET')
    relative=Path('Mod/Fem/femmesh/meshtools.py')
    if (source/relative).is_file():modules=[source/relative]
    elif (source/'Contents/Resources'/relative).is_file():modules=[source/'Contents/Resources'/relative]
    else:
        children=list(source.iterdir())
        if len(children)!=1 or not children[0].is_dir() or children[0].is_symlink() or not (children[0]/relative).is_file():raise ValueError('R1_DISTRIBUTION_ROOT')
        source=children[0];modules=[source/relative]
    if len(modules)!=1 or modules[0].is_symlink():raise ValueError('R1_BASE_MODULE_DIGEST')
    original=modules[0].read_bytes();canonical_baseline(original);replacement=patched_source(original)
    if replacement and digest(replacement)!=json.loads((ASSETS/'provenance.json').read_text())['patched_sha256']:raise ValueError('R1_PATCHED_MODULE_IDENTITY')
    if patch_bytes(original)!=(ASSETS/'meshtools.patch').read_bytes():raise ValueError('R1_REVIEWED_DIFF_MISMATCH')
    _validate_config(base_config)
    if base_config['schema_version']!='FEAP0Configuration/v1':raise ValueError('R1_BASE_CONFIG_REQUIRED')
    relative_tools={name:relative_source_tool(pin['path'],source) for name,pin in base_config['tools'].items()}
    for pin in base_config['tools'].values():
        verify_tool(pin)
    source_identity={'module_sha256':digest(original),'tools':{n:verify_tool(p) for n,p in base_config['tools'].items()}}
    target.mkdir(mode=0o700)
    runtime=target/source.name;excluded=_clone(source,runtime)
    # Reject external symlinks before starting any copied executable.
    baseline_tree_sha256=digest(json.dumps(tree_inventory(runtime),sort_keys=True).encode('utf-8'))
    signatures={}
    if sys.platform=='darwin':signatures['baseline']=_signature(runtime)
    module=runtime/modules[0].relative_to(source)
    if not baseline:
        module.write_bytes(replacement)
        for cached in module.parent.glob('__pycache__/meshtools.*.pyc'):cached.unlink()
        (module.parent/'meshtools.pyc').unlink(missing_ok=True)
        if sys.platform=='darwin':
            signatures['local_sign']=_signature(runtime,sign=True)
            signatures['patched']=_signature(runtime)
    tools={n:{**pin,'path':str(runtime/relative_tools[n])} for n,pin in base_config['tools'].items()}
    # Signing the app envelope may change a launcher's signature bytes.
    for pin in tools.values():pin['sha256']=digest(Path(pin['path']).read_bytes())
    _readonly(runtime)
    identity=_identity(target,tools,module,'official-freecad-1.1.3' if baseline else PROFILE,digest(original) if baseline else digest(replacement))
    if sys.platform=='darwin':signatures['after_identity']=_signature(runtime)
    source_after={'module_sha256':digest(modules[0].read_bytes()),'tools':{n:verify_tool(p) for n,p in base_config['tools'].items()}}
    if source_identity!=source_after:raise ValueError('R1_SOURCE_INSTALL_CHANGED')
    manifest={'schema_version':'FEAR1Runtime/v1','profile':'official-freecad-1.1.3' if baseline else PROFILE,'runtime_root':str(runtime),'module_path':str(module),'module_sha256':digest(module.read_bytes()),'assets':asset_identities(),'source_commit':SOURCE_COMMIT,'source_archive_sha256':source_archive_sha256,'baseline_tree_sha256':baseline_tree_sha256,'source_identity':source_identity,'source_unchanged':True,'excluded_generated_caches':excluded,'signatures':signatures,'tools':tools,'native_identity':identity['identity'],'loaded_identity':identity['module'],'tree':tree_inventory(runtime)}
    manifest_path=target/'runtime-manifest.json';manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    config={'schema_version':'FEAP0Configuration/v1' if baseline else 'FEAP0Configuration/v2','tools':tools,'timeout_seconds':base_config.get('timeout_seconds',300)}
    if not baseline:config['runtime']={'profile':PROFILE,'manifest_path':str(manifest_path),'manifest_sha256':digest(manifest_path.read_bytes())}
    config_path=target/'config.json';config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    return config_path
