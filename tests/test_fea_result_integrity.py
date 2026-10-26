from __future__ import annotations

import math
import pytest
from fea_helpers import validate_result_fields, nodal_load_evidence


def fields():
    return ([1, 2], [1, 2], [(0., 0., 0.), (.1, 0., 0.)], [1., 1.], [1., 1.])


def test_complete_native_inventory_is_accepted():
    assert validate_result_fields(*fields()) == {"complete": True, "nodes": 2}


@pytest.mark.parametrize('kind', ['truncated', 'duplicate', 'foreign', 'stress_nan', 'mises_nan', 'displacement_nan'])
def test_partial_or_nonfinite_native_fields_fail(kind):
    mesh, ids, disp, stress, mises = fields()
    if kind == 'truncated': disp.pop()
    if kind == 'duplicate': ids[1] = 1
    if kind == 'foreign': ids[1] = 3
    if kind == 'stress_nan': stress[1] = math.nan
    if kind == 'mises_nan': mises[1] = math.nan
    if kind == 'displacement_nan': disp[1] = (math.nan, 0, 0)
    with pytest.raises(RuntimeError, match='RESULT_'):
        validate_result_fields(mesh, ids, disp, stress, mises)


def test_nodal_load_keeps_moment_and_region_membership(tmp_path):
    inp = tmp_path / 'native.inp'
    inp.write_text('*NODE\n1,0,1,0\n2,10,3,0\n*CLOAD\n1,3,-20\n2,3,-20\n')
    actual = nodal_load_evidence(inp, {1, 2})
    assert actual['force_N'] == [0, 0, -40]
    assert actual['moment_Nmm'] == [-80, 200, 0]
    assert actual['load_region_valid']
    assert not nodal_load_evidence(inp, {1})['load_region_valid']


def test_native_gmsh_writer_uses_relative_paths_in_unicode_workspace(tmp_path):
    from fea_helpers import prepare_native_gmsh_input
    from pathlib import Path
    root = tmp_path / '中文 空格'
    root.mkdir()
    original = Path.cwd()
    class Writer:
        def get_tmp_file_paths(self, directory):
            self.temp_file_geo = str(Path(directory) / 'shape2mesh.geo')
            self.temp_file_mesh = str(Path(directory) / 'Solid_Mesh.unv')
        def write_gmsh_input_files(self):
            Path(self.temp_file_geo).write_text('// ' + self.temp_file_geo, encoding='cp1252')
    geo, mesh = prepare_native_gmsh_input(Writer(), root)
    assert geo == root / 'shape2mesh.geo'
    assert mesh == root / 'Solid_Mesh.unv'
    assert Path.cwd() == original
    assert geo.read_text() == '// shape2mesh.geo'
