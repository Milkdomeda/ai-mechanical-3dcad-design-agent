from copy import deepcopy
import json
from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_geometry import geometry, quantity, selector


def load_fixture(*, gravity=False, curved=False):
    from mechanical_design_agent.analysis_geometry import resolve_regions
    spec = json.loads((Path(__file__).parent / 'fixtures/fea/spec_minimal.json').read_text())
    manifest = geometry()
    for index in range(3, 6):
        face = deepcopy(manifest['faces'][1]); face['index'] = index
        manifest['faces'].append(face)
    if curved:
        for face in manifest['faces'][1:]: face['surface_type'] = 'cylinder'
    target = selector(); target.update(region_id='payload', centroid=[quantity(v, 'mm') for v in (100, 5, 5)], expected_faces=4, normal=None)
    if curved: target['surface_type'] = 'cylinder'
    regions = resolve_regions(manifest, [selector(), target])
    spec['source'] = {'sha256': manifest['source_sha256'], 'design_model_sha256': manifest['source_sha256'], 'object_names': ['Solid'], 'excluded_object_names': manifest['excluded_objects'], 'geometry_sha256': manifest['geometry_sha256'], 'regions_sha256': regions['bindings_sha256']}
    spec['frame_mass'] = None
    spec['validation'] = {name: quantity(value, unit) for name, value, unit in (
        ('force_rtol', '.001', '1'), ('force_atol', '.000001', 'N'),
        ('moment_rtol', '.001', '1'), ('moment_atol', '.000001', 'N*mm'),
        ('displacement_rtol', '.02', '1'), ('displacement_atol', '.000001', 'mm'),
        ('stress_rtol', '.02', '1'), ('stress_atol', '.000001', 'MPa'))}
    spec['validation']['moment_origin'] = [quantity(0, 'mm')] * 3
    case = {'case_id': 'static', 'fixed_regions': ['support'], 'free_regions': [], 'operating_condition': 'supported', 'load_meaning': 'direct_applied_loads', 'screening_force': None,
            'loads': [{'type': 'total_force', 'id': 'payload', 'region_id': 'payload', 'components': [quantity(v, 'N') for v in (0, 0, -40)], 'distribution': 'native_uniform_surface', 'factor': quantity(1, '1'), 'basis': {'type': 'specified_force', 'evidence': ['Synthetic40N software fixture.']}}]}
    if gravity:
        spec['frame_mass'] = {'method': 'measured', 'value': quantity('.001', 'tonne'), 'distribution': 'uniform_volume', 'evidence': ['Synthetic fixture: one kilogram, gravity10m/s2.']}
        case['loads'].append({'type': 'gravity', 'id': 'self-weight', 'components': [quantity(v, 'mm/s^2') for v in (0, 0, -10000)], 'factor': quantity(1, '1')})
    spec['cases'] = [case]
    return spec, {'geometry': manifest, 'regions': regions}


def test_multiface_payload_is_one_total():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture()
    ledger = build_load_ledger(spec, bundle)
    case = ledger['cases'][0]
    assert case['total_force_N'] == ['0', '0', '-40']
    assert case['total_moment_Nmm'] == ['-200', '4000', '0']
    assert len(case['native_objects']) == 1
    assert len(case['native_objects'][0]['face_indices']) == 4
    assert case['native_objects'][0]['components_N'] == ['0', '0', '-40']


def test_frame_mass_is_not_inferred_from_infill():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(gravity=True)
    spec['frame_mass'] = None
    with pytest.raises(AnalysisError) as error: build_load_ledger(spec, bundle)
    assert error.value.code == 'FRAME_MASS_UNRESOLVED'


@pytest.mark.parametrize('meaning,payload,total', [('total_including_self_weight', '-65', '-75'), ('payload_only', '-75', '-85')])
def test_explicit_screening_meaning_counts_self_weight_once(meaning, payload, total):
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(gravity=True)
    spec['cases'][0].update(load_meaning=meaning, screening_force=quantity(75, 'N'))
    case = build_load_ledger(spec, bundle)['cases'][0]
    assert case['native_objects'][0]['components_N'] == ['0', '0', payload]
    assert case['total_force_N'] == ['0', '0', total]
    assert case['native_objects'][1]['resultant_N'] == ['0', '0', '-10']


def test_unknown_75n_meaning_blocks_case():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(gravity=True)
    spec['cases'][0].update(load_meaning='unresolved', screening_force=quantity(75, 'N'))
    with pytest.raises(AnalysisError) as error: build_load_ledger(spec, bundle)
    assert error.value.code == 'LOAD_MEANING_UNRESOLVED'


def test_negative_payload_remainder_rejected():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(gravity=True)
    spec['cases'][0].update(load_meaning='total_including_self_weight', screening_force=quantity(5, 'N'))
    with pytest.raises(AnalysisError) as error: build_load_ledger(spec, bundle)
    assert error.value.code == 'LOAD_NEGATIVE_REMAINDER'


def test_separate_dynamic_factors_apply_after_screening_interpretation():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(gravity=True)
    case = spec['cases'][0]
    case.update(load_meaning='total_including_self_weight', screening_force=quantity(75, 'N'))
    case['loads'][0]['factor'] = quantity(2, '1')
    case['loads'][1]['factor'] = quantity(3, '1')
    assert build_load_ledger(spec, bundle)['cases'][0]['total_force_N'] == ['0', '0', '-160']


def test_force_is_not_curved_normal_pressure():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(curved=True)
    # Force direction remains global even on a curved bound region.
    result = build_load_ledger(spec, bundle)
    assert result['cases'][0]['total_force_N'] == ['0', '0', '-40']
    assert result['cases'][0]['native_objects'][0]['type'] == 'total_force'


def test_no_strength_allowable_means_not_evaluated():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture()
    assert build_load_ledger(spec, bundle)['criteria_status'] == 'criteria_not_evaluated'


@pytest.mark.parametrize('reversed,expected', [(False, '-400'), (True, '400')])
def test_planar_pressure_uses_area_and_outward_normal_not_global_force(reversed, expected):
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture()
    spec['cases'][0]['loads'] = [{'type': 'pressure', 'id': 'surface-pressure', 'region_id': 'payload', 'value': quantity(1, 'MPa'), 'reversed': reversed, 'factor': quantity(1, '1')}]
    result = build_load_ledger(spec, bundle)
    assert result['cases'][0]['total_force_N'] == [expected, '0', '0']


def test_curved_pressure_cannot_be_approximated_by_a_single_midpoint_normal():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(curved=True)
    spec['cases'][0]['loads'] = [{'type': 'pressure', 'id': 'surface-pressure', 'region_id': 'payload', 'value': quantity(1, 'MPa'), 'reversed': False, 'factor': quantity(1, '1')}]
    with pytest.raises(AnalysisError) as error: build_load_ledger(spec, bundle)
    assert error.value.code == 'CURVED_PRESSURE_NOT_VALIDATED'


def test_density_volume_mass_excludes_reference_balls_and_preserves_stiffness():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(gravity=True)
    spec['frame_mass'] = {'method': 'density_volume', 'distribution': 'uniform_volume', 'evidence': ['Synthetic homogeneous effective density.']}
    spec['material']['density'] = quantity('.0000001', 'tonne/mm^3')
    before = deepcopy(spec['material'])
    result = build_load_ledger(spec, bundle)
    assert result['frame_mass']['mass_tonne'] == '0.001'
    assert result['cases'][0]['total_force_N'] == ['0', '0', '-50']
    from decimal import Decimal
    assert spec['material'] == before
    for key in ('youngs_modulus', 'poisson_ratio', 'density'):
        assert Decimal(result['material']['material'][key]['value']) == Decimal(before[key]['value'])
    assert result['material']['stiffness_inferred_from_mass_or_infill'] is False


def test_moment_origin_is_explicit_and_changes_moment_only():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture()
    spec['validation']['moment_origin'] = [quantity(v, 'mm') for v in (100, 5, 5)]
    case = build_load_ledger(spec, bundle)['cases'][0]
    assert case['total_force_N'] == ['0', '0', '-40']
    assert case['total_moment_Nmm'] == ['0', '0', '0']


def test_payload_mass_and_acceleration_must_explain_the_nominal_force():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture()
    load = spec['cases'][0]['loads'][0]
    load['basis'] = {'type': 'mass_acceleration', 'mass': quantity('.004', 'tonne'), 'acceleration': [quantity(v, 'mm/s^2') for v in (0, 0, -10000)], 'evidence': ['Synthetic4kg at10m/s2.']}
    assert build_load_ledger(spec, bundle)['cases'][0]['total_force_N'] == ['0', '0', '-40']
    load['basis']['mass']['value'] = '.003'
    with pytest.raises(AnalysisError) as error: build_load_ledger(spec, bundle)
    assert error.value.code == 'LOAD_MASS_FORCE_MISMATCH'


def test_binding_area_or_centroid_cannot_drift_from_the_geometry_inventory():
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture()
    # The BREP digest stays fixed while the supplied face inventory is altered.
    # A bound force center must therefore be rechecked against those faces.
    bundle['geometry']['faces'][1]['centroid'][1]['value'] = '6'
    with pytest.raises(AnalysisError): build_load_ledger(spec, bundle)


@pytest.mark.parametrize('key,value', [('youngs_modulus', '0'), ('youngs_modulus', '-1'), ('poisson_ratio', '-1'), ('poisson_ratio', '.5'), ('density', '-1')])
def test_invalid_isotropic_material_is_rejected(key, value):
    from mechanical_design_agent.analysis_loads import validate_materials
    spec, _ = load_fixture()
    spec['material'][key] = quantity(value, 'MPa' if key == 'youngs_modulus' else ('1' if key == 'poisson_ratio' else 'tonne/mm^3'))
    with pytest.raises(AnalysisError): validate_materials(spec)


@pytest.mark.parametrize('defect', ['missing_material_evidence', 'duplicate_case', 'duplicate_load', 'duplicate_gravity', 'negative_factor', 'fixed_loaded_overlap', 'lifted_fixed_feet', 'custom_weights', 'stale_geometry'])
def test_load_contract_fails_closed(defect):
    from mechanical_design_agent.analysis_loads import build_load_ledger
    spec, bundle = load_fixture(gravity=True)
    case = spec['cases'][0]
    if defect == 'missing_material_evidence': spec['material']['evidence'] = []
    if defect == 'duplicate_case': spec['cases'].append(deepcopy(case))
    if defect == 'duplicate_load': case['loads'].append(deepcopy(case['loads'][0]))
    if defect == 'duplicate_gravity':
        duplicate = deepcopy(case['loads'][1]); duplicate['id'] = 'second-gravity'; case['loads'].append(duplicate)
    if defect == 'negative_factor': case['loads'][0]['factor']['value'] = '-1'
    if defect == 'fixed_loaded_overlap': case['loads'][0]['region_id'] = 'support'
    if defect == 'lifted_fixed_feet': case.update(operating_condition='lifted', free_regions=['support'])
    if defect == 'custom_weights': case['loads'][0]['weights'] = [1, 2, 3, 4]
    if defect == 'stale_geometry': spec['source']['geometry_sha256'] = 'f' * 64
    with pytest.raises(AnalysisError): build_load_ledger(spec, bundle)
