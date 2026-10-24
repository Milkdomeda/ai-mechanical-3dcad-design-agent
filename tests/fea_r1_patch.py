"""Exact native source patch recipe; no installed module is mutated here."""
import ast
import difflib
import hashlib
from pathlib import Path

PROFILE='freecad-1.1.3-fem-faceforce-r1'
BASE_SHA256='cca1ab3f4aaded4e058295316b5276420538d3170d745c2fc96420c258cf58cc'
SOURCE_COMMIT='145529fe741292ff0b3977a01195bf0247425794'
ASSETS=Path(__file__).resolve().parents[1]/'runtime-patches/freecad-1.1.3-faceforce-r1'


def digest(data):return hashlib.sha256(data).hexdigest()


def patched_source(original: bytes) -> bytes:
    if digest(original)!=BASE_SHA256:raise ValueError('R1_BASE_MODULE_DIGEST')
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
    return ''.join(difflib.unified_diff(original.decode().splitlines(keepends=True),patched_source(original).decode().splitlines(keepends=True),
        fromfile='a/src/Mod/Fem/femmesh/meshtools.py',tofile='b/src/Mod/Fem/femmesh/meshtools.py')).encode('utf-8')


def asset_identities() -> dict:
    return {str(path.relative_to(ASSETS)) : digest(path.read_bytes()) for path in (ASSETS/'native_faceforce.py',ASSETS/'meshtools.patch',ASSETS/'provenance.json')} | {'recipe_sha256':digest(Path(__file__).read_bytes())}
