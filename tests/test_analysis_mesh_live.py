"""Real fixed native meshing; explicit isolated runtime only."""
import hashlib
import json
from pathlib import Path

import pytest

from test_analysis_geometry_live import native_geometry


def mesh_inputs(manifest, kind):
    from mechanical_design_agent.analysis_geometry import resolve_regions
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels
    from test_analysis_loads import load_fixture
    spec, _ = load_fixture()
    spec['source'].update(geometry_sha256=manifest['geometry_sha256'])
    selectors = []
    # Geometry-derived exact identities, not a first-face policy. All native
    # faces become individually reviewed regions in this synthetic fixture.
    for face in manifest['faces']:
        selectors.append({'region_id': 'surface-' + str(face['index']), 'object_name': 'Solid', 'surface_type': face['surface_type'], 'centroid': face['centroid'], 'centroid_tolerance': {'value': '.000001', 'unit': 'mm'}, 'area': face['area'], 'area_tolerance': {'value': '.000001', 'unit': 'mm^2'}, 'expected_faces': 1, 'normal': None})
    bindings = resolve_regions(manifest, selectors)
    spec['source']['regions_sha256'] = bindings['bindings_sha256']
    spec['mesh'] = {'element_type': 'C3D10', 'initial_size': {'value': '4', 'unit': 'mm'}, 'refinement_ratio': {'value': '.7', 'unit': '1'}, 'levels': 3, 'minimum_scaled_jacobian': {'value': '.000001', 'unit': '1'}, 'minimum_thickness_elements': 2, 'critical_rays': [], 'local_sizes': []}
    # The box retains P0's actual 10 mm thickness rays. Cylinder axial rays
    # isolate curved-element intersection/mapping compatibility; they do not
    # establish radial thin-wall resolution or a generic minimum-wall claim.
    pairs = [((x, y, 0), (x, y, 10)) for x, y in [(37, 4.3), (51, 5.7), (73, 3.7)]] if kind == 'box' else [((0, y, z), (100, y, z)) for y, z in [(4.3, 4.1), (5.7, 5.3), (3.7, 6.1)]]
    for index, (start, end) in enumerate(pairs):
        spec['mesh']['critical_rays'].append({'id': 'span-' + str(index), 'start': [{'value': str(v), 'unit': 'mm'} for v in start], 'end': [{'value': str(v), 'unit': 'mm'} for v in end], 'coverage_atol': {'value': '.000001', 'unit': 'mm'}, 'evidence': 'Exact synthetic full boundary span, selected before meshing; box thickness / cylinder axial compatibility only.'})
    spec['budget'] = {'wall_seconds': 600, 'stage_seconds': 180, 'threads': 1, 'max_nodes': 100000, 'max_elements': 100000, 'max_disk_bytes': 1000000000, 'max_attempts': 1, 'memory_mode': 'observed_only', 'max_memory_mb': 2048}
    return bindings, plan_mesh_levels(spec)


@pytest.mark.live_fea
@pytest.mark.parametrize('kind', ['box', 'cylinder'])
def test_real_second_order_mesh_and_curved_region_mapping(native_geometry, kind):
    from mechanical_design_agent.analysis_geometry import inspect_geometry
    from mechanical_design_agent.analysis_mesh import generate_mesh, validate_mesh
    directory, fixtures, settings = native_geometry
    source = fixtures / (kind + '.FCStd')
    request = {'schema_version': 'AnalysisGeometryRequest/v1', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'included_object': 'Solid', 'excluded_objects': ['Ball1', 'Ball2', 'Ball3', 'Ball4'], 'partitions': [], 'volume_tolerance': {'value': '.000001', 'unit': 'mm^3'}}
    output = directory / ('mesh geometry 中文 ' + kind)
    geometry = inspect_geometry(source, request, settings, output)
    bindings, levels = mesh_inputs(geometry, kind)
    target = directory / ('mesh result 中文 ' + kind)
    receipt = generate_mesh(output / 'geometry.brep', bindings, levels[0], settings, target)
    assert validate_mesh(receipt, levels[0])['status'] == 'passed'
    assert receipt['element_type'] == 'C3D10'
    assert receipt['quality']['sample_points_per_element'] == 15
    assert len(receipt['quality']['thickness_rays']) == 3
    assert all(ray['elements'] >= 2 for ray in receipt['quality']['thickness_rays'])
    (target / 'fixture-level.json').write_text(json.dumps(levels[0], indent=2), encoding='utf-8')
