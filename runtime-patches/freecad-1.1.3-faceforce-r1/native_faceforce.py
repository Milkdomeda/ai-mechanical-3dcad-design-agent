# SPDX-License-Identifier: LGPL-2.0-or-later
# R1 modifications, 2026. Installed into FreeCAD's LGPL meshtools.py only.
# Origin/baseline and installation boundaries are recorded in provenance.json.
"""Native face-force replacement payload; no runtime agent-side load adapter."""
import math as _r1_math
from functools import lru_cache as _r1_cache


def _r1_shape(r, s):
    a=1-r-s
    return ((a*(2*a-1),r*(2*r-1),s*(2*s-1),4*a*r,4*r*s,4*s*a),
            (1-4*a,4*r-1,0.,4*(a-r),4*s,-4*s),
            (1-4*a,0.,4*s-1,-4*r,4*r,4*(a-s)))


def _r1_cross(a,b):
    return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])


def _r1_surface(points,r,s):
    shape,dr,ds=_r1_shape(r,s)
    ar=tuple(_r1_math.fsum(dr[i]*points[i][d] for i in range(6)) for d in range(3))
    ass=tuple(_r1_math.fsum(ds[i]*points[i][d] for i in range(6)) for d in range(3))
    return shape,_r1_cross(ar,ass)


@_r1_cache(maxsize=3)
def _r1_rule(order):
    """Gauss-Legendre rule on [0,1]; bounded Newton iteration, no new library."""
    def polynomial(z):
        previous,current=1.,z
        for j in range(2,order+1):previous,current=current,((2*j-1)*z*current-(j-1)*previous)/j
        return current,order*(z*current-previous)/(z*z-1)
    rule=[]
    for i in range(order//2):
        z=_r1_math.cos(_r1_math.pi*(i+.75)/(order+.5))
        for _ in range(30):
            value,derivative=polynomial(z);new=z-value/derivative
            if abs(new-z)<=2e-16:z=new;break
            z=new
        else:raise ValueError('R1_GAUSS_RULE_NOT_CONVERGED')
        _,derivative=polynomial(z)
        weight=1/((1-z*z)*derivative*derivative)
        rule.extend((((1-z)/2,weight),((1+z)/2,weight)))
    return tuple(sorted(rule))


def t6_weights(points):
    """Integrate all six signed shape functions against the curved metric.

    A positive quadratic Bernstein bound on the projected Jacobian is a
    sufficient whole-reference-triangle orientation certificate. Maps that
    cannot be certified against their corner normal are rejected, including
    some valid but extremely curved maps; this is deliberately conservative.
    """
    try:points=tuple(tuple(float(v) for v in p) for p in points)
    except (TypeError,ValueError,OverflowError) as exc:raise ValueError('R1_T6_COORDINATES') from exc
    if len(points)!=6 or any(len(p)!=3 or not all(_r1_math.isfinite(v) for v in p) for p in points):raise ValueError('R1_T6_COORDINATES')
    scale=max(_r1_math.dist(points[0],p) for p in points[1:])
    if not _r1_math.isfinite(scale) or not 0<scale*scale<_r1_math.inf:raise ValueError('R1_T6_SCALE')
    normalized=tuple(tuple((p[d]-points[0][d])/scale for d in range(3)) for p in points)
    normal=_r1_cross(normalized[1],normalized[2])
    normal2=_r1_math.fsum(v*v for v in normal)
    if normal2<=1e-28:raise ValueError('R1_T6_DEGENERATE_CORNERS')
    samples=[]
    for r,s in ((0,0),(1,0),(0,1),(.5,0),(.5,.5),(0,.5)):
        _,cross=_r1_surface(normalized,r,s)
        samples.append(_r1_math.fsum(a*b for a,b in zip(cross,normal)))
    q0,q1,q2,q3,q4,q5=samples
    bernstein=(q0,q1,q2,2*q3-(q0+q1)/2,2*q4-(q1+q2)/2,2*q5-(q2+q0)/2)
    if min(bernstein)<=1e-14*normal2:raise ValueError('R1_T6_UNCERTIFIED_ORIENTATION_OR_ORDER')
    previous=None
    for order in (8,12,16):
        terms=[[] for _ in range(7)]
        for u,wu in _r1_rule(order):
            for v,wv in _r1_rule(order):
                shape,cross=_r1_surface(normalized,u,(1-u)*v)
                measure=wu*wv*(1-u)*_r1_math.hypot(*cross)
                for i in range(6):terms[i].append(shape[i]*measure)
                terms[6].append(measure)
        current=tuple(_r1_math.fsum(values) for values in terms)
        area=current[6]
        if not area>0 or not all(_r1_math.isfinite(v) for v in current):raise ValueError('R1_T6_NONFINITE_INTEGRAL')
        if abs(_r1_math.fsum(current[:6])-area)>area*1e-13:raise ValueError('R1_T6_PARTITION_FAILURE')
        if previous is not None and max(abs(a-b) for a,b in zip(current,previous))<=area*1e-12:
            result=tuple(v*scale*scale for v in current[:6])
            if not all(_r1_math.isfinite(v) for v in result):raise ValueError('R1_T6_NONFINITE_INTEGRAL')
            return result
        previous=current
    raise ValueError('R1_T6_QUADRATURE_NOT_CONVERGED')


def get_force_obj_face_nodeload_table(femmesh, femelement_table, femnodes_mesh, frc_obj):
    """One declared total force over every unique referenced mesh face.

    Scalar Quantity tables retain FreeCAD's existing downstream direction and
    Reversed handling. CAD area is not mixed with discrete integration area.
    """
    force=FreeCAD.Units.Quantity(frc_obj.Force.getValueAs('N'))
    if not _r1_math.isfinite(float(force.Value)):raise ValueError('R1_NONFINITE_FORCE')
    references=[(obj,name) for obj,names in frc_obj.References for name in names]
    if not references:raise ValueError('R1_EMPTY_FORCE_REGION')
    seen=set();groups=[];total_terms=[]
    for obj,name in sorted(references,key=lambda v:(v[0].Name,v[1])):
        face=sub_shape_at_global_placement(obj,name)
        if face.ShapeType!='Face' or not _r1_math.isfinite(face.Area) or face.Area<=0:raise ValueError('R1_INVALID_REFERENCE_FACE')
        table=get_ref_facenodes_table(femmesh,femelement_table,face)
        if not table:raise ValueError('R1_EMPTY_MESH_REGION')
        by_node={}
        for element in sorted(table):
            nodes=tuple(table[element])
            if len(nodes) not in (3,4,6,8) or len(set(nodes))!=len(nodes) or any(type(n) is not int or n<=0 or n not in femnodes_mesh for n in nodes):raise ValueError('R1_INVALID_FACE_INVENTORY')
            key=tuple(sorted(nodes))
            if key in seen:raise ValueError('R1_DUPLICATE_PHYSICAL_FACE')
            seen.add(key)
            entries=get_ref_facenodes_areas(femnodes_mesh,{element:nodes})
            if len(entries)!=len(nodes) or {n for n,_ in entries}!=set(nodes) or not all(_r1_math.isfinite(w) for _,w in entries):raise ValueError('R1_INVALID_FACE_WEIGHTS')
            area=_r1_math.fsum(w for _,w in entries)
            if area<=0:raise ValueError('R1_NONPOSITIVE_FACE_AREA')
            total_terms.append(area)
            for node,weight in entries:by_node.setdefault(node,[]).append(weight)
        groups.append(('node loads on shape: '+obj.Name+':'+name,{n:_r1_math.fsum(w) for n,w in sorted(by_node.items())}))
    total=_r1_math.fsum(total_terms)
    if not _r1_math.isfinite(total) or total<=0:raise ValueError('R1_INVALID_TOTAL_AREA')
    force_per_area=force/total
    return [(label,{node:weight*force_per_area for node,weight in areas.items()}) for label,areas in groups]
