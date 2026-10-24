import math
import pytest


@pytest.mark.parametrize('value,expected',[(-4,(0,0,1)),(-2,(0,1,1)),(0,(0,1,0)),(1,(1,1,0)),(2,(1,0,0))])
def test_legend_matches_native_signed_color_anchors(value,expected):
    from fea_gui_plot import native_color
    assert native_color(value,-4,2)==pytest.approx(expected)


def test_positive_only_color_scale_includes_zero_rather_than_inventing_blue_at_data_min():
    from fea_gui_plot import native_color,color_domain
    assert color_domain(.5,1)==(0,1)
    assert native_color(.5,.5,1)==(1,1,0)
    assert native_color(0,.5,1)==(0,1,0)


def test_constant_zero_field_has_defined_green_color():
    from fea_gui_plot import native_color
    assert native_color(0,0,0)==(0,1,0)


@pytest.mark.parametrize('bad',[math.nan,math.inf,-math.inf])
def test_color_scale_rejects_nonfinite_field_bounds(bad):
    from fea_gui_plot import color_domain
    with pytest.raises(ValueError):color_domain(bad,1)
