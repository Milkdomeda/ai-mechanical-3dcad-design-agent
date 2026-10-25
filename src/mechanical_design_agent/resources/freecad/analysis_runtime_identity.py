"""Fixed Python3.11 identity probe; no customer document is opened."""
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
import types


def code_record(code):
    def constant(value):
        if isinstance(value, types.CodeType): return {'code': code_record(value)}
        if isinstance(value, tuple): return {'tuple': [constant(v) for v in value]}
        if isinstance(value, frozenset): return {'frozenset': sorted((constant(v) for v in value), key=repr)}
        if isinstance(value, bytes): return {'bytes': value.hex()}
        return {'type': type(value).__name__, 'value': repr(value)}
    fields = ('co_name','co_qualname','co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize','co_flags','co_names','co_varnames','co_freevars','co_cellvars')
    return {name: getattr(code,name) for name in fields} | {'bytecode': code.co_code.hex(), 'constants': [constant(value) for value in code.co_consts], 'exceptiontable': code.co_exceptiontable.hex()}


def loaded(module, pin, names):
    path = Path(module.__file__)
    source = path.read_bytes()
    if not os.path.samefile(path, pin['path']) or hashlib.sha256(source).hexdigest() != pin['sha256']:
        raise ValueError('R2_LOADED_MODULE_PATH_OR_SOURCE')
    expected = {code.co_name: code for code in compile(source, str(path), 'exec', dont_inherit=True, optimize=sys.flags.optimize).co_consts if isinstance(code, types.CodeType)}
    result = {}
    for name in names:
        function = getattr(module,name)
        actual = code_record(getattr(function,'__wrapped__',function).__code__)
        if actual != code_record(expected[name]): raise ValueError('R2_STALE_BYTECODE: '+name)
        result[name] = hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest()
    return {'path': str(path), 'sha256': pin['sha256'], 'loaded_code': result}


def main(request_path, output_path):
    import FreeCAD as App
    from femmesh import meshtools
    from femsolver.calculix import write_step_output
    import Part
    from PySide import QtCore
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    result = {'status': 'failed'}
    try:
        if App.Version()[:3] != ['1','1','3'] or not os.path.samefile(App.getTempPath(),request['temp']) or not os.path.samefile(App.getUserAppDataDir(),request['home']):
            raise ValueError('R2_NATIVE_VERSION_OR_ISOLATION')
        result = {'status': 'passed', 'request_sha256': hashlib.sha256(Path(request_path).read_bytes()).hexdigest(), 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'native_identity': {'app_version': list(App.Version()), 'python': sys.version, 'qt': QtCore.qVersion(), 'occt': Part.OCC_VERSION}, 'modules': {'faceforce': loaded(meshtools,request['faceforce'],['get_ref_facenodes_areas','get_force_obj_face_nodeload_table','t6_weights']), 'output': loaded(write_step_output,request['output'],['write_step_output'])}}
    except Exception: result['error'] = traceback.format_exc()
    Path(output_path).write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf-8')


if __name__ == '__main__': main(sys.argv[1],sys.argv[2])
