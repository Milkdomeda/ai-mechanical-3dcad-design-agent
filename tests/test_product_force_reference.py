import pytest

from fea_product_force_reference import surface_weights
from fea_r1_reference import graph_points, graph_reference


@pytest.mark.parametrize('curvature', [0., .1, .5, 1.])
def test_separate_2d_reference_matches_analytic_1d_integrals(curvature):
    weights, area = surface_weights(graph_points(curvature))
    expected = graph_reference(curvature)
    assert weights == pytest.approx(expected['weights'], abs=2e-14)
    assert area == pytest.approx(expected['area'], abs=2e-14)
