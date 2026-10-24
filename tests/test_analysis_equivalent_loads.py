from math import fsum

import pytest

from test_analysis_mesh import POINTS


def test_gravity_uses_consistent_quadratic_nodal_loads():
    from mechanical_design_agent.analysis_equivalent_loads import tetra_gravity
    result = tetra_gravity(POINTS, density=6., acceleration=[0., 0., -10.])
    assert result['volume_mm3'] == pytest.approx(1/6)
    assert [fsum(row[i] for row in result['nodal_forces_N']) for i in range(3)] == pytest.approx([0, 0, -10])
    # Quadratic corner weights are negative; uniform lumping is incorrect.
    assert result['nodal_forces_N'][0][2] == pytest.approx(.5)
    assert result['nodal_forces_N'][4][2] == pytest.approx(-2)


def test_planar_t6_pressure_preserves_force_and_first_moment():
    from mechanical_design_agent.analysis_equivalent_loads import triangle_pressure
    face = [POINTS[i] for i in (0, 1, 2, 4, 5, 6)]
    result = triangle_pressure(face, pressure=2., opposite=POINTS[3])
    forces = result['nodal_forces_N']
    # z=0 face, outward -z, compressive pressure acts +z.
    assert [fsum(row[i] for row in forces) for i in range(3)] == pytest.approx([0, 0, 1])
    assert fsum(point[1]*force[2] for point, force in zip(face, forces)) == pytest.approx(1/3)
    assert fsum(-point[0]*force[2] for point, force in zip(face, forces)) == pytest.approx(-1/3)
    assert forces[0][2] == pytest.approx(0, abs=1e-14)


def test_curved_gravity_integrates_quadratic_geometry():
    from mechanical_design_agent.analysis_equivalent_loads import tetra_gravity
    points = list(POINTS)
    points[9] = (0, .5, .65)
    result = tetra_gravity(points, density=1., acceleration=[0, 0, -1])
    # z=w*(1+.6*v), det=1+.6*v; integral(v)=1/24.
    assert result['volume_mm3'] == pytest.approx(1/6 + .6/24)


def test_native_and_independent_quadratures_are_not_confused():
    from mechanical_design_agent.analysis_equivalent_loads import native_tetra_gravity, tetra_gravity
    native = native_tetra_gravity(POINTS, density=1., acceleration=[0, 0, -1])
    independent = tetra_gravity(POINTS, density=1., acceleration=[0, 0, -1])
    assert native['volume_mm3'] == pytest.approx(independent['volume_mm3'])
    assert native['nodal_forces_N'][0] == pytest.approx(independent['nodal_forces_N'][0])
    curved = list(POINTS); curved[9] = (0, .5, .65)
    native = native_tetra_gravity(curved, density=1., acceleration=[0, 0, -1])
    independent = tetra_gravity(curved, density=1., acceleration=[0, 0, -1])
    assert abs(native['nodal_forces_N'][0][2]-independent['nodal_forces_N'][0][2]) > 1e-5
    assert native['method'] != independent['method']


def test_rf_correction_subtracts_both_concentrated_and_distributed_force():
    from mechanical_design_agent.analysis_equivalent_loads import recover_support_reactions
    nodes = {1: [0., 2., 0.], 2: [0., -2., 0.]}
    raw = {1: [0., 0., -3.], 2: [0., 0., -2.]}
    applied = {1: [0., 0., 1.], 2: [0., 0., -1.]}
    result = recover_support_reactions(nodes=nodes, fixed_node_ids=[1, 2], raw_rf=raw, applied_nodal_forces=applied, moment_origin=[0., 0., 0.])
    assert result['force_N'] == [0., 0., -5.]
    assert result['moment_Nmm'] == [-6., 0., 0.]
    assert result['accounting'] == 'native RF - all CLOAD and consistent DLOAD at each constrained node'


def test_missing_native_nodal_rf_cannot_estimate_moment():
    from mechanical_design_agent.analysis_equivalent_loads import recover_support_reactions
    from mechanical_design_agent.analysis_types import AnalysisError
    with pytest.raises(AnalysisError) as error:
        recover_support_reactions(nodes={1: [0., 0., 0.]}, fixed_node_ids=[1], raw_rf={}, applied_nodal_forces={}, moment_origin=[0., 0., 0.])
    assert error.value.code == 'RESULT_RF_INCOMPLETE'
