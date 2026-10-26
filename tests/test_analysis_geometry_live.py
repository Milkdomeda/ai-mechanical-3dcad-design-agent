"""Native product geometry phase; requires an explicit isolated P0 runtime."""
import hashlib
import json
import os
from pathlib import Path

import pytest



def native_settings(root, config_path, scratch):
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
    from mechanical_design_agent.workspace_bootstrap import initialize_workspace
    root.mkdir()
    initialize_workspace(workspace=root, actor_id='fixture-engineer', organization_id='synthetic-org', design_group_id='synthetic-group', dry_run=False)
    legacy = json.loads(Path(config_path).read_text(encoding='utf-8'))
    manifest = json.loads(Path(legacy['runtime']['manifest_path']).read_text(encoding='utf-8'))
    runtime = Path(manifest['runtime_root'])
    relative = 'Contents/Resources/bin/freecad' if os.name != 'nt' else 'bin/FreeCAD.exe'
    tools = dict(legacy['tools'])
    tools['freecadgui'] = {'path': str(runtime / relative), 'version': '1.1.3', 'sha256': hashlib.sha256((runtime / relative).read_bytes()).hexdigest()}
    config = {'schema_version': 'FEAConfiguration/v1', 'tools': tools, 'runtime': legacy['runtime'], 'native_scratch_root': str(scratch), 'software_opengl': False}
    (root / 'config/fea.json').write_text(json.dumps(config), encoding='utf-8')
    return load_analysis_configuration(BootstrapRuntime.from_process(cwd=root, environ={}, workspace=root))


@pytest.fixture(scope='module')
def native_geometry(native_product_session):
    return native_product_session[:3]


@pytest.mark.live_fea
@pytest.mark.parametrize('kind', ['box', 'cylinder', 'placed'])
def test_partial_face_partition_preserves_volume_and_connectivity(native_geometry, native_transaction, kind):
    from decimal import Decimal
    from mechanical_design_agent.analysis_geometry import inspect_geometry, resolve_regions
    from mechanical_design_agent.fcstd_security import inspect_fcstd_bytes
    directory, fixtures, settings = native_geometry
    source = fixtures / (kind + '.FCStd')
    original = source.read_bytes()
    request = {'schema_version': 'AnalysisGeometryRequest/v1', 'source_sha256': hashlib.sha256(original).hexdigest(), 'included_object': 'Solid', 'excluded_objects': ['Ball1', 'Ball2', 'Ball3', 'Ball4'], 'partitions': [{'axis': 'y', 'coordinate': {'value': '70' if kind == 'placed' else '5', 'unit': 'mm'}}], 'volume_tolerance': {'value': '0.000001', 'unit': 'mm^3'}}
    output = directory / ('result 中文 ' + kind)
    manifest = inspect_geometry(source, request, settings, output, transaction=native_transaction)
    assert manifest['included_objects'] == ['Solid']
    assert len(manifest['excluded_objects']) == 4
    assert manifest['valid'] is True and manifest['closed'] is True and manifest['solid_count'] == 1
    assert abs(Decimal(manifest['volume']['value']) - Decimal(manifest['original_volume']['value'])) <= Decimal('.000001')
    assert source.read_bytes() == original
    inspect_fcstd_bytes((output / 'geometry.FCStd').read_bytes())
    assert len(manifest['faces']) > (3 if kind == 'cylinder' else 6)
    if kind == 'placed':
        assert [float(q['value']) for q in manifest['bounds']] == pytest.approx([100, 20, 30, 110, 120, 40], abs=1e-9)
        assert {'Parent', 'Origin', 'X_Axis', 'XY_Plane'} <= set(manifest['construction_objects'])
    candidates = [face for face in manifest['faces'] if face['surface_type'] == ('cylinder' if kind == 'cylinder' else 'plane')]
    face = candidates[0]
    selected = {'region_id': 'load', 'object_name': 'Solid', 'surface_type': face['surface_type'], 'centroid': face['centroid'], 'centroid_tolerance': {'value': '.000001', 'unit': 'mm'}, 'area': face['area'], 'area_tolerance': {'value': '.000001', 'unit': 'mm^2'}, 'expected_faces': 1, 'normal': None}
    bound = resolve_regions(manifest, [selected])
    assert bound['bindings'][0]['face_indices'] == [face['index']]
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
