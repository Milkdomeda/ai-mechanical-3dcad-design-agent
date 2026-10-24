"""Read-only, deliberately bounded native static C3D10 input comparison."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path

from fea_native_probe import _near


OPTIONS = {
    'NODE': {'NSET'}, 'ELEMENT': {'TYPE','ELSET'}, 'NSET': {'NSET','GENERATE'},
    'ELSET': {'ELSET','GENERATE'}, 'MATERIAL': {'NAME'}, 'ELASTIC': set(),
    'DENSITY': set(), 'EXPANSION': {'ZERO'}, 'SOLID SECTION': {'ELSET','MATERIAL'},
    'PHYSICAL CONSTANTS': {'ABSOLUTE ZERO','STEFAN BOLTZMANN'},
    'STEP': {'INC','NLGEOM'}, 'STATIC': set(), 'BOUNDARY': set(), 'CLOAD': set(),
    'NODE FILE': {'FREQUENCY'}, 'EL FILE': {'FREQUENCY'},
    'NODE PRINT': {'NSET','TOTALS','FREQUENCY'}, 'EL PRINT': {'ELSET','TOTALS','FREQUENCY'},
    'OUTPUT': {'FREQUENCY'}, 'END STEP': set(),
}


def number(text):
    try:
        value=Decimal(text.strip().replace('D','E').replace('d','e'))
        if not value.is_finite() or not math.isfinite(float(value)):raise ValueError('nonfinite number')
        return value
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError('invalid number') from exc


def integer(text):
    value=number(text)
    if value!=int(value) or value<=0:raise ValueError('positive integer required')
    return int(value)


def records(path):
    result=[]
    if Path(path).stat().st_size>64*1024*1024:raise ValueError('input size limit')
    for raw in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line=raw.strip()
        if not line or line.startswith('**'):continue
        if line.startswith('*'):
            cells=[v.strip().upper() for v in line[1:].split(',')]
            keyword=cells.pop(0);options={}
            if keyword not in OPTIONS:raise ValueError('unsupported card '+keyword)
            for cell in cells:
                if not cell:continue
                key,sep,value=cell.partition('=')
                if key not in OPTIONS[keyword] or key in options:raise ValueError('unsupported/duplicate option '+cell)
                options[key]=value if sep else None
            result.append((keyword,options,[]))
        else:
            if not result:raise ValueError('data before card')
            cells=[v.strip().upper() for v in line.split(',')]
            while cells and not cells[-1]:cells.pop()
            if not cells or any(not v for v in cells):raise ValueError('empty data field')
            result[-1][2].append(cells)
    return result


def parse_input(path):
    nodes={};elements={};sets={'NSET':defaultdict(list),'ELSET':defaultdict(list)}
    materials={};sections=[];boundary=[];loads=[];output=[];meta=[]
    material=None;step=0;static=0
    for keyword,options,rows in records(path):
        if keyword=='STEP':
            if step!=0:raise ValueError('one static step only')
            step=1
        elif keyword=='END STEP':
            if step!=1 or rows:raise ValueError('step closure')
            step=2
        elif keyword in {'STATIC','BOUNDARY','CLOAD','NODE FILE','EL FILE','NODE PRINT','EL PRINT','OUTPUT'}:
            if step!=1:raise ValueError('card outside step')
        elif step!=0:raise ValueError('definition inside/after step')
        if keyword=='NODE':
            if not rows:raise ValueError('empty nodes')
            for row in rows:
                if len(row)!=4:raise ValueError('node arity')
                node=integer(row[0])
                if node in nodes:raise ValueError('duplicate node')
                nodes[node]=tuple(map(number,row[1:]))
                if 'NSET' in options:sets['NSET'][options['NSET']].append(node)
        elif keyword=='ELEMENT':
            if options.get('TYPE')!='C3D10' or not rows:raise ValueError('only C3D10')
            for row in rows:
                if len(row)!=11:raise ValueError('C3D10 arity')
                element,*ids=map(integer,row)
                if element in elements or len(set(ids))!=10:raise ValueError('element inventory')
                elements[element]=tuple(ids)
                if 'ELSET' in options:sets['ELSET'][options['ELSET']].append(element)
        elif keyword in sets:
            name=options.get(keyword)
            if not name or not rows:raise ValueError('empty set')
            for row in rows:
                if 'GENERATE' in options:
                    if len(row) not in (2,3):raise ValueError('GENERATE arity')
                    start,end=map(integer,row[:2]);increment=integer(row[2]) if len(row)==3 else 1
                    if end<start or (end-start)%increment or end-start>1000000:raise ValueError('GENERATE range')
                    sets[keyword][name].extend(range(start,end+1,increment))
                else:
                    sets[keyword][name].extend(integer(v) if v.isdigit() else v for v in row)
        elif keyword=='MATERIAL':
            material=options.get('NAME')
            if not material or material in materials or rows:raise ValueError('material identity')
            materials[material]={}
        elif keyword in {'ELASTIC','DENSITY','EXPANSION'}:
            if material is None or keyword in materials[material] or len(rows)!=1:raise ValueError('material data')
            arity=2 if keyword=='ELASTIC' else 1
            if len(rows[0])!=arity:raise ValueError('material arity')
            materials[material][keyword]=(tuple(sorted((k,number(v)) for k,v in options.items())),tuple(map(number,rows[0])))
        elif keyword=='SOLID SECTION':
            if set(options)!={'ELSET','MATERIAL'} or rows:raise ValueError('section data')
            sections.append((options['ELSET'],options['MATERIAL']))
        elif keyword=='BOUNDARY':
            if not rows:raise ValueError('empty boundary')
            boundary.extend(rows)
        elif keyword=='CLOAD':
            if not rows:raise ValueError('empty loads')
            loads.extend(rows)
        elif keyword in {'NODE FILE','EL FILE','NODE PRINT','EL PRINT'}:
            if not rows:raise ValueError('empty output request')
            output.append((keyword,options,tuple(sorted(v for row in rows for v in row))))
        else:
            if keyword=='STATIC':
                static+=1
                if len(rows)!=1 or len(rows[0])!=4:raise ValueError('static data')
            elif rows:raise ValueError('unexpected card data')
            meta.append((keyword,tuple(sorted((k,str(number(v).normalize()) if v is not None and k not in {'NLGEOM'} else v) for k,v in options.items())),tuple(tuple(map(number,row)) for row in rows)))
    if step!=2 or static!=1 or not all((nodes,elements,materials,sections,boundary,loads,output)):
        raise ValueError('incomplete static input')
    if len(set(nodes.values()))!=len(nodes):raise ValueError('ambiguous coincident nodes')
    if any(n not in nodes for ids in elements.values() for n in ids):raise ValueError('undefined node')
    if any('ELASTIC' not in value for value in materials.values()):raise ValueError('missing elastic data')
    element_keys={e:tuple(nodes[n] for n in ids) for e,ids in elements.items()}
    if len(set(element_keys.values()))!=len(elements):raise ValueError('duplicate physical element')

    def resolve(kind,name,trail=()):
        if name not in sets[kind] or name in trail:raise ValueError('undefined/cyclic set')
        result=set()
        for value in sets[kind][name]:
            result.update(resolve(kind,value,trail+(name,)) if isinstance(value,str) else (value,))
        universe=nodes if kind=='NSET' else elements
        if not result or not result.issubset(universe):raise ValueError('invalid set member')
        return result

    canonical_sets={kind:{name:tuple(sorted((nodes if kind=='NSET' else element_keys)[v] for v in resolve(kind,name))) for name in sets[kind]} for kind in sets}
    material_keys={name:tuple(sorted(value.items())) for name,value in materials.items()}
    def named(kind,name):
        if name not in canonical_sets[kind]:raise ValueError('undefined named set')
        return canonical_sets[kind][name]
    def node_ids(token):return [integer(token)] if token.isdigit() else resolve('NSET',token)
    fixed={};forces=defaultdict(lambda:Decimal(0))
    for row in boundary:
        if not 2<=len(row)<=4:raise ValueError('boundary arity')
        first=integer(row[1]);last=integer(row[2]) if len(row)>2 else first
        value=number(row[3]) if len(row)>3 else Decimal(0)
        if not 1<=first<=last<=3:raise ValueError('boundary dof')
        for node in node_ids(row[0]):
            if node not in nodes:raise ValueError('undefined boundary node')
            for axis in range(first,last+1):
                key=(nodes[node],axis)
                if key in fixed and fixed[key]!=value:raise ValueError('conflicting boundary')
                fixed[key]=value
    for row in loads:
        if len(row)!=3:raise ValueError('load arity')
        axis=integer(row[1]);value=number(row[2])
        if axis not in (1,2,3):raise ValueError('load axis')
        for node in node_ids(row[0]):
            if node not in nodes:raise ValueError('undefined load node')
            forces[(nodes[node],axis)]+=value
    canonical_output=[]
    for keyword,options,data in output:
        values=[]
        for key,value in options.items():
            values.append((key,named(key,value) if key in sets else value))
        canonical_output.append((keyword,tuple(sorted(values)),data))
    try:canonical_sections=tuple(sorted((named('ELSET',e),material_keys[m]) for e,m in sections))
    except KeyError as exc:raise ValueError('undefined section material') from exc
    return {'nodes':tuple(sorted(nodes.values())),'elements':tuple(sorted(element_keys.values())),
            'sets':{kind:tuple(sorted(value.values())) for kind,value in canonical_sets.items()},
            'materials':tuple(sorted(material_keys.values())),'sections':canonical_sections,
            'boundary':fixed,'loads':dict(forces),'output':tuple(sorted(canonical_output)),
            'steps_and_constants':tuple(meta)}


def metrics(data):
    f=[Decimal(0)]*3;m=[Decimal(0)]*3;work=Decimal(0)
    for (point,axis),value in data['loads'].items():
        x,y,z=point;v=[Decimal(0)]*3;v[axis-1]=value
        fx,fy,fz=v
        for i,a in enumerate(v):f[i]+=a
        for i,a in enumerate((y*fz-z*fy,z*fx-x*fz,x*fy-y*fx)):m[i]+=a
        if axis==3:work+=value*(x*x+y*z)/100
    return {'nodes':len(data['nodes']),'elements':len(data['elements']),
            'force_N':list(map(float,f)),'moment_Nmm':list(map(float,m)),
            'quadratic_virtual_work_Nmm':float(work)}


def compare_inputs(reference,exported,*,curved=False):
    result={'schema_version':'P0GuiInputComparison/v1','status':'failed','checks':{},'errors':[]}
    try:
        a,b=map(parse_input,(reference,exported))
        result['reference']=metrics(a);result['exported']=metrics(b)
        result['input_sha256']={k:hashlib.sha256(Path(p).read_bytes()).hexdigest() for k,p in (('reference',reference),('exported',exported))}
        checks=result['checks']
        for key in ('nodes','elements','sets','materials','sections','boundary','output','steps_and_constants'):
            checks[key]=a[key]==b[key]
        keys=set(a['loads'])|set(b['loads'])
        error=max(abs(a['loads'].get(k,0)-b['loads'].get(k,0)) for k in keys)
        result['max_nodal_force_error_N']=float(error);checks['nodal_loads']=error<=Decimal('1e-9')
        for key,tolerance in (('force_N',1e-9),('moment_Nmm',.01 if curved else 1e-6)):
            checks[key]=all(_near(x,y,tolerance) for x,y in zip(result['exported'][key],result['reference'][key]))
        checks['quadratic_virtual_work']=_near(result['exported']['quadratic_virtual_work_Nmm'],result['reference']['quadratic_virtual_work_Nmm'],1e-9)
        if all(checks.values()):result['status']='passed'
    except (ValueError,OSError,KeyError,OverflowError) as exc:
        result['errors'].append(str(exc))
    return result
