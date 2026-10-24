from copy import deepcopy

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def quantity(value, unit):
    return {'value': str(value), 'unit': unit}


def geometry():
    return {
        'schema_version': 'AnalysisGeometry/v1', 'source_sha256': 'a' * 64,
        'included_objects': ['Solid'], 'excluded_objects': ['Ball1', 'Ball2', 'Ball3', 'Ball4'],
        'construction_objects': [], 'geometry_sha256': 'b' * 64,
        'volume': quantity(10000, 'mm^3'), 'original_volume': quantity(10000, 'mm^3'),
        'center_of_mass': [quantity(v, 'mm') for v in (50, 5, 5)],
        'bounds': [quantity(v, 'mm') for v in (0, 0, 0, 100, 10, 10)],
        'solid_count': 1, 'valid': True, 'closed': True,
        'partitions': [], 'faces': [
            {'index': i, 'surface_type': 'plane', 'area': quantity(100, 'mm^2'),
             'centroid': [quantity(v, 'mm') for v in (x, 5, 5)],
             'normal': [quantity(v, '1') for v in normal]}
            for i, x, normal in [(1, 0, (-1, 0, 0)), (2, 100, (1, 0, 0))]
        ],
        'artifacts': {'brep': {'relative_path': 'geometry.brep', 'sha256': 'b' * 64},
                      'fcstd': {'relative_path': 'geometry.FCStd', 'sha256': 'c' * 64}},
    }


def selector():
    return {'region_id': 'support', 'object_name': 'Solid', 'surface_type': 'plane',
            'centroid': [quantity(v, 'mm') for v in (0, 5, 5)],
            'centroid_tolerance': quantity('.001', 'mm'),
            'area': quantity(100, 'mm^2'), 'area_tolerance': quantity('.001', 'mm^2'),
            'expected_faces': 1, 'normal': [quantity(v, '1') for v in (-1, 0, 0)]}


def test_reference_balls_excluded():
    from mechanical_design_agent.analysis_geometry import resolve_regions
    manifest = geometry()
    result = resolve_regions(manifest, [selector()])
    assert manifest['included_objects'] == ['Solid']
    assert len(manifest['excluded_objects']) == 4
    assert result['bindings'][0]['face_indices'] == [1]
    bad = selector(); bad['object_name'] = 'Ball1'
    with pytest.raises(AnalysisError) as error:
        resolve_regions(manifest, [bad])
    assert error.value.code == 'REGION_OBJECT_EXCLUDED'


def test_ambiguous_region_never_selects_first_face():
    from mechanical_design_agent.analysis_geometry import resolve_regions
    manifest = geometry()
    duplicate = deepcopy(manifest['faces'][0]); duplicate['index'] = 3
    manifest['faces'].append(duplicate)
    with pytest.raises(AnalysisError) as error:
        resolve_regions(manifest, [selector()])
    assert error.value.code == 'REGION_AMBIGUOUS'


def test_binding_hash_changes_with_region_geometry():
    from mechanical_design_agent.analysis_geometry import resolve_regions
    before = resolve_regions(geometry(), [selector()])
    changed = geometry(); changed['geometry_sha256'] = 'd' * 64
    changed['artifacts']['brep']['sha256'] = 'd' * 64
    after = resolve_regions(changed, [selector()])
    assert before['bindings_sha256'] != after['bindings_sha256']


@pytest.mark.parametrize('mutation', ['negative_tolerance', 'duplicate_region', 'bad_normal', 'empty_area', 'unknown_key'])
def test_region_contract_fails_closed(mutation):
    from mechanical_design_agent.analysis_geometry import resolve_regions
    selected = selector(); selectors = [selected]
    if mutation == 'negative_tolerance': selected['centroid_tolerance']['value'] = '-1'
    if mutation == 'duplicate_region': selectors.append(deepcopy(selected))
    if mutation == 'bad_normal': selected['normal'] = [quantity(0, '1')] * 3
    if mutation == 'empty_area': selected['area']['value'] = '0'
    if mutation == 'unknown_key': selected['python'] = 'print(1)'
    with pytest.raises(AnalysisError):
        resolve_regions(geometry(), selectors)


def test_mapping_requires_exact_geometry_and_complete_nonempty_face_sets():
    from mechanical_design_agent.analysis_geometry import resolve_regions, verify_region_mapping
    bindings = resolve_regions(geometry(), [selector()])
    mapping = {'geometry_sha256': 'b' * 64, 'regions': [
        {'region_id': 'support', 'face_indices': [1], 'node_ids': [1, 2, 3], 'element_faces': [[1, 1]]}]}
    assert verify_region_mapping(bindings, mapping)['status'] == 'passed'
    for key, value in [('geometry_sha256', 'e' * 64), ('regions', [])]:
        bad = deepcopy(mapping); bad[key] = value
        with pytest.raises(AnalysisError): verify_region_mapping(bindings, bad)
    for key, value in [('node_ids', []), ('face_indices', [2]), ('element_faces', []), ('node_ids', [True])]:
        bad = deepcopy(mapping); bad['regions'][0][key] = value
        with pytest.raises(AnalysisError): verify_region_mapping(bindings, bad)


@pytest.mark.parametrize('defect', ['collection_hash', 'nested_source', 'nested_geometry', 'duplicate_binding'])
def test_region_collection_cannot_claim_a_stale_or_mixed_binding(defect):
    from mechanical_design_agent.analysis_geometry import resolve_regions, verify_region_mapping
    bindings = resolve_regions(geometry(), [selector()])
    if defect == 'collection_hash': bindings['bindings_sha256'] = 'f' * 64
    if defect == 'nested_source': bindings['bindings'][0]['source_sha256'] = 'f' * 64
    if defect == 'nested_geometry': bindings['bindings'][0]['geometry_sha256'] = 'f' * 64
    if defect == 'duplicate_binding': bindings['bindings'].append(deepcopy(bindings['bindings'][0]))
    mapping = {'geometry_sha256': 'b' * 64, 'regions': [{'region_id': 'support', 'face_indices': [1], 'node_ids': [1, 2, 3], 'element_faces': [[1, 1]]}]}
    with pytest.raises(AnalysisError): verify_region_mapping(bindings, mapping)


def test_partition_contract_requires_explicit_numerical_tolerance():
    from mechanical_design_agent.analysis_geometry import validate_geometry_request
    request = {'schema_version': 'AnalysisGeometryRequest/v1', 'source_sha256': 'a' * 64,
               'included_object': 'Solid', 'excluded_objects': [], 'partitions': [
                   {'axis': 'y', 'coordinate': quantity(5, 'mm')}],
               'volume_tolerance': quantity('.000001', 'mm^3')}
    normalized = validate_geometry_request(request)
    assert normalized['volume_tolerance'] == quantity('0.000001', 'mm^3')
    assert normalized['partitions'] == request['partitions']
    for mutate in [lambda x: x.pop('volume_tolerance'), lambda x: x['partitions'][0].update(axis='custom'),
                   lambda x: x['excluded_objects'].append('Solid'),
                   lambda x: x['volume_tolerance'].update(value='-1')]:
        bad = deepcopy(request); mutate(bad)
        with pytest.raises(AnalysisError): validate_geometry_request(bad)
