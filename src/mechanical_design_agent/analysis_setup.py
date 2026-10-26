"""Explicit, new-copy materialization of the two reviewed FreeCAD patches.

No downloads, source installation writes, user add-ons or test imports. Native
execution starts only after the complete copied inventory and signatures pass.
"""
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys

from . import secure_fs
from .analysis_native import _native_path,_read
from .analysis_runtime import inventory,_signature,_readonly,_process
from .analysis_runtime_patch import PROFILE,PATCHED_SHA256,patch_assets,patch_output_source
from .analysis_scratch import native_scratch
from .analysis_types import AnalysisError
from .package_resources import analysis_resources_directory,freecad_scripts_directory

R1_BASE='cca1ab3f4aaded4e058295316b5276420538d3170d745c2fc96420c258cf58cc'
R1_BASE_CRLF='0bb170d0f468a106e8ead419fb1b42bddf26e29394e9ef3bf8064f299c6af6d1'
R1_PATCHED='ca4af36e48f0b1cb6b3b57e7c63e61ed7a3de768df497d50d4fa171493634c53'
SOURCE_COMMIT='145529fe741292ff0b3977a01195bf0247425794'
_ASSETS={'native_faceforce.py':'926f8318b69f09be3fcfa767550e13edfb80d4dc90420f41d21f73f05ab08bbd','meshtools.patch':'a213a0390713c0880fe3e0705eaf15f339f3696da87faf3d78248a9e41abb9a2','provenance.json':'41176afbbaa98687d22799525f530c735277ce703e61dfd20a21e7c9cbad5da5','COPYING.LESSER':'1dfab741fe12676a0a7e8166ad4a74670cb536ddff56b84b36a48fab28a1a110'}


def _sha(data):return hashlib.sha256(data).hexdigest()


def faceforce_assets():
    with analysis_resources_directory() as resources:
        assets={name:(resources/'runtime-patches/faceforce-r1'/name).read_bytes() for name in _ASSETS}
    if any(_sha(assets[name])!=pin for name,pin in _ASSETS.items()):raise AnalysisError('FEA_PATCH_ASSET_CHANGED','packaged R1 review assets changed')
    return {**assets,'identities':dict(_ASSETS)}


def patch_faceforce_source(original):
    assets=faceforce_assets()
    if type(original) is not bytes or _sha(original) not in (R1_BASE,R1_BASE_CRLF):raise AnalysisError('FEA_PATCH_BASE_CHANGED','only the exact reviewed official FreeCAD 1.1.3 source is supported')
    original=original.replace(b'\r\n',b'\n');text=original.decode('utf-8');lines=text.splitlines(keepends=True);tree=ast.parse(text)
    force=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_force_obj_face_nodeload_table')
    areas=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_ref_facenodes_areas')
    six=next(n for n in ast.walk(areas) if isinstance(n,ast.If) and ast.unparse(n.test)=='femmesh_facetype == 6')
    body=('            points = [femnodes_mesh[n] for n in face_table[mf]]\n'
          '            weights = t6_weights([(p.x, p.y, p.z) for p in points])\n'
          '            node_area_table.extend(zip(face_table[mf], weights))\n')
    changes=[(six.body[0].lineno-1,six.body[-1].end_lineno,body),(force.lineno-1,force.end_lineno,assets['native_faceforce.py'].decode('utf-8').rstrip()+'\n')]
    for start,end,replacement in sorted(changes,reverse=True):lines[start:end]=[replacement]
    result=''.join(lines).encode('utf-8')
    patch=''.join(difflib.unified_diff(original.decode().splitlines(keepends=True),result.decode().splitlines(keepends=True),fromfile='a/src/Mod/Fem/femmesh/meshtools.py',tofile='b/src/Mod/Fem/femmesh/meshtools.py')).encode('utf-8')
    if _sha(result)!=R1_PATCHED or patch!=assets['meshtools.patch']:raise AnalysisError('FEA_PATCH_DIFF_CHANGED','native source differs from the reviewed R1 patch')
    return result


def discover_tools(source,explicit=None):
    source=secure_fs.validate_managed_path(Path(source),allow_missing_leaf=False).path;explicit=explicit or {}
    if set(explicit)-{'freecadcmd','freecadgui','gmsh','ccx'}:raise AnalysisError('FEA_TOOL_UNSUPPORTED','only the four fixed native executable roles are supported')
    relative={'freecadcmd':('bin/FreeCADCmd.exe','bin/freecadcmd','Contents/Resources/bin/freecadcmd'),'freecadgui':('bin/FreeCAD.exe','bin/freecad','Contents/Resources/bin/freecad'),'gmsh':('bin/gmsh.exe','bin/gmsh','Contents/Resources/bin/gmsh'),'ccx':('bin/ccx.exe','bin/ccx','Contents/Resources/bin/ccx')}
    result={}
    for role,paths in relative.items():
        candidates=[Path(explicit[role])] if role in explicit else [source/path for path in paths if (source/path).is_file()]
        if len(candidates)!=1:raise AnalysisError('FEA_TOOL_DISCOVERY_REQUIRED','select exactly one executable inside the reviewed distribution',details={'tool':role})
        located=secure_fs.validate_managed_path(candidates[0],allow_missing_leaf=False).path
        if not located.is_relative_to(source):raise AnalysisError('FEA_TOOL_OUTSIDE_RUNTIME','all four executables must belong to the selected distribution',details={'tool':role})
        result[role]={'relative_path':located.relative_to(source).as_posix(),'sha256':_read(located).sha256}
    return result


def _clone(source,target):
    sealed=set();excluded=[]
    if sys.platform=='darwin':
        for signature in source.rglob('CodeResources'):
            if signature.parent.name!='_CodeSignature' or signature.is_symlink():continue
            facts=plistlib.loads(signature.read_bytes())
            for key in set(facts.get('files',{}))|set(facts.get('files2',{})):sealed.add((signature.parent.parent/key).relative_to(source).as_posix())
        subprocess.run(['/usr/bin/ditto','--rsrc','--extattr',str(source),str(target)],check=True,capture_output=True,timeout=300)
        for path in sorted(target.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            relative=path.relative_to(target).as_posix()
            if path.name.endswith(('.pyc','.pyo')) and relative not in sealed:path.unlink();excluded.append(relative)
            elif path.name=='__pycache__' and not path.is_symlink() and not any(path.iterdir()):path.rmdir()
    else:
        def ignore(parent,names):
            result=[]
            for name in names:
                if name=='__pycache__' or name.endswith(('.pyc','.pyo')):
                    result.append(name);excluded.append((Path(parent)/name).relative_to(source).as_posix())
            return result
        shutil.copytree(source,target,symlinks=True,ignore=ignore)
    return excluded


def prepare_runtime(source_root,destination,*,native_scratch_root,tools=None,software_opengl=False):
    source=secure_fs.validate_managed_path(Path(source_root),allow_missing_leaf=False).path
    target=secure_fs.validate_managed_path(Path(destination),allow_missing_leaf=True).path
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target) or target.is_relative_to(Path(__file__).resolve().parent):raise AnalysisError('FEA_RUNTIME_TARGET_UNSAFE','select a new separate directory outside the source installation and installed package')
    scratch=secure_fs.validate_managed_path(Path(native_scratch_root),allow_missing_leaf=True).path
    if type(software_opengl) is not bool or (software_opengl and os.name!='nt'):raise AnalysisError('FEA_GRAPHICS_PROFILE_UNSUPPORTED','the bundled software OpenGL materialization is Windows-only')
    if not str(scratch).isascii() or scratch.is_relative_to(source) or scratch.is_relative_to(target):raise AnalysisError('FEA_SCRATCH_ASCII_REQUIRED','W1 requires a separate private ASCII native scratch directory')
    candidates=[source/path for path in ('Mod/Fem/femmesh/meshtools.py','Contents/Resources/Mod/Fem/femmesh/meshtools.py') if (source/path).is_file()]
    if len(candidates)!=1:raise AnalysisError('FEA_DISTRIBUTION_ROOT','select the actual official FreeCAD distribution root containing its native FEM modules')
    module=candidates[0];relative=module.relative_to(source);output_relative=relative.parent.parent/'femsolver/calculix/write_step_output.py'
    replacement=patch_faceforce_source(_read(module).content);rf_replacement=patch_output_source(_read(source/output_relative).content)
    original_tools=discover_tools(source,tools);before=inventory(source);source_signatures={}
    if sys.platform=='darwin':source_signatures['source']=_signature(source)
    secure_fs.ensure_managed_directory(target,parents=False,exist_ok=False);runtime=target/source.name
    excluded=_clone(source,runtime);copied=inventory(runtime)
    # Every copied file must match the protected source before either patch.
    if any(before.get(name)!=pin for name,pin in copied.items()):raise AnalysisError('FEA_RUNTIME_COPY_CHANGED','copied native bytes differ from the protected source')
    if any(name not in copied and not any(name==item or name.startswith(item+'/') for item in excluded) for name in before):raise AnalysisError('FEA_RUNTIME_COPY_CHANGED','native copy omitted a non-cache source file')
    signatures=dict(source_signatures);graphics={'mode':'desktop','materialization':'none'}
    if sys.platform=='darwin':signatures['baseline']=_signature(runtime)
    for path in [runtime,*runtime.rglob('*')]:
        if not path.is_symlink():path.chmod(path.stat().st_mode|stat.S_IWUSR)
    for rel,content in ((relative,replacement),(output_relative,rf_replacement)):
        path=runtime/rel
        if os.path.samefile(path,source/rel):raise AnalysisError('FEA_RUNTIME_TARGET_UNSAFE','native copy shares a patched source file')
        path.write_bytes(content)
        for cache in [*path.parent.glob('__pycache__/'+path.stem+'.*.pyc'),path.with_suffix('.pyc')]:cache.unlink(missing_ok=True)
    if software_opengl:
        driver=runtime/'bin/opengl32sw.dll';alias=runtime/'bin/opengl32.dll';expected='53e75f1161a0a86259f8094423eecf88209e2d2b2056cb1d9fb85c76bff9a78f'
        if alias.exists() or _read(driver).sha256!=expected:raise AnalysisError('FEA_GRAPHICS_PROFILE_UNSUPPORTED','only the exact official bundled Windows software OpenGL driver is supported')
        secure_fs.atomic_publish_new(alias,_read(driver).content)
        graphics={'mode':'official-bundled-mesa-windows-x64-v1','driver_source':'bin/opengl32sw.dll','driver_alias':'bin/opengl32.dll','sha256':expected}
    if sys.platform=='darwin':
        signatures['signed_copy']=_signature(runtime,sign=True);signatures['verified_copy']=_signature(runtime)
    # Mac startup must not add caches to the signed application envelope.
    _readonly(runtime)
    pins={name:{'path':str(runtime/tool['relative_path']),'sha256':_read(runtime/tool['relative_path']).sha256,'version':'1.1.3' if name.startswith('freecad') else ('4.15.0' if name=='gmsh' else 'pending')} for name,tool in original_tools.items()}
    identity_dir=secure_fs.ensure_managed_directory(target/'identity',parents=False,exist_ok=False).path
    with native_scratch(scratch) as owned:
        home=secure_fs.ensure_managed_directory(owned.path/'home',parents=False,exist_ok=False).path
        for name in ('user.cfg','system.cfg'):secure_fs.atomic_publish_new(home/name,b'<FCParameters><FCParamGroup Name="Root"/></FCParameters>')
        if os.name=='nt':
            # Windows has no POSIX write seal. Materialize source-bound caches
            # using only the distribution's own Python, before inventory seal.
            compile_dir=secure_fs.ensure_managed_directory(target/'compile',parents=False,exist_ok=False).path
            cache_before=inventory(runtime)
            with freecad_scripts_directory() as resources:compiler=_read(resources/'analysis_runtime_compile.py')
            script_path=owned.path/'compile.py';secure_fs.atomic_publish_new(script_path,compiler.content)
            compile_request={'root':_native_path(runtime),'sources':{name:entry['sha256'] for name,entry in cache_before.items() if name.endswith('.py') and 'sha256' in entry},'existing_caches':{name:entry['sha256'] for name,entry in cache_before.items() if name.endswith('.pyc') and 'sha256' in entry},'output':_native_path(owned.path/'compiled.json')}
            secure_fs.atomic_publish_new(owned.path/'compile-request.json',json.dumps(compile_request).encode())
            _process([runtime/'bin/python.exe','-I','-B',script_path,owned.path/'compile-request.json'],compile_dir,home,owned.path,timeout=300)
            compiled=_read(owned.path/'compiled.json');secure_fs.atomic_publish_new(compile_dir/'result.json',compiled.content);cache_result=json.loads(compiled.content);cache_after=inventory(runtime)
            if _read(script_path).sha256!=compiler.sha256 or any(cache_after.get(name)!=pin for name,pin in cache_before.items()) or set(cache_after)-set(cache_before)!=set(cache_result['compiled']):raise AnalysisError('FEA_RUNTIME_COPY_CHANGED','cache materialization changed original native bytes or added an unregistered file')
            for name,entry in cache_result['compiled'].items():
                if cache_after[name]!={'sha256':entry['sha256']} or compile_request['sources'].get(entry['source'])!=entry['source_sha256']:raise AnalysisError('FEA_RUNTIME_COPY_CHANGED','compiled native cache lost its source binding')
        with freecad_scripts_directory() as resources:script=_read(resources/'analysis_runtime_identity.py')
        secure_fs.atomic_publish_new(owned.path/'identity.py',script.content)
        request={'temp':_native_path(owned.path),'home':_native_path(home),'faceforce':{'path':_native_path(runtime/relative),'sha256':R1_PATCHED},'output':{'path':_native_path(runtime/output_relative),'sha256':PATCHED_SHA256}}
        data=json.dumps(request,sort_keys=True).encode();secure_fs.atomic_publish_new(owned.path/'request.json',data)
        args=[_native_path(owned.path/name) for name in ('identity.py','request.json','response.json')]
        statement='import sys,runpy;sys.dont_write_bytecode=True;sys.argv='+ascii(args)+";runpy.run_path(sys.argv[0],run_name='__main__')"
        _process([pins['freecadcmd']['path'],'-u',home/'user.cfg','-s',home/'system.cfg','-c',statement],identity_dir,home,owned.path)
        response=_read(owned.path/'response.json');secure_fs.atomic_publish_new(identity_dir/'native.json',response.content);probe=json.loads(response.content)
        if probe.get('status')!='passed' or probe.get('request_sha256')!=_sha(data) or probe.get('script_sha256')!=script.sha256:raise AnalysisError('FEA_RUNTIME_PROBE_FAILED','actual isolated native source, bytecode, version or temp identity failed')
        for role,flag,pattern,supported in [('gmsh','--version',r'(?<!\d)(4\.15\.0)(?!\d)',{'4.15.0'}),('ccx','-v',r'Version\s+(2\.\d+)\b',{'2.22','2.23'})]:
            directory=secure_fs.ensure_managed_directory(target/('version-'+role),parents=False,exist_ok=False).path
            _process([pins[role]['path'],flag],directory,home,owned.path,informational=role=='ccx')
            text='\n'.join(_read(directory/name).content.decode('utf-8',errors='replace') for name in ('stdout.log','stderr.log'))
            matches=set(re.findall(pattern,text))
            if len(matches)!=1 or not matches<=supported:raise AnalysisError('FEA_TOOL_VERSION_UNSUPPORTED','copied executable version requires a separate compatibility run',details={'tool':role})
            pins[role]['version']=matches.pop()
    if inventory(source)!=before:raise AnalysisError('FEA_SOURCE_INSTALL_CHANGED','protected source installation changed during isolated preparation')
    if sys.platform=='darwin':signatures['after_probe']=_signature(runtime)
    manifest={'schema_version':'FEAR2Runtime/v1','profile':PROFILE,'runtime_root':str(runtime),'module_path':str(runtime/relative),'module_sha256':R1_PATCHED,'output_module_path':str(runtime/output_relative),'output_module_sha256':PATCHED_SHA256,'source_commit':SOURCE_COMMIT,'source_unchanged':True,'source_tree_sha256':_sha(json.dumps(before,sort_keys=True).encode()),'source_tools':original_tools,'tools':pins,'native_identity':probe['native_identity'],'loaded_identity':probe['modules'],'patches':{'r1':json.loads(faceforce_assets()['provenance.json']),'r2':patch_assets()['provenance']},'excluded_generated_caches':excluded,'signatures':signatures,'graphics':graphics,'tree':inventory(runtime),'qualification':'not_executed'}
    manifest_path=target/'runtime-manifest.json';secure_fs.atomic_publish_new(manifest_path,json.dumps(manifest,ensure_ascii=True,indent=2).encode())
    config={'schema_version':'FEAConfiguration/v1','tools':pins,'runtime':{'profile':PROFILE,'manifest_path':str(manifest_path),'manifest_sha256':_read(manifest_path).sha256},'native_scratch_root':str(scratch),'software_opengl':software_opengl}
    path=target/'config.json';secure_fs.atomic_publish_new(path,json.dumps(config,ensure_ascii=True,indent=2).encode())
    return path
