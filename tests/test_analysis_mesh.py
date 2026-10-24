from copy import deepcopy
from decimal import Decimal
import math

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_coordinator import setup


def mesh_spec(tmp_path):
    _, _, _, request, _, _, _ = setup(tmp_path)
    spec = request['draft_spec']
    spec['source'] = {'geometry_sha256': 'a' * 64, 'regions_sha256': 'b' * 64}
    spec['mesh']['critical_rays'] = [{'id': 'ray-' + str(index), 'start': [{'value': str(v), 'unit': 'mm'} for v in [x, y, 0]], 'end': [{'value': str(v), 'unit': 'mm'} for v in [x, y, 10]], 'coverage_atol': {'value': '0.000001', 'unit': 'mm'}, 'evidence': 'Explicit synthetic full-thickness ray.'} for index, (x, y) in enumerate([(37, 4.3), (51, 5.7), (73, 3.7)])]
    return spec


def test_three_levels_preserve_physical_patch(tmp_path):
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels
    spec = mesh_spec(tmp_path)
    levels = plan_mesh_levels(spec)
    assert [Decimal(level['size']['value']) for level in levels] == [Decimal('4'), Decimal('2.8'), Decimal('1.96')]
    assert len({level['region_bindings_sha256'] for level in levels}) == 1
    assert all(level['element_type'] == 'C3D10' and level['threads'] == 1 for level in levels)
    assert all(level['critical_rays'] == spec['mesh']['critical_rays'] for level in levels)
    assert spec['mesh']['initial_size']['value'] == '4'


@pytest.mark.parametrize('field,value', [('initial_size', '0'), ('initial_size', '-1'), ('refinement_ratio', '1'), ('refinement_ratio', '0'), ('minimum_scaled_jacobian', '0'), ('minimum_scaled_jacobian', '1.1')])
def test_nonphysical_mesh_parameters_are_rejected(tmp_path, field, value):
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels
    spec = mesh_spec(tmp_path)
    spec['mesh'][field]['value'] = value
    with pytest.raises(AnalysisError): plan_mesh_levels(spec)


def test_missing_critical_ray_is_needs_input(tmp_path):
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels
    spec = mesh_spec(tmp_path)
    spec['mesh'].pop('critical_rays')
    with pytest.raises(AnalysisError) as error: plan_mesh_levels(spec)
    assert error.value.code == 'MESH_CRITICAL_RAYS_REQUIRED'


def test_budget_cannot_claim_unenforced_memory_limit(tmp_path):
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels
    spec = mesh_spec(tmp_path)
    spec['budget']['memory_mode'] = 'hard_limit'
    with pytest.raises(AnalysisError) as error: plan_mesh_levels(spec)
    assert error.value.code == 'ANALYSIS_MEMORY_MODE_UNSUPPORTED'


POINTS = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (.5, 0, 0), (.5, .5, 0), (0, .5, 0), (0, 0, .5), (.5, 0, .5), (0, .5, .5)]


def test_native_numbering_conversion_is_fixed_not_a_jacobian_sign_repair():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import freecad_to_ccx_nodes, tet10_quality
    assert freecad_to_ccx_nodes(list(range(1, 11))) == [2, 1, 3, 4, 5, 7, 6, 9, 8, 10]
    native = [POINTS[i] for i in (1, 0, 2, 3, 4, 6, 5, 8, 7, 9)]
    with pytest.raises(ValueError): tet10_quality(native, .1)
    canonical = [native[i] for i in freecad_to_ccx_nodes(list(range(10)))]
    assert tet10_quality(canonical, .1)['min_det_mm3'] == pytest.approx(1)


def test_actual_tet10_map_is_sampled_and_bounded():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import tet10_quality
    quality = tet10_quality(POINTS, .1)
    assert quality['sample_points'] == 15
    assert quality['min_det_mm3'] == pytest.approx(1)
    assert quality['bernstein_scaled_lower_bound'] > .99
    bent = deepcopy(POINTS)
    bent[4] = (.5, 0, .06)
    curved = tet10_quality(bent, .1)
    assert 0 < curved['min_det_mm3'] < 1
    assert curved['bernstein_scaled_lower_bound'] > .1


def test_first_order_and_folded_midside_geometry_cannot_pass():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import tet10_quality
    for points in (POINTS[:4], [*POINTS[:4], (.5, 0, 3), *POINTS[5:]]):
        with pytest.raises(ValueError): tet10_quality(points, .1)


def test_ray_checks_actual_curved_faces_not_corner_tetrahedron():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import tetra_ray_intervals
    points = deepcopy(POINTS)
    points[9] = (0, .5, .65)
    linear = tetra_ray_intervals(POINTS, [.2, .2, -1], [.2, .2, 2])
    curved = tetra_ray_intervals(points, [.2, .2, -1], [.2, .2, 2])
    assert linear[0] == pytest.approx((1, 1.6))
    # Exact map: z = w*(1 + .6*v); at u=v=.2, w=.6.
    assert curved[0] == pytest.approx((1, 1.672), abs=1e-10)


def test_thin_region_resolution_not_global_node_count():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import ray_coverage
    assert ray_coverage([(0, 5), (5, 10)]) == {'covered_mm': 10, 'elements': 2}
    assert ray_coverage([(0, 10), (0, 5), (5, 10)])['elements'] == 1
    assert ray_coverage([(0, 4), (6, 10)])['covered_mm'] == 8


def test_parallel_curved_face_is_not_falsely_an_intersection():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import tetra_ray_intervals
    # Diagnostic captured from the first real cylindrical Gmsh fixture.
    points = [[60.,5.,10.],[56.,5.,10.],[58.,7.527497864325568,9.314134275359276],[59.95691998346517,5.062112938149966,4.999686516349371],[58.,5.,10.],[57.,6.3094519126725555,9.825488129546915],[59.,6.3094519126725555,9.825488129546915],[59.978459991732585,5.031056469074984,7.4998432581746854],[57.978459991732585,5.031056469074984,7.4998432581746854],[58.97845999173259,6.294805401237767,7.156910395854323]]
    intervals = tetra_ray_intervals(points, [0., 5.7, 5.3], [100., 5.7, 5.3])
    assert intervals == []


def test_parallel_ruling_contact_remains_ambiguous():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import _face_hits
    face = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (.5, 0, 0), (.5, .5, .25), (0, .5, .25)]
    assert _face_hits(face, [0, .5, .2], [1, 0, 0]) == []
    with pytest.raises(ValueError, match='TANGENT'):
        _face_hits(face, [0, .5, .25], [1, 0, 0])


def test_bernstein_bound_covers_independent_dense_samples():
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import _jacobian, det, norm, tet10_quality
    points = deepcopy(POINTS)
    points[9] = (0, .5, .65)
    lower = tet10_quality(points, .1)['bernstein_scaled_lower_bound']
    for i in range(9):
        for j in range(9-i):
            for k in range(9-i-j):
                point = [i/8, j/8, k/8, 1-(i+j+k)/8]
                jacobian = _jacobian(points, point)
                assert det(jacobian)/math.prod(norm(column) for column in jacobian) >= lower


def test_mesh_budget_exhaustion_is_inconclusive(tmp_path):
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels, validate_mesh
    level = plan_mesh_levels(mesh_spec(tmp_path))[0]
    receipt = {'element_type': 'C3D10', 'node_count': level['budget']['max_nodes'] + 1, 'element_count': 10, 'disk_bytes': 100}
    assert validate_mesh(receipt, level) == {'status': 'inconclusive', 'code': 'MESH_BUDGET_EXCEEDED'}


def test_every_critical_ray_and_element_must_be_accounted_for(tmp_path):
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels, validate_mesh
    level = plan_mesh_levels(mesh_spec(tmp_path))[0]
    quality = {'checked_elements': 10, 'sample_points_per_element': 15, 'min_det_mm3': .3, 'min_scaled_jacobian': .5, 'bernstein_scaled_lower_bound': .4, 'thickness_rays': [{'id': ray['id'], 'covered_mm': 10, 'elements': 3} for ray in level['critical_rays']]}
    receipt = {'element_type': 'C3D10', 'node_count': 30, 'element_count': 10, 'disk_bytes': 100, 'quality': quality}
    assert validate_mesh(receipt, level)['status'] == 'passed'
    for field, value in [('checked_elements', 9), ('min_det_mm3', 0), ('sample_points_per_element', 5)]:
        broken = deepcopy(receipt)
        broken['quality'][field] = value
        with pytest.raises(AnalysisError): validate_mesh(broken, level)
    for field, value in [('elements', 1), ('covered_mm', 9.9)]:
        broken = deepcopy(receipt)
        broken['quality']['thickness_rays'][1][field] = value
        with pytest.raises(AnalysisError): validate_mesh(broken, level)


def test_numerical_policy_and_local_sizes_are_frozen_before_meshing(tmp_path):
    from mechanical_design_agent.analysis_mesh import plan_mesh_levels
    spec = mesh_spec(tmp_path)
    spec['mesh']['local_sizes'] = [{'region_id': 'root', 'size': {'value': '2', 'unit': 'mm'}, 'evidence': 'Explicit root refinement.'}]
    first = plan_mesh_levels(spec)
    assert first[2]['local_sizes'][0]['size']['value'] == '0.98'
    assert len({level['policy_sha256'] for level in first}) == 1
    spec['budget']['stage_seconds'] = 0
    with pytest.raises(AnalysisError): plan_mesh_levels(spec)
