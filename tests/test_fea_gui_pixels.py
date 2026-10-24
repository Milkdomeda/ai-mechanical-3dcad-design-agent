import pytest


@pytest.mark.parametrize('kind,passed', [('cloud', True), ('black', False), ('white', False), ('tiny', False), ('clipped', False)])
def test_actual_viewport_pixels_must_contain_a_framed_native_color_field(kind, passed):
    from fea_gui_pixels import inspect_rgb_pixels
    def pixel(x, y):
        if kind == 'white': return (255, 255, 255)
        if kind == 'cloud' and 20 <= x <= 80 and 30 <= y <= 60: return (0, 255, 0)
        if kind == 'tiny' and x == 50 and y == 50: return (255, 0, 0)
        if kind == 'clipped' and x < 80 and y < 60: return (255, 0, 0)
        return (0, 0, 0)
    result = inspect_rgb_pixels(100, 100, pixel)
    assert (result['status'] == 'passed') is passed
    assert result['sampled_pixels'] > 0


def test_report_cannot_pass_without_actual_pixel_evidence():
    from fea_gui_checks import assess_display_fields
    from test_fea_gui_checks import displayed_fields
    fields = displayed_fields()
    for field in fields.values(): field.pop('viewport_pixels', None)
    assert assess_display_fields(fields)['status'] == 'failed'
