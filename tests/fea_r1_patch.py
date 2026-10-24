"""Exact native source patch recipe; no installed module is mutated here."""
import ast
import difflib
import hashlib
from pathlib import Path

PROFILE='freecad-1.1.3-fem-faceforce-r1'
BASE_SHA256='cca1ab3f4aaded4e058295316b5276420538d3170d745c2fc96420c258cf58cc'
BASE_VARIANTS={BASE_SHA256:'LF','0bb170d0f468a106e8ead419fb1b42bddf26e29394e9ef3bf8064f299c6af6d1':'CRLF'}
SOURCE_COMMIT='145529fe741292ff0b3977a01195bf0247425794'
ASSETS=Path(__file__).resolve().parents[1]/'runtime-patches/freecad-1.1.3-faceforce-r1'


def digest(data):return hashlib.sha256(data).hexdigest()


def canonical_baseline(original: bytes) -> bytes:
    if digest(original) not in BASE_VARIANTS:raise ValueError('R1_BASE_MODULE_DIGEST')
    canonical=original.replace(b'\r\n',b'\n')
    if digest(canonical)!=BASE_SHA256:raise ValueError('R1_CANONICAL_MODULE_DIGEST')
    return canonical


def patched_source(original: bytes) -> bytes:
    original=canonical_baseline(original)
    text=original.decode('utf-8');lines=text.splitlines(keepends=True);tree=ast.parse(text)
    force=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_force_obj_face_nodeload_table')
    areas=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_ref_facenodes_areas')
    six=next(n for n in ast.walk(areas) if isinstance(n,ast.If) and ast.unparse(n.test)=='femmesh_facetype == 6')
    body=('            points = [femnodes_mesh[n] for n in face_table[mf]]\n'
          '            weights = t6_weights([(p.x, p.y, p.z) for p in points])\n'
          '            node_area_table.extend(zip(face_table[mf], weights))\n')
    replacements=[(six.body[0].lineno-1,six.body[-1].end_lineno,body),
                  (force.lineno-1,force.end_lineno,(ASSETS/'native_faceforce.py').read_text(encoding='utf-8').rstrip()+'\n')]
    for start,end,replacement in sorted(replacements,reverse=True):lines[start:end]=[replacement]
    result=''.join(lines).encode('utf-8')
    compile(result,'meshtools.py','exec')
    return result


def patch_bytes(original: bytes) -> bytes:
    original=canonical_baseline(original)
    return ''.join(difflib.unified_diff(original.decode().splitlines(keepends=True),patched_source(original).decode().splitlines(keepends=True),
        fromfile='a/src/Mod/Fem/femmesh/meshtools.py',tofile='b/src/Mod/Fem/femmesh/meshtools.py')).encode('utf-8')


def asset_identities() -> dict:
    identities={str(path.relative_to(ASSETS)):digest(path.read_bytes()) for path in (ASSETS/'native_faceforce.py',ASSETS/'meshtools.patch',ASSETS/'provenance.json',ASSETS/'COPYING.LESSER',ASSETS/'native_faceforce_unittest.py')}
    for name in ('fea_r1_patch.py','fea_r1_runtime.py','fea_r1_prepare.py','fea_r1_identity.py','fea_native_probe.py','fea_helpers.py','fea_r1_reference.py','fea_r1_evidence.py','fea_r1_worker.py','fea_r1_runner.py'):
        path=Path(__file__).with_name(name)
        identities['integration/'+name]=digest(path.read_bytes())
    return identities
