from __future__ import annotations

import pytest

from fea_helpers import ray_coverage, tet10_quality


RIGHT_TET10 = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (.5, 0, 0), (.5, .5, 0), (0, .5, 0), (0, 0, .5), (.5, 0, .5), (0, .5, .5)]


def test_straight_quadratic_tet_has_unit_jacobian():
    value = tet10_quality(RIGHT_TET10)
    assert value["min_det_mm3"] == pytest.approx(1)
    assert value["min_scaled_jacobian"] == pytest.approx(1)


def test_inverted_element_is_detected():
    value = tet10_quality([(y, x, z) for x, y, z in RIGHT_TET10])
    assert value["min_det_mm3"] == pytest.approx(-1)


def test_folded_midside_node_is_not_hidden_by_valid_corners():
    folded = list(RIGHT_TET10)
    folded[4] = (.5, 2, 0)
    assert tet10_quality(folded)["min_det_mm3"] < 0


def test_shared_face_ray_does_not_double_count_thickness():
    result = ray_coverage([(0, 6), (0, 6), (4, 10), (6, 10)])
    assert result["covered_mm"] == 10
    assert result["elements"] == 2


def test_single_element_spanning_thickness_is_not_resolved_by_overlaps():
    result = ray_coverage([(0, 10), (0, 5), (5, 10)])
    assert result["elements"] == 1
