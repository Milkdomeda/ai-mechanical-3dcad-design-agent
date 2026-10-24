"""Independent reference: integrate analytic polynomials over a curved graph.

For x=r, y=s, z=k*r*r/2, J=sqrt(1+k*k*r*r). The s integral
of each shape function is a literal polynomial below. Adaptive Simpson in
one dimension is independent of the native 2-D Gaussian/Jacobian code.
"""
import math


def integral(function, tolerance=2e-15):
    def segment(a,b,fa,fm,fb,whole,budget,depth):
        m=(a+b)/2; l=(a+m)/2; r=(m+b)/2
        fl,fr=function(l),function(r)
        left=(m-a)*(fa+4*fl+fm)/6
        right=(b-m)*(fm+4*fr+fb)/6
        delta=left+right-whole
        if abs(delta)<=15*budget:return left+right+delta/15
        if not depth:raise AssertionError('reference integration did not converge')
        return segment(a,m,fa,fl,fm,left,budget/2,depth-1)+segment(m,b,fm,fr,fb,right,budget/2,depth-1)
    a,m,b=0.,.5,1.;fa,fm,fb=map(function,(a,m,b))
    return segment(a,b,fa,fm,fb,(fa+4*fm+fb)/6,tolerance,25)


def graph_points(k=.5):
    return ((0.,0.,0.),(1.,0.,k/2),(0.,1.,0.),(.5,0.,k/8),(.5,.5,k/8),(0.,.5,0.))


def graph_reference(k=.5):
    j=lambda r:math.sqrt(1+k*k*r*r)
    polynomials=(lambda r:2*(1-r)**3/3-(1-r)**2/2,
                 lambda r:(2*r*r-r)*(1-r),
                 lambda r:2*(1-r)**3/3-(1-r)**2/2,
                 lambda r:2*r*(1-r)**2,
                 lambda r:2*r*(1-r)**2,
                 lambda r:2*(1-r)**3/3)
    return {'weights':tuple(integral(lambda r,p=p:p(r)*j(r)) for p in polynomials),
            'area':integral(lambda r:(1-r)*j(r)),
            'x':integral(lambda r:r*(1-r)*j(r)),
            'y':integral(lambda r:(1-r)**2*j(r)/2),
            'x2':integral(lambda r:r*r*(1-r)*j(r))}


def read_native_input(path):
    nodes={};elements={};terms={};section=''
    try:
        for raw in path.read_text(encoding='utf-8').splitlines():
            line=raw.strip()
            if not line or line.startswith('**'):continue
            if line.startswith('*'):
                section=line.split(',')[0].upper()
                if section=='*ELEMENT' and 'TYPE=C3D10' not in line.upper().replace(' ',''):raise ValueError('unsupported element')
                continue
            data=[v.strip() for v in line.split(',') if v.strip()]
            if section=='*NODE':
                node=int(data[0]);xyz=tuple(map(float,data[1:]))
                if node<=0 or node in nodes or len(xyz)!=3 or not all(math.isfinite(v) for v in xyz):raise ValueError('node inventory')
                nodes[node]=xyz
            elif section=='*ELEMENT':
                element=int(data[0]);ids=tuple(map(int,data[1:]))
                if element<=0 or element in elements or len(ids)!=10 or len(set(ids))!=10:raise ValueError('element inventory')
                elements[element]=ids
            elif section=='*CLOAD':
                node,axis,value=int(data[0]),int(data[1]),float(data[2])
                if len(data)!=3 or axis not in (1,2,3) or not math.isfinite(value):raise ValueError('load inventory')
                terms.setdefault(node,[[],[],[]])[axis-1].append(value)
        if not nodes or not elements or not terms or any(n not in nodes for ids in elements.values() for n in ids) or any(n not in nodes for n in terms):raise ValueError('missing inventory')
    except (ValueError,IndexError) as exc:raise ValueError('R1_REFERENCE_INPUT: '+str(exc)) from exc
    return nodes,list(elements.values()),{n:tuple(math.fsum(v) for v in axes) for n,axes in terms.items()}


def boundary_faces(elements):
    from collections import Counter
    faces={};counts=Counter()
    local=((0,1,2,4,5,6),(0,3,1,7,8,4),(1,3,2,8,9,5),(2,3,0,9,7,6))
    for element in elements:
        for indices in local:
            face=tuple(element[i] for i in indices);key=tuple(sorted(face))
            faces[key]=face;counts[key]+=1
    if any(v>2 for v in counts.values()):raise ValueError('R1_REFERENCE_NONMANIFOLD')
    return [face for key,face in faces.items() if counts[key]==1]


def independent_input_reference(path,allowed_nodes,force_z):
    """Separate metric/basis implementation, using bundled NumPy only live.

    Fit the six monomials to the six nodal interpolation conditions instead
    of using native shape/derivative helpers. NumPy constructs an independent
    order-20 rule; no R1 native helper is called. Input bytes are read only.
    """
    import numpy as np
    nodes,elements,loads=read_native_input(path)
    faces=[f for f in boundary_faces(elements) if set(f).issubset(allowed_nodes)]
    if not faces or any(n not in allowed_nodes for n in loads):raise ValueError('R1_REFERENCE_LOAD_REGION')
    q,w=np.polynomial.legendre.leggauss(20);q=(q+1)/2;w=w/2
    r=np.repeat(q,20);s=(1-r)*np.tile(q,20)
    base_weights=np.repeat(w,20)*np.tile(w,20)*(1-r)
    features=lambda x,y:np.column_stack((np.ones_like(x),x,y,x*x,x*y,y*y))
    ref=np.array(((0,0),(1,0),(0,1),(.5,0),(.5,.5),(0,.5)),dtype=float)
    inverse=np.linalg.solve(features(ref[:,0],ref[:,1]),np.eye(6))
    polynomial=features(r,s);basis=polynomial@inverse
    zero=np.zeros_like(r);one=np.ones_like(r)
    dr=np.column_stack((zero,one,zero,2*r,s,zero))
    ds=np.column_stack((zero,zero,one,zero,r,2*s))
    by_node={};areas=[]
    for face in faces:
        xyz=np.array([nodes[n] for n in face]);coefficients=inverse@(xyz-xyz[0])
        measure=base_weights*np.linalg.norm(np.cross(dr@coefficients,ds@coefficients),axis=1)
        weights=basis.T@measure
        if not np.all(np.isfinite(weights)) or not float(np.sum(measure))>0:raise ValueError('R1_REFERENCE_METRIC')
        areas.append(float(np.sum(measure)))
        for n,v in zip(face,weights):by_node.setdefault(n,[]).append(float(v))
    area=math.fsum(areas)
    reference={n:(0.,0.,force_z*math.fsum(v)/area) for n,v in by_node.items()}
    max_error=max(abs(loads.get(n,(0,0,0))[axis]-reference.get(n,(0,0,0))[axis]) for n in set(loads)|set(reference) for axis in range(3))
    moment=[math.fsum(nodes[n][1]*v[2] for n,v in reference.items()),-math.fsum(nodes[n][0]*v[2] for n,v in reference.items()),0.]
    field=lambda p:(p[0]*p[0]+p[1]*p[2])/100
    return {'reference_area_mm2':area,'reference_moment_Nmm':moment,
            'virtual_work_Nmm':math.fsum(v[2]*field(nodes[n]) for n,v in loads.items()),
            'reference_virtual_work_Nmm':math.fsum(v[2]*field(nodes[n]) for n,v in reference.items()),
            'max_nodal_force_error_N':max_error,'boundary_faces':len(faces),
            'compared_nodes':len(set(loads)|set(reference)),'reference_quadrature_order':20}


def support_load_accounting(path):
    """Read-only RF accounting for one static global homogeneous-SPC fixture.

    CalculiX 2.22/2.23 NODE PRINT RF contains reaction plus applied loading at
    printed nodes. No input, force property or solver output is modified.
    General MPC, transformed, distributed or multi-step cases are rejected.
    """
    import hashlib
    nodes,_,loads=read_native_input(path)
    blocks=[]
    for raw in path.read_text(encoding='utf-8').splitlines():
        line=raw.strip()
        if not line or line.startswith('**'):continue
        if line.startswith('*'):blocks.append((line.upper(),[]))
        elif blocks:blocks[-1][1].append(line)
    kind=lambda header:header.split(',')[0].strip()
    unsupported={'*DLOAD','*DSLOAD','*MPC','*EQUATION','*TRANSFORM','*INCLUDE','*DYNAMIC','*AMPLITUDE','*TEMPERATURE'}
    if any(kind(h) in unsupported for h,_ in blocks) or sum(kind(h)=='*STEP' for h,_ in blocks)!=1 or sum(kind(h)=='*STATIC' for h,_ in blocks)!=1:raise ValueError('R1_RF_UNSUPPORTED_STEP_OR_LOAD')
    sets={};bounds=[];prints=[]
    try:
        for header,rows in blocks:
            fields=[v.strip() for v in header.split(',') if v.strip()]
            options=dict(v.split('=',1) for v in fields[1:] if '=' in v)
            if kind(header)=='*NSET':
                if 'GENERATE' in fields:raise ValueError('generated NSET not in fixture contract')
                name=options['NSET'];ids={int(v.strip()) for row in rows for v in row.split(',') if v.strip()}
                if not ids or not ids.issubset(nodes):raise ValueError('unknown NSET inventory')
                sets.setdefault(name,set()).update(ids)
            elif kind(header)=='*BOUNDARY':
                if len(fields)!=1:raise ValueError('boundary options not in fixture contract')
                bounds.extend(rows)
            elif kind(header)=='*NODE PRINT':prints.append((options,rows))
            elif kind(header)=='*CLOAD' and len(fields)!=1:raise ValueError('CLOAD options not in fixture contract')
        if len(prints)!=1:raise ValueError('one RF print set required')
        options,rows=prints[0]
        if options.get('TOTALS') not in ('ONLY','YES') or rows!=['RF']:raise ValueError('unambiguous RF totals required')
        printed=sets[options['NSET']];constrained=set()
        for row in bounds:
            values=[v.strip() for v in row.split(',')]
            if not 2<=len(values)<=4:raise ValueError('boundary format')
            first=int(values[1]);last=int(values[2]) if len(values)>=3 and values[2] else first
            magnitude=float(values[3]) if len(values)==4 and values[3] else 0.
            if not 1<=first<=last<=3 or magnitude!=0:raise ValueError('only homogeneous translations supported')
            ids={int(values[0])} if values[0].isdigit() else sets[values[0].upper()]
            if not ids.issubset(nodes):raise ValueError('unknown boundary nodes')
            constrained.update((n,d) for n in ids for d in range(first,last+1))
        if constrained!={(n,d) for n in printed for d in (1,2,3)}:raise ValueError('RF set must cover all and only fully clamped nodes')
    except (ValueError,KeyError,IndexError) as exc:raise ValueError('R1_RF_UNSUPPORTED_ACCOUNTING: '+str(exc)) from exc
    return {'rf_nset':options['NSET'],'constrained_nodes':len(printed),
            'constrained_cload_N':[math.fsum(v[i] for n,v in loads.items() if n in printed) for i in range(3)],
            'free_cload_N':[math.fsum(v[i] for n,v in loads.items() if n not in printed) for i in range(3)],
            'applied_cload_N':[math.fsum(v[i] for v in loads.values()) for i in range(3)],
            'input_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
