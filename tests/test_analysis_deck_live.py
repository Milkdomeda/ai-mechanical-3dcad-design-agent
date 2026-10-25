"""Real standard writer, synthetic declarations, no customer approval action."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_analysis_geometry_live import native_geometry
from test_analysis_mesh_live import mesh_inputs


@pytest.fixture(scope='module')
def native_decks(native_geometry, native_product_session):
    from mechanical_design_agent.analysis_geometry import inspect_geometry
    from mechanical_design_agent.analysis_mesh import generate_mesh
    directory, fixtures, settings = native_geometry
    native = native_product_session[3]
    source = fixtures / 'box.FCStd'
    request = {'schema_version': 'AnalysisGeometryRequest/v1', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'included_object': 'Solid', 'excluded_objects': ['Ball1', 'Ball2', 'Ball3', 'Ball4'], 'partitions': [], 'volume_tolerance': {'value': '.000001', 'unit': 'mm^3'}}
    geometry_dir = directory / 'deck geometry 中文'
    manifest = inspect_geometry(source, request, settings, geometry_dir, transaction=native)
    bindings, levels = mesh_inputs(manifest, 'box')
    mesh_dir = directory / 'deck mesh 中文'
    receipt = generate_mesh(geometry_dir / 'geometry.brep', bindings, levels[0], settings, mesh_dir, transaction=native)
    bundle = {'directory': str(mesh_dir), 'receipt': receipt, 'geometry_manifest': manifest, 'region_bindings': bindings}
    yield directory, settings, geometry_dir / 'geometry.brep', bundle, levels[0], native


def declared_spec(bundle, level, kind, settings):
    from test_analysis_loads import load_fixture
    from mechanical_design_agent.analysis_session import _policy
    spec, _ = load_fixture()
    geometry, regions = bundle['geometry_manifest'], bundle['region_bindings']
    spec['source'] = {'sha256': geometry['source_sha256'], 'design_model_sha256': geometry['source_sha256'], 'object_names': ['Solid'], 'excluded_object_names': geometry['excluded_objects'], 'geometry_sha256': geometry['geometry_sha256'], 'regions_sha256': regions['bindings_sha256']}
    supports = [face for face in geometry['faces'] if face['surface_type'] == 'plane' and abs(float(face['centroid'][0]['value'])) < 1e-8]
    loaded = [face for face in geometry['faces'] if face['surface_type'] == 'plane' and abs(float(face['centroid'][0]['value'])-100) < 1e-8]
    assert supports and loaded
    payload = 'curved-payload' if kind == 'curved_force' else 'surface-'+str(loaded[0]['index'])
    spec['cases'][0]['fixed_regions'] = ['surface-'+str(face['index']) for face in supports]
    spec['cases'][0]['loads'][0]['region_id'] = payload
    spec['mesh'] = {'element_type': 'C3D10', 'initial_size': level['size'], 'refinement_ratio': {'value': '.7', 'unit': '1'}, 'levels': 3, 'minimum_scaled_jacobian': level['minimum_scaled_jacobian'], 'minimum_thickness_elements': 2, 'critical_rays': level['critical_rays'], 'local_sizes': []}
    spec['budget'] = level['budget']
    if kind == 'gravity':
        spec['material']['density'] = {'value': '.000000001', 'unit': 'tonne/mm^3'}
        spec['frame_mass'] = {'method': 'density_volume', 'distribution': 'uniform_volume', 'evidence': ['Synthetic density/volume software fixture.']}
        spec['cases'][0]['loads'] = [{'type': 'gravity', 'id': 'self-weight', 'components': [{'value': str(value), 'unit': 'mm/s^2'} for value in (0, 0, -10000)], 'factor': {'value': '1', 'unit': '1'}}]
    elif kind == 'pressure':
        spec['cases'][0]['loads'] = [{'type': 'pressure', 'id': 'pressure', 'region_id': payload, 'value': {'value': '1', 'unit': 'MPa'}, 'reversed': False, 'factor': {'value': '1', 'unit': '1'}}]
    spec.update(status='approved', unresolved=[])
    spec['tool_constraints'] = {'runtime_manifest_sha256': settings['runtime']['manifest_sha256'], 'policy_sha256': _policy(spec), 'adapter_version': 'native-solid-static-v1'}
    return spec


@pytest.mark.live_fea
@pytest.mark.parametrize('kind', ['force', 'gravity', 'pressure'])
def test_standard_objects_and_native_input_match_declared_units(native_decks, kind):
    from mechanical_design_agent.analysis_deck import build_native_deck
    directory, settings, geometry, bundle, level, native = native_decks
    spec = declared_spec(bundle, level, kind, settings)
    result = build_native_deck(spec, geometry, bundle, directory / ('deck output 中文 '+kind), settings, case_id='static', transaction=native)
    inspection = result['inspection']
    assert result['numerical_acceptance'] == 'not_executed'
    assert inspection['element_count'] == bundle['receipt']['element_count']
    from mechanical_design_agent.analysis_runtime_patch import PROFILE
    assert inspection['rf_output'] == ('nodal_and_totals' if settings['configuration']['runtime']['profile'] == PROFILE else 'totals_only')
    assert {'Fixed', 'CalculiX', 'Material', 'Mesh', 'AnalysisSolid', 'Analysis'} <= {obj['name'] for obj in result['objects']}
    if kind == 'force':
        assert [float(value) for value in inspection['force_N']] == pytest.approx([0, 0, -40], abs=1e-9)
        assert [float(value) for value in inspection['moment_Nmm']] == pytest.approx([-200, 4000, 0], abs=1e-6)
    elif kind == 'gravity':
        assert inspection['distributed_loads'][0][0] == 'gravity'
        assert inspection['distributed_loads'][0][2] == ['0', '0', '-10000']
        assert len(inspection['distributed_loads'][0][1]) == bundle['receipt']['element_count']
        densities = [value[1][0] for material in inspection['semantics']['materials'] for key, value in material if key == 'DENSITY']
        assert densities == ['0.000000001']
    else:
        assert inspection['distributed_loads'] and all(row[0] == 'pressure' and row[2][1] == '1' for row in inspection['distributed_loads'])
    if settings['configuration']['runtime']['profile']==PROFILE:
        from fea_product_solve import solve_and_extract,verify_synthetic_balance
        fields=solve_and_extract(result,bundle,settings,native,directory/('solver output 中文 '+kind))
        assert fields['fields']['validity']=='passed'
        assert fields['numerical_acceptance']=='not_executed'  # no convergence/GUI gate yet
        verify_synthetic_balance(fields,result,spec['validation'])


@pytest.mark.live_fea
def test_one_total_force_across_two_curved_native_references(native_geometry, native_transaction):
    from mechanical_design_agent.analysis_geometry import inspect_geometry, resolve_regions
    from mechanical_design_agent.analysis_mesh import generate_mesh
    from mechanical_design_agent.analysis_deck import build_native_deck, _parse_input
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import FACES
    from fea_product_force_reference import compare_force
    directory, fixtures, settings = native_geometry
    source = fixtures / 'cylinder.FCStd'
    request = {'schema_version': 'AnalysisGeometryRequest/v1', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'included_object': 'Solid', 'excluded_objects': ['Ball1', 'Ball2', 'Ball3', 'Ball4'], 'partitions': [{'axis': 'y', 'coordinate': {'value': '5', 'unit': 'mm'}}], 'volume_tolerance': {'value': '.000001', 'unit': 'mm^3'}}
    geometry_dir = directory / 'curved deck geometry 中文'
    manifest = inspect_geometry(source, request, settings, geometry_dir, transaction=native_transaction)
    bindings, levels = mesh_inputs(manifest, 'cylinder')
    curved = [face for face in manifest['faces'] if face['surface_type'] == 'cylinder']
    assert len(curved) == 2
    # This explicit synthetic selector chooses both equal-area half cylinders.
    # Its geometric tolerance is part of the fixture, not a relaxed analysis gate.
    selector = {'region_id': 'curved-payload', 'object_name': 'Solid', 'surface_type': 'cylinder', 'centroid': [{'value': str(value), 'unit': 'mm'} for value in (50,5,5)], 'centroid_tolerance': {'value': '4', 'unit': 'mm'}, 'area': curved[0]['area'], 'area_tolerance': {'value': '.000001', 'unit': 'mm^2'}, 'expected_faces': 2, 'normal': None}
    bindings = resolve_regions(manifest, [item['selector'] for item in bindings['bindings']] + [selector])
    for level in levels: level['region_bindings_sha256'] = bindings['bindings_sha256']
    mesh_dir = directory / 'curved deck mesh 中文'
    receipt = generate_mesh(geometry_dir/'geometry.brep', bindings, levels[0], settings, mesh_dir, transaction=native_transaction)
    bundle = {'directory': str(mesh_dir), 'receipt': receipt, 'geometry_manifest': manifest, 'region_bindings': bindings}
    spec = declared_spec(bundle, levels[0], 'curved_force', settings)
    output = directory/'curved deck output 中文'
    result = build_native_deck(spec, geometry_dir/'geometry.brep', bundle, output, settings, case_id='static', transaction=native_transaction)
    assert len(result['contract']['case']['native_objects']) == 1
    assert len(result['contract']['case']['native_objects'][0]['face_indices']) == 2
    assert [float(value) for value in result['inspection']['force_N']] == pytest.approx([0,0,-40], abs=1e-9)
    data = _parse_input(output/result['input_ref']['relative_path'], allowed_root=output)
    mapping = json.loads((mesh_dir/receipt['artifacts']['mapping']['relative_path']).read_text(encoding='utf-8'))
    region = next(item for item in mapping['regions'] if item['region_id'] == 'curved-payload')
    faces = [tuple(data['_elements'][element][i] for i in FACES[face-1]) for element,face in region['element_faces']]
    evidence = compare_force(data, faces, [0,0,-40])
    (output/'independent-force-reference.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    # Original R1 independent-reference threshold is retained.
    assert evidence['max_nodal_force_error_N'] <= 1e-9
    assert evidence['compared_nodes'] == len(region['node_ids'])
    from mechanical_design_agent.analysis_runtime_patch import PROFILE
    if settings['configuration']['runtime']['profile']==PROFILE:
        from fea_product_solve import solve_and_extract,verify_synthetic_balance
        fields=solve_and_extract(result,bundle,settings,native_transaction,directory/'curved solver output 中文')
        verify_synthetic_balance(fields,result,spec['validation'])
