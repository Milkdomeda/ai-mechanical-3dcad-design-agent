"""Fixed Python 3.11 cache materializer for the isolated Windows runtime.

Compilation does not import or execute the compiled application modules.
"""
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import py_compile
import sys


def compile_sources(request):
    if set(request)!={'root','sources','existing_caches','output'}:raise ValueError('cache compilation request')
    root=Path(request['root']).resolve(strict=True)
    result={'schema_version':'P0GuiCompile/v1','python':sys.version,'magic':importlib.util.MAGIC_NUMBER.hex(),'compiled':{},'preserved':{},'syntax_errors':{}}
    for name,expected in request['sources'].items():
        parts=PurePosixPath(name)
        if parts.is_absolute() or '..' in parts.parts or '\\' in name or ':' in name or parts.suffix!='.py':raise ValueError('source boundary')
        source=root/name
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):raise ValueError('source boundary')
        raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('source checksum changed')
        existing=Path(importlib.util.cache_from_source(str(source),optimization=''))
        if existing.exists():
            name=existing.relative_to(root).as_posix()
            sha=hashlib.sha256(existing.read_bytes()).hexdigest()
            if existing.is_symlink() or request['existing_caches'].get(name)!=sha:raise ValueError('unexpected or changed existing native cache')
            result['preserved'][name]={'source':source.relative_to(root).as_posix(),'source_sha256':expected,'sha256':sha}
            continue
        try:
            cache=Path(py_compile.compile(str(source),doraise=True,optimize=0,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,quiet=1))
        except py_compile.PyCompileError as exc:
            result['syntax_errors'][name]={'source_sha256':expected,'reason':str(exc)}
            continue
        header=cache.read_bytes()[:16]
        if header[:4]!=importlib.util.MAGIC_NUMBER or int.from_bytes(header[4:8],'little')!=3 or header[8:16]!=importlib.util.source_hash(raw):raise ValueError('compiled cache source binding')
        result['compiled'][cache.relative_to(root).as_posix()]={'source':name,'source_sha256':expected,'sha256':hashlib.sha256(cache.read_bytes()).hexdigest()}
    return result


def main(request_path):
    if sys.version_info[:2]!=(3,11):raise ValueError('reviewed embedded Python 3.11 required')
    request=json.loads(Path(request_path).read_text(encoding='utf-8'))
    result=compile_sources(request)
    Path(request['output']).write_text(json.dumps(result,ensure_ascii=True,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main(sys.argv[1])
