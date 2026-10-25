"""Pixel evidence for native cloud visibility; independent of FEM values."""


def inspect_rgb_pixels(width, height, pixel):
    if type(width) is not int or type(height) is not int or width < 10 or height < 10:
        raise ValueError('bounded viewport dimensions required')
    step = max(1, min(width, height) // 400)
    sampled = colored = 0
    xmin, ymin, xmax, ymax = width, height, -1, -1
    for y in range(0, height, step):
        for x in range(0, width, step):
            sampled += 1
            rgb = pixel(x, y)
            if max(rgb) > 40 and max(rgb) - min(rgb) > 30:
                colored += 1
                xmin, ymin, xmax, ymax = min(xmin, x), min(ymin, y), max(xmax, x), max(ymax, y)
    fraction = colored / sampled
    bounds = [xmin, ymin, xmax, ymax] if colored else None
    framed = colored > 0 and xmin > step and ymin > step and xmax < width - 2 * step and ymax < height - 2 * step
    return {'status': 'passed' if fraction >= .01 and framed else 'failed', 'sampled_pixels': sampled, 'colored_pixels': colored, 'colored_fraction': fraction, 'bounds': bounds, 'width': width, 'height': height, 'sampling_step': step, 'policy': 'native palette foreground >=1 percent, fully inside viewport; render quality only'}


def inspect_native_png(path):
    from PySide import QtGui
    image = QtGui.QImage(str(path))
    if image.isNull():
        raise ValueError('native viewport PNG is unreadable')
    return inspect_rgb_pixels(image.width(), image.height(), lambda x, y: QtGui.QColor(image.pixel(x, y)).getRgb()[:3])
