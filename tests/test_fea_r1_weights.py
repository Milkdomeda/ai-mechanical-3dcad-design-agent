import math
from pathlib import Path
import runpy
import pytest
from fea_r1_reference import graph_points,graph_reference


PAYLOAD=Path(__file__).resolve().parents[1]/'runtime-patches/freecad-1.1.3-faceforce-r1/native_faceforce.py'


def weights(points):
    return runpy.run_path(str(PAYLOAD))['t6_weights'](points)


def test_straight_triangle_has_analytic_consistent_weights():
    assert weights(graph_points(0))==pytest.approx((0,0,0,1/6,1/6,1/6),abs=2e-14)


@pytest.mark.parametrize('curvature',[.1,.5,1.])
def test_curved_weights_match_independent_integrated_polynomials(curvature):
    actual=weights(graph_points(curvature));expected=graph_reference(curvature)
    assert actual==pytest.approx(expected['weights'],rel=2e-12,abs=2e-14)
    assert math.fsum(actual)==pytest.approx(expected['area'],rel=1e-12)
    assert actual[0]<0 and actual[1]>0  # Clipping corners must fail this test.


def test_curved_centroid_and_nonrigid_quadratic_virtual_work():
    points=graph_points();actual=weights(points);reference=graph_reference()
    assert math.fsum(w*p[0] for w,p in zip(actual,points))==pytest.approx(reference['x'],rel=1e-12)
    assert math.fsum(w*p[1] for w,p in zip(actual,points))==pytest.approx(reference['y'],rel=1e-12)
    assert math.fsum(w*p[0]**2 for w,p in zip(actual,points))==pytest.approx(reference['x2'],rel=1e-12)


@pytest.mark.parametrize('scale',[1e-8,1.,1e8])
def test_weights_transform_as_area_under_rotation_translation_and_scaling(scale):
    points=tuple(tuple(scale*(v+offset) for v,offset in zip((p[2],p[0],p[1]),(3.,-7.,2.))) for p in graph_points())
    actual=tuple(w/scale**2 for w in weights(points))
    assert actual==pytest.approx(graph_reference()['weights'],rel=2e-11,abs=2e-14)


@pytest.mark.parametrize('defect',['empty','duplicate','nonfinite','folded','misordered'])
def test_invalid_or_uncertified_surface_fails_before_producing_weights(defect):
    points=list(graph_points())
    if defect=='empty':points=[]
    if defect=='duplicate':points[1]=points[0]
    if defect=='nonfinite':points[4]=(math.nan,.5,0)
    if defect=='folded':points[3]=(2.,2.,0.)
    if defect=='misordered':points[3],points[4]=points[4],points[3]
    with pytest.raises(ValueError,match='R1_'):weights(points)
