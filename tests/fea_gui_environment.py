"""Explicit P0 Windows software-OpenGL test environment, separate from R1.

Uses only the driver already in the official distribution. Existing reviewed
sources and binaries cannot change; new checked-hash caches are pinned before
GUI execution. This is test environment preparation, not product deployment.
"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

from mechanical_design_agent import secure_fs
from fea_gui_evidence import digest,pin,read_json,write_json
from fea_gui_session import owned_gui_process
from fea_r1_runtime import canonical_path,tree_inventory,verify_runtime

PROFILE='official-bundled-mesa-windows-x64-v1'
DRIVER_SHA256='53e75f1161a0a86259f8094423eecf88209e2d2b2056cb1d9fb85c76bff9a78f'


def assess_materialization(before,after):
    checks={
        'original_files_unchanged':all(after.get(k)==v for k,v in before.items()),
        'driver_was_absent':'bin/opengl32.dll' not in before,
        'official_driver_alias':after.get('bin/opengl32.dll')==before.get('bin/opengl32sw.dll') and 'bin/opengl32sw.dll' in before,
    }
    sources=[];valid=True
    for name in sorted(set(after)-set(before)-{'bin/opengl32.dll'}):
        path=PurePosixPath(name)
        match=re.fullmatch(r'(.+)\.cpython-311\.pyc',path.name)
        source=(path.parent.parent/(match.group(1)+'.py')).as_posix() if match else ''
        if not match or path.parent.name!='__pycache__' or source not in before:valid=False
        else:sources.append(source)
    checks['only_source_bound_python311_caches']=valid
    return {'status':'passed' if all(checks.values()) else 'failed','checks':checks,'compiled_sources':sorted(sources)}


def prepare_environment(config_path,scratch_root,output):
    if sys.platform!='win32':raise ValueError('Windows-only GUI environment')
    from fea_native_probe import parse_config
    config_path=secure_fs.validate_managed_path(Path(config_path),allow_missing_leaf=False).path
    output=secure_fs.validate_managed_path(Path(output),allow_missing_leaf=True).path
    if output.exists():raise ValueError('new GUI environment evidence required')
    config=parse_config(config_path);verify_runtime(config)
    manifest_path=Path(config['runtime']['manifest_path']);base=read_json(manifest_path)
    gui_manifest_path=config_path.parent/'gui-runtime-manifest.json';gui_config_path=config_path.parent/'gui-config.json'
    if gui_manifest_path.exists() or gui_config_path.exists():raise ValueError('GUI materialization files already exist')
    if base.get('gui_environment'):raise ValueError('GUI environment already materialized')
    runtime=canonical_path(Path(base['runtime_root']))
    if not os.path.samefile(config_path.parent,runtime.parent):raise ValueError('isolated R1 preparation directory required')
    driver=runtime/'bin/opengl32sw.dll';alias=runtime/'bin/opengl32.dll';python=runtime/'bin/python.exe'
    if base['tree'].get('bin/opengl32sw.dll')!={'sha256':DRIVER_SHA256} or digest(driver)!=DRIVER_SHA256:raise ValueError('official bundled Mesa identity')
    if alias.exists() or digest(python)!=base['tree']['bin/python.exe']['sha256']:raise ValueError('native GUI environment executable boundary')
    evidence=config_path.parent/'gui-materialization'
    evidence.mkdir();(evidence/'logs').mkdir()
    secure_fs.atomic_publish_new(evidence/'base-runtime-manifest.json',manifest_path.read_bytes())
    secure_fs.atomic_publish_new(evidence/'base-config.json',config_path.read_bytes())
    script=Path(__file__).with_name('fea_gui_compile.py');compiler_pin=pin(script)
    request=evidence/'compile-request.json';response=evidence/'compile-result.json'
    source_map={name:entry['sha256'] for name,entry in base['tree'].items() if name.endswith('.py') and 'sha256' in entry}
    existing_caches={name:entry['sha256'] for name,entry in base['tree'].items() if name.endswith('.pyc') and 'sha256' in entry}
    write_json(request,{'root':str(runtime),'sources':source_map,'existing_caches':existing_caches,'output':str(canonical_path(response.parent)/response.name)})
    # Same bytes, ordinary application-local DLL lookup, never a system DLL edit.
    secure_fs.atomic_publish_new(alias,driver.read_bytes())
    def build(owned):
        env={k:v for k,v in os.environ.items() if k in {'SYSTEMROOT','WINDIR','COMSPEC','PATHEXT'}}
        env.update(HOME=str(owned.path),USERPROFILE=str(owned.path),TEMP=str(owned.path),TMP=str(owned.path),PATH=os.defpath)
        return (str(python),'-I','-B',str(script),str(canonical_path(request))),env
    outcome=owned_gui_process(evidence/'logs',scratch_root,build,lambda *args:None,timeout_seconds=300)
    if outcome['outcome']!='exited' or outcome['returncode']!=0 or digest(script)!=compiler_pin['sha256']:raise ValueError('native checked-hash compilation failed')
    compiled=read_json(response);after=tree_inventory(runtime);assessment=assess_materialization(base['tree'],after)
    write_json(evidence/'materialization-assessment.json',{**assessment,'added':sorted(set(after)-set(base['tree'])),'removed':sorted(set(base['tree'])-set(after)),'changed':sorted(k for k in set(after)&set(base['tree']) if after[k]!=base['tree'][k])})
    if assessment['status']!='passed':raise ValueError('GUI preparation changed an original file or added an unreviewed entry')
    expected_caches=set(after)-set(base['tree'])-{'bin/opengl32.dll'}
    if set(compiled['compiled'])!=expected_caches:raise ValueError('cache provenance inventory mismatch')
    for name,entry in compiled['compiled'].items():
        if after[name]!={'sha256':entry['sha256']} or source_map[entry['source']]!=entry['source_sha256']:raise ValueError('cache provenance checksum mismatch')
    profile={'schema_version':'P0GuiEnvironment/v1','profile':PROFILE,'base_manifest_sha256':config['runtime']['manifest_sha256'],'base_manifest':pin(evidence/'base-runtime-manifest.json'),'base_config':pin(evidence/'base-config.json'),'driver':{'relative_path':'bin/opengl32.dll','sha256':DRIVER_SHA256,'source_relative_path':'bin/opengl32sw.dll'},'compiler':compiler_pin,'embedded_python_sha256':base['tree']['bin/python.exe']['sha256'],'compile_result':pin(response),'assessment':assessment,'scope':'New materialized Windows software OpenGL CI revision, not a hardware graphics claim; original numeric manifests retained'}
    materialized={**base,'tree':after,'gui_environment':profile}
    write_json(gui_manifest_path,materialized)
    config['runtime'].update(manifest_path=str(gui_manifest_path),manifest_sha256=digest(gui_manifest_path))
    write_json(gui_config_path,config)
    verify_runtime(config)
    write_json(output,{**profile,'config':pin(gui_config_path),'runtime_manifest_sha256':config['runtime']['manifest_sha256'],'status':'prepared'})
    return output


def verify_environment(path,config,*,expected_native=None):
    value=read_json(path);manifest=read_json(config['runtime']['manifest_path'])
    if value.get('schema_version')!='P0GuiEnvironment/v1' or value.get('profile')!=PROFILE or value.get('status')!='prepared':raise ValueError('GUI environment profile')
    if value['runtime_manifest_sha256']!=config['runtime']['manifest_sha256'] or manifest.get('gui_environment',{}).get('profile')!=PROFILE:raise ValueError('GUI environment manifest binding')
    expected=manifest['gui_environment']
    if any(value.get(k)!=v for k,v in expected.items()):raise ValueError('GUI environment provenance mismatch')
    for key in ('config','base_manifest','base_config','compile_result'):
        if digest(value[key]['path'])!=value[key]['sha256']:raise ValueError('GUI environment evidence changed')
    if manifest['tree'].get('bin/opengl32.dll')!={'sha256':DRIVER_SHA256}:raise ValueError('GUI driver identity')
    native=verify_runtime(config)
    if expected_native is not None and native!=expected_native:raise ValueError('GUI environment native identity changed')
    return value
