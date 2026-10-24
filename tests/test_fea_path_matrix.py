from pathlib import Path
import zipfile
import pytest
import fea_path_matrix as matrix


def raw(temp, *, native_ok=True, direct_ok=True):
    return {'version': '1.1.3', 'temp_path': str(temp), 'auto_reopen_count': 0,
            'mesh_nodes': 10, 'mesh_volumes': 1,
            'mesh_save': {'ok': native_ok, 'error': '' if native_ok else 'Salome Exception: ERROR: Output file not good.'},
            'plain_save': {'ok': True}, 'direct_unv': {'ok': direct_ok, 'error': '' if direct_ok else 'ERROR: Output file not good.'},
            'direct_archive': {'valid':True,'nodes':10,'tet10':1,'sha256':'synthetic-inventory-receipt'},
            'mesh_archive': {'valid': native_ok, 'nodes': 10, 'tet10': 1}}


def test_matrix_crosses_external_and_temp_independently(tmp_path):
    cells = matrix.matrix_cases(tmp_path)
    assert len(cells) == 4
    assert {(c['external'].name.isascii(), c['native_temp'].name.isascii()) for c in cells} == {(True, True), (True, False), (False, True), (False, False)}
    assert len({c['external'] for c in cells}) == len({c['native_temp'] for c in cells}) == 4


@pytest.mark.parametrize('external_ascii,temp_ascii', [(True, True), (True, False), (False, True), (False, False)])
def test_expected_windows_pattern_is_diagnostic_only(tmp_path, external_ascii, temp_ascii):
    external=tmp_path/('external' if external_ascii else '外部')
    temp=tmp_path/('temp' if temp_ascii else '缓存')
    result=matrix.assess_case(raw(temp, native_ok=temp_ascii, direct_ok=external_ascii), external, temp, system='Windows')
    assert result['diagnostic_status'] == 'passed'
    assert result['p0_status'] == 'blocked'


@pytest.mark.parametrize('defect', ['wrong_temp', 'arbitrary_error', 'empty_mesh', 'missing_control', 'wrong_version', 'reopen'])
def test_unrelated_failure_or_bad_evidence_cannot_confirm_matrix(tmp_path, defect):
    external,temp=tmp_path/'external',tmp_path/'缓存'
    value=raw(temp, native_ok=False)
    if defect == 'wrong_temp': value['temp_path']=str(tmp_path/'other')
    if defect == 'arbitrary_error': value['mesh_save']['error']='access denied'
    if defect == 'empty_mesh': value['mesh_nodes']=0
    if defect == 'missing_control': value['plain_save']['ok']=False
    if defect == 'wrong_version': value['version']='1.0.0'
    if defect == 'reopen': value['auto_reopen_count']=1
    assert matrix.assess_case(value, external, temp, system='Windows')['diagnostic_status']=='failed'


def test_success_with_empty_archived_mesh_is_not_valid(tmp_path):
    value=raw(tmp_path/'temp');value['mesh_archive']['valid']=False
    assert matrix.assess_case(value,tmp_path/'external',tmp_path/'temp',system='Windows')['diagnostic_status']=='failed'


def test_zip_crc_alone_is_insufficient(tmp_path):
    path=tmp_path/'empty.FCStd'
    with zipfile.ZipFile(path,'w') as z:z.writestr('FemMesh.unv','')
    assert matrix.inspect_mesh_archive(path)['valid'] is False


@pytest.mark.parametrize('content', [None,b'',b'    -1\n  2411\n',b'    -1\n  2411\n         1 1 1 1\n0 0 0\n    -1\n'])
def test_direct_export_requires_complete_expected_inventory(tmp_path,content):
    path=tmp_path/'direct.unv'
    if content is not None:path.write_bytes(content)
    value=raw(tmp_path/'temp')
    value['direct_archive']=matrix.inspect_direct_unv(path)
    assert matrix.assess_case(value,tmp_path/'external',tmp_path/'temp',system='Windows')['diagnostic_status']=='failed'


def test_expected_unicode_export_error_still_requires_written_mesh(tmp_path):
    value=raw(tmp_path/'temp',direct_ok=False)
    value['direct_archive']={'valid':False}
    assert matrix.assess_case(value,tmp_path/'外部',tmp_path/'temp',system='Windows')['diagnostic_status']=='failed'
