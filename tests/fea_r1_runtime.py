"""P0 native runtime identity; importable by embedded Python without product deps."""
import hashlib
import json
import os
from pathlib import Path
import sys
import types
from fea_r1_patch import ASSETS,BASE_SHA256,PROFILE,asset_identities,digest


def approved_module_digest():
    return json.loads((ASSETS/'provenance.json').read_text(encoding='utf-8'))['patched_sha256']


def tree_inventory(root):
    root=Path(root).resolve(strict=True);result={}
    for path in sorted(root.rglob('*')):
        name=path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):raise ValueError('R1_RUNTIME_EXTERNAL_SYMLINK')
            result[name]={'symlink':os.readlink(path)}
        elif path.is_file():
            with path.open('rb') as stream:result[name]={'sha256':hashlib.file_digest(stream,'sha256').hexdigest()}
    return result


def module_for_executable(executable):
    path=Path(executable).resolve(strict=True)
    candidates={p for parent in list(path.parents)[:4] for p in (parent/'Mod/Fem/femmesh/meshtools.py',parent/'Resources/Mod/Fem/femmesh/meshtools.py',parent/'Contents/Resources/Mod/Fem/femmesh/meshtools.py') if p.is_file()}
    if len(candidates)!=1:raise ValueError('R1_NATIVE_MODULE_MISSING_OR_AMBIGUOUS')
    return candidates.pop().resolve(strict=True)


def verify_runtime(config, *, full_tree=True):
    module=module_for_executable(config['tools']['freecadcmd']['path']) if config['schema_version']=='FEAP0Configuration/v1' else None
    if module is not None:
        if digest(module.read_bytes())!=BASE_SHA256:raise ValueError('R1_V1_REQUIRES_OFFICIAL_MODULE')
        return {'profile':'official-freecad-1.1.3','module_path':str(module),'module_sha256':BASE_SHA256}
    pin=config['runtime'];path=Path(pin['manifest_path'])
    if path.is_symlink() or not path.is_file() or digest(path.read_bytes())!=pin['manifest_sha256']:raise ValueError('R1_MANIFEST_DIGEST')
    manifest=json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version')!='FEAR1Runtime/v1' or manifest.get('profile')!=PROFILE or pin['profile']!=PROFILE:raise ValueError('R1_PROFILE')
    if manifest.get('assets')!=asset_identities():raise ValueError('R1_PATCH_ASSET_IDENTITY')
    if manifest.get('tools')!=config['tools']:raise ValueError('R1_TOOL_CONFIG_IDENTITY')
    root=Path(manifest['runtime_root']);module=Path(manifest['module_path'])
    if root.is_symlink() or module.is_symlink() or not module.resolve(strict=True).is_relative_to(root.resolve(strict=True)):raise ValueError('R1_MODULE_PATH')
    expected=approved_module_digest()
    if manifest.get('module_sha256')!=expected or digest(module.read_bytes())!=expected:raise ValueError('R1_NATIVE_MODULE_DIGEST')
    for tool in config['tools'].values():
        p=Path(tool['path'])
        if p.is_symlink() or not p.resolve(strict=True).is_relative_to(root.resolve(strict=True)) or digest(p.read_bytes())!=tool['sha256']:raise ValueError('R1_TOOL_DIGEST')
    if full_tree:
        if tree_inventory(root)!=manifest['tree']:raise ValueError('R1_RUNTIME_TREE_CHANGED')
        if sys.platform=='darwin' and root.suffix=='.app':
            from fea_r1_prepare import _signature
            _signature(root)
    return {'profile':PROFILE,'manifest_sha256':pin['manifest_sha256'],'module_path':str(module.resolve(strict=True)),'module_sha256':expected,'native_identity':manifest['native_identity']}


def loaded_module_receipt(expected, module):
    path=Path(module.__file__).resolve(strict=True)
    if path!=Path(expected['module_path']).resolve(strict=True) or digest(path.read_bytes())!=expected['module_sha256']:raise ValueError('R1_LOADED_MODULE_IDENTITY')
    compiled=compile(path.read_bytes(),module.__file__,'exec',dont_inherit=True,optimize=sys.flags.optimize)
    names=['get_ref_facenodes_areas','get_force_obj_face_nodeload_table']
    codes={code.co_name:code for code in compiled.co_consts if isinstance(code,types.CodeType)}
    if expected['profile']==PROFILE:names+=['t6_weights']+[name for name in codes if name.startswith('_r1_')]
    fingerprints={}
    for name in names:
        function=getattr(module,name)
        actual=getattr(function,'__wrapped__',function).__code__
        actual_record=_code_record(actual)
        if actual_record!=_code_record(codes[name]):raise ValueError('R1_LOADED_CODE_MISMATCH: '+name)
        fingerprints[name]=digest(json.dumps(actual_record,sort_keys=True,separators=(',',':')).encode('ascii'))
    return {**expected,'loaded_module_path':str(path),'loaded_code_sha256':fingerprints}


def _code_record(code):
    # marshal preserves interning/object-sharing details that are not code
    # semantics and differ between .pyc loading and a fresh source compile.
    def constant(value):
        if isinstance(value,types.CodeType):return {'code':_code_record(value)}
        if isinstance(value,tuple):return {'tuple':[constant(v) for v in value]}
        if isinstance(value,frozenset):return {'frozenset':sorted((constant(v) for v in value),key=repr)}
        if isinstance(value,bytes):return {'bytes':value.hex()}
        return {'type':type(value).__name__,'value':repr(value)}
    fields=('co_name','co_qualname','co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize','co_flags','co_names','co_varnames','co_freevars','co_cellvars')
    return {name:getattr(code,name) for name in fields}|{'bytecode':code.co_code.hex(),'constants':[constant(v) for v in code.co_consts],'exceptiontable':code.co_exceptiontable.hex()}


def native_identity():
    import FreeCAD as App
    import Part
    from PySide import QtCore
    return {'app_version':list(App.Version()),'python':sys.version,'qt':QtCore.qVersion(),'occt':Part.OCC_VERSION}


def startup_receipt(directory):
    import FreeCAD as App
    actual=Path(App.getUserAppDataDir())
    if not actual.is_dir() or not os.path.samefile(actual,directory):raise ValueError('R1_USER_DATA_NOT_ISOLATED')
    return {'user_data':str(actual.resolve(strict=True)),'user_home':App.ConfigGet('UserHomePath')}


def prepare_runtime(*args,**kwargs):
    from fea_r1_prepare import prepare_runtime as prepare
    return prepare(*args,**kwargs)
