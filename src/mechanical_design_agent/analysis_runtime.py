"""Explicit new-copy R2 preparation from a fully verified isolated R1 runtime.

This does not download tools, change a user's installation or activate a solve
capability. Numerical/GUI qualification is a separate release requirement.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

from . import secure_fs
from .analysis_native import _environment, _native_path, _read, verify_settings
from .analysis_process import ProcessController, ProcessRequest
from .analysis_runtime_patch import PROFILE, PATCHED_SHA256, patch_assets, patch_output_source
from .analysis_scratch import native_scratch
from .analysis_types import AnalysisError
from .package_resources import freecad_scripts_directory


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if getattr(path,'is_junction',lambda:False)(): raise AnalysisError('FEA_RUNTIME_INVALID','runtime junction is unsupported')
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root): raise AnalysisError('FEA_RUNTIME_INVALID','runtime link leaves its root')
            result[name] = {'symlink': os.readlink(path)}
        elif path.is_file(): result[name] = {'sha256': _read(path).sha256}
    return result


def _signature(root, *, sign=False):
    argv = ['codesign','--force','--sign','-','--preserve-metadata=identifier,entitlements,flags,runtime',str(root)] if sign else ['codesign','--verify','--strict',str(root)]
    outcome = subprocess.run(argv,capture_output=True,text=True,timeout=90)
    if outcome.returncode: raise AnalysisError('FEA_RUNTIME_SIGNATURE_FAILED','isolated application signature failed: '+outcome.stderr[-2000:])
    return {'argv':argv,'returncode':outcome.returncode,'stderr':outcome.stderr}


def _readonly(root):
    if os.name == 'posix':
        for path in reversed([root,*root.rglob('*')]):
            if not path.is_symlink(): path.chmod(path.stat().st_mode & ~0o222)


def _process(argv, directory, home, scratch, *, timeout=90):
    controller = ProcessController()
    identity = controller.start(ProcessRequest(argv=tuple(map(_native_path,argv)),cwd=directory,env=_environment(home,scratch),stdout_path=directory/'stdout.log',stderr_path=directory/'stderr.log',timeout_seconds=timeout,max_log_bytes=16*1024*1024))
    outcome = None
    try:
        while (outcome := controller.poll(identity)) is None: time.sleep(.05)
    finally: controller.terminate(identity,reason='interrupted')
    secure_fs.atomic_publish_new(directory/'process.json',json.dumps(asdict(outcome)).encode())
    if outcome.reason != 'exited' or outcome.returncode != 0:
        raise AnalysisError('FEA_RUNTIME_PROBE_FAILED','fixed isolated preparation process failed; inspect retained logs')


def prepare_nodal_rf_runtime(settings, destination):
    verify_settings(settings)
    config = settings['configuration']
    if config['runtime']['profile'] != 'freecad-1.1.3-fem-faceforce-r1':
        raise AnalysisError('FEA_PATCH_BASE_CHANGED','R2 preparation needs the verified R1 base, not an already patched runtime')
    assets = patch_assets()
    base = json.loads(_read(settings['runtime']['manifest_path']).content)
    source = secure_fs.validate_managed_path(Path(base['runtime_root']),allow_missing_leaf=False).path
    target = secure_fs.validate_managed_path(Path(destination),allow_missing_leaf=True).path
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target) or target.is_relative_to(Path(__file__).resolve().parent):
        raise AnalysisError('FEA_RUNTIME_TARGET_UNSAFE','select a new separate runtime directory outside the source installation')
    module_relative = secure_fs.validate_managed_path(Path(base['module_path']),allow_missing_leaf=False).path.relative_to(source)
    output_relative = module_relative.parent.parent/'femsolver/calculix/write_step_output.py'
    original = _read(source/output_relative)
    patched = patch_output_source(original.content)
    secure_fs.ensure_managed_directory(target,parents=False,exist_ok=False)
    runtime = target/source.name
    if sys.platform == 'darwin':
        subprocess.run(['/usr/bin/ditto','--rsrc','--extattr',str(source),str(runtime)],check=True,capture_output=True,timeout=300)
    else: shutil.copytree(source,runtime,symlinks=True)
    if inventory(runtime) != base['tree']:
        raise AnalysisError('FEA_RUNTIME_COPY_CHANGED','new copy is not byte-identical to its verified base')
    # Only the separately owned copy becomes writable during materialization.
    for path in [runtime,*runtime.rglob('*')]:
        if not path.is_symlink(): path.chmod(path.stat().st_mode | stat.S_IWUSR)
    output = runtime/output_relative
    if os.path.samefile(output,source/output_relative): raise AnalysisError('FEA_RUNTIME_TARGET_UNSAFE','native patch target shares its source file')
    output.write_bytes(patched)
    for cache in [*output.parent.glob('__pycache__/write_step_output.*.pyc'),output.with_suffix('.pyc')]: cache.unlink(missing_ok=True)
    tools = {}
    for name, tool in config['tools'].items():
        relative = Path(settings['tools'][name]['path']).relative_to(source)
        tools[name] = {**tool,'path':str(runtime/relative)}
    signatures = {}
    if sys.platform == 'darwin':
        signatures['signed_copy'] = _signature(runtime,sign=True)
        signatures['verified_copy'] = _signature(runtime)
    for tool in tools.values(): tool['sha256'] = _read(tool['path']).sha256
    # Embedded FreeCAD startup can create caches before its console script
    # sets dont_write_bytecode. Preserve the signed source tree from startup.
    _readonly(runtime)
    identity_dir = secure_fs.ensure_managed_directory(target/'identity',parents=False,exist_ok=False).path
    with native_scratch(Path(settings['native_scratch_root'])) as owned:
        home = secure_fs.ensure_managed_directory(owned.path/'home',parents=False,exist_ok=False).path
        for name in ('user.cfg','system.cfg'): secure_fs.atomic_publish_new(home/name,b'<FCParameters><FCParamGroup Name="Root"/></FCParameters>')
        # Materialize just this changed module's checked-hash cache in Windows;
        # all existing source-bound caches in the base remain unchanged.
        if os.name == 'nt':
            compile_dir = secure_fs.ensure_managed_directory(target/'compile',parents=False,exist_ok=False).path
            python = runtime/'bin/python.exe'
            _process([python,'-I','-B','-c','import py_compile,sys;py_compile.compile(sys.argv[1],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)',output],compile_dir,home,owned.path)
        with freecad_scripts_directory() as resources:
            script = _read(resources/'analysis_runtime_identity.py')
        secure_fs.atomic_publish_new(owned.path/'identity.py',script.content)
        request = {'temp':_native_path(owned.path),'home':_native_path(home),'faceforce':{'path':_native_path(runtime/module_relative),'sha256':base['module_sha256']},'output':{'path':_native_path(output),'sha256':PATCHED_SHA256}}
        request_bytes = json.dumps(request,sort_keys=True).encode()
        secure_fs.atomic_publish_new(owned.path/'request.json',request_bytes)
        args = [_native_path(owned.path/name) for name in ('identity.py','request.json','response.json')]
        statement = 'import sys,runpy;sys.dont_write_bytecode=True;sys.argv='+ascii(args)+";runpy.run_path(sys.argv[0],run_name='__main__')"
        _process([tools['freecadcmd']['path'],'-u',home/'user.cfg','-s',home/'system.cfg','-c',statement],identity_dir,home,owned.path)
        response = _read(owned.path/'response.json')
        secure_fs.atomic_publish_new(identity_dir/'native.json',response.content)
        probe = json.loads(response.content)
        if probe.get('status') != 'passed' or probe['request_sha256'] != hashlib.sha256(request_bytes).hexdigest() or probe['script_sha256'] != script.sha256:
            raise AnalysisError('FEA_RUNTIME_PROBE_FAILED','actual loaded native source or bytecode identity failed; inspect native.json')
    verify_settings(settings)
    if sys.platform == 'darwin': signatures['after_probe'] = _signature(runtime)
    tree = inventory(runtime)
    result = {'schema_version':'FEAR2Runtime/v1','profile':PROFILE,'runtime_root':str(runtime),'module_path':str(runtime/module_relative),'module_sha256':base['module_sha256'],'output_module_path':str(output),'output_module_sha256':PATCHED_SHA256,'source_commit':base['source_commit'],'source_unchanged':True,'base_manifest_sha256':settings['runtime']['manifest_sha256'],'source_archive_sha256':base.get('source_archive_sha256'),'tools':tools,'native_identity':probe['native_identity'],'loaded_identity':probe['modules'],'patch':assets['provenance'],'signatures':signatures,'tree':tree,'changed':sorted(name for name in set(tree)|set(base['tree']) if tree.get(name)!=base['tree'].get(name)),'qualification':'not_executed'}
    manifest = target/'runtime-manifest.json'
    secure_fs.atomic_publish_new(manifest,json.dumps(result,indent=2).encode())
    created = {**config,'tools':tools,'runtime':{'profile':PROFILE,'manifest_path':str(manifest),'manifest_sha256':_read(manifest).sha256}}
    path = target/'config.json'
    secure_fs.atomic_publish_new(path,json.dumps(created,indent=2).encode())
    return path
