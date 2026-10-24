from copy import deepcopy
import pytest


def trees():
    before={'bin/opengl32sw.dll':{'sha256':'a'*64},'Mod/Fem/Init.py':{'sha256':'b'*64}}
    after={**before,'bin/opengl32.dll':{'sha256':'a'*64},'Mod/Fem/__pycache__/Init.cpython-311.pyc':{'sha256':'c'*64}}
    return before,after


def test_gui_materialization_only_adds_bundled_driver_alias_and_source_bound_caches():
    from fea_gui_environment import assess_materialization
    before,after=trees()
    result=assess_materialization(before,after)
    assert result['status']=='passed'
    assert result['compiled_sources']==['Mod/Fem/Init.py']


@pytest.mark.parametrize('defect',['source_changed','binary_changed','removed','foreign_dll','foreign_cache','wrong_python_cache','wrong_driver','missing_driver'])
def test_gui_materialization_cannot_reseal_unreviewed_runtime_changes(defect):
    from fea_gui_environment import assess_materialization
    before,after=trees()
    if defect=='source_changed':after['Mod/Fem/Init.py']={'sha256':'d'*64}
    if defect=='binary_changed':after['bin/opengl32sw.dll']={'sha256':'d'*64}
    if defect=='removed':del after['Mod/Fem/Init.py']
    if defect=='foreign_dll':after['bin/custom.dll']={'sha256':'d'*64}
    if defect=='foreign_cache':after['Mod/Fem/__pycache__/unknown.cpython-311.pyc']={'sha256':'d'*64}
    if defect=='wrong_python_cache':after['Mod/Fem/__pycache__/Init.cpython-312.pyc']={'sha256':'d'*64}
    if defect=='wrong_driver':after['bin/opengl32.dll']={'sha256':'d'*64}
    if defect=='missing_driver':del after['bin/opengl32.dll']
    assert assess_materialization(before,after)['status']=='failed'


def test_cache_compilation_never_executes_sources_and_records_invalid_upstream_samples(tmp_path):
    import hashlib
    from fea_gui_compile import compile_sources
    sources={'valid.py':b'raise AssertionError("compilation must never execute this")\n','invalid.py':b'def invalid(:\n'}
    for name,raw in sources.items():(tmp_path/name).write_bytes(raw)
    result=compile_sources({'root':str(tmp_path),'sources':{k:hashlib.sha256(v).hexdigest() for k,v in sources.items()},'existing_caches':{},'output':str(tmp_path/'result.json')})
    assert set(result['syntax_errors'])=={'invalid.py'}
    assert {v['source'] for v in result['compiled'].values()}=={'valid.py'}
    assert all((tmp_path/name).read_bytes()==raw for name,raw in sources.items())


def test_previously_pinned_runtime_cache_bytes_are_preserved(tmp_path):
    import hashlib,py_compile
    from pathlib import Path
    from fea_gui_compile import compile_sources
    source=tmp_path/'module.py';source.write_text('value=42\n')
    cache=Path(py_compile.compile(str(source),doraise=True));before=cache.read_bytes();name=cache.relative_to(tmp_path).as_posix()
    result=compile_sources({'root':str(tmp_path),'sources':{'module.py':hashlib.sha256(source.read_bytes()).hexdigest()},'existing_caches':{name:hashlib.sha256(before).hexdigest()},'output':str(tmp_path/'result.json')})
    assert cache.read_bytes()==before
    assert result['compiled']=={} and result['preserved'][name]['sha256']==hashlib.sha256(before).hexdigest()
