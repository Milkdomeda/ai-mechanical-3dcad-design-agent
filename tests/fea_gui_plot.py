"""Scientific framing of actual native viewport pixels, with Qt already in FreeCAD.

Independent interpolation through the documented native color anchors; no
FreeCAD C++ code is copied. Source for the pinned 1.1.3 color behavior:
https://github.com/FreeCAD/FreeCAD/blob/145529fe741292ff0b3977a01195bf0247425794/src/Mod/Fem/Gui/ViewProviderFemMeshPyImp.cpp
"""
import math

NATIVE_COLOR_SOURCE='https://github.com/FreeCAD/FreeCAD/blob/145529fe741292ff0b3977a01195bf0247425794/src/Mod/Fem/Gui/ViewProviderFemMeshPyImp.cpp'


def color_domain(minimum,maximum):
    if not all(type(v) in (int,float) and math.isfinite(v) for v in (minimum,maximum)) or minimum>maximum:raise ValueError('invalid nodal color domain')
    low,high=min(0.,minimum),max(0.,maximum)
    if not math.isfinite(high-low):raise ValueError('unrepresentable nodal color span')
    return low,high


def _anchors(low,high):
    return ([(low,(0.,0.,1.)),(low/2,(0.,1.,1.))] if low<0 else [])+[(0.,(0.,1.,0.))]+([(high/2,(1.,1.,0.)),(high,(1.,0.,0.))] if high>0 else [])


def native_color(value,minimum,maximum):
    low,high=color_domain(minimum,maximum)
    if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('nonfinite nodal color value')
    if value<low:return (0.,0.,1.)
    if value>high:return (1.,0.,0.)
    anchors=_anchors(low,high)
    if len(anchors)==1:return anchors[0][1]
    for (a,ca),(b,cb) in zip(anchors,anchors[1:]):
        if value<=b:
            weight=(value-a)/(b-a)
            return tuple(x+(y-x)*weight for x,y in zip(ca,cb))
    return anchors[-1][1]


def frame_native_image(source,target,*,case_id,field,unit,minimum,maximum):
    """Annotate captured native pixels; show the true zero-inclusive palette."""
    from PySide import QtCore,QtGui
    low,high=color_domain(minimum,maximum)
    image=QtGui.QImage(str(source))
    if image.isNull():raise ValueError('native viewport PNG is unreadable')
    canvas=QtGui.QImage(1600,1080,QtGui.QImage.Format_RGB32)
    canvas.fill(QtGui.QColor('white'));p=QtGui.QPainter(canvas)
    try:
        p.setPen(QtGui.QColor('black'));p.setFont(QtGui.QFont('Sans Serif',18))
        p.drawText(24,40,case_id+' | '+field+' ['+unit+']')
        p.setFont(QtGui.QFont('Sans Serif',12))
        p.drawText(24,76,'FreeCAD 1.1.3 native nodal field | deformation display OFF (effective scale 0)')
        p.drawImage(QtCore.QRect(20,110,1200,800),image)
        x,y,w,h=1280,150,55,650
        if high>low:
            gradient=QtGui.QLinearGradient(0,y+h,0,y)
            for value,color in _anchors(low,high):gradient.setColorAt((value-low)/(high-low),QtGui.QColor.fromRgbF(*color))
            p.setBrush(QtGui.QBrush(gradient))
        else:p.setBrush(QtGui.QColor(0,255,0))
        p.drawRect(x,y,w,h)
        ticks=sorted({low,high,0.})
        for value in ticks:
            position=y+h-h*(value-low)/(high-low) if high>low else y+h/2
            p.drawText(x+w+15,int(position)+5,f'{value:.6g} {unit}')
        p.drawText(1260,110,'Native color scale')
        p.drawText(24,956,f'Data minimum: {minimum:.6g} {unit}    Data maximum: {maximum:.6g} {unit}')
        p.drawText(24,994,'The color scale includes zero, matching native FEM node colors; it is distinct from the data minimum.')
        p.drawText(24,1032,'Actual viewport pixels and scalar extrema are bound to the same case, mesh and result field in the JSON receipt.')
    finally:p.end()
    if not canvas.save(str(target),'PNG'):raise ValueError('annotated native figure was not saved')
    return {'color_domain':[low,high],'legend_policy':'freecad-1.1.3-native-nodal','color_source':NATIVE_COLOR_SOURCE,'deformation_scale':0}
