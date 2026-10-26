"""Read-only native static C3D10 semantics, promoted from the P0 comparator.

Only reviewed cards are recognized; no solver input is executed or rewritten.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import math
from pathlib import Path

import json
import re
from pathlib import PureWindowsPath

from . import secure_fs
from .analysis_types import AnalysisError
from .analysis_units import canonical_decimal


OPTIONS = {
    'NODE': {'NSET'}, 'ELEMENT': {'TYPE','ELSET'}, 'NSET': {'NSET','GENERATE'},
    'ELSET': {'ELSET','GENERATE'}, 'MATERIAL': {'NAME'}, 'ELASTIC': set(),
    'DENSITY': set(), 'EXPANSION': {'ZERO'}, 'SOLID SECTION': {'ELSET','MATERIAL'},
    'PHYSICAL CONSTANTS': {'ABSOLUTE ZERO','STEFAN BOLTZMANN'},
    'STEP': {'INC','NLGEOM'}, 'STATIC': set(), 'BOUNDARY': set(), 'CLOAD': set(),
    'DLOAD': set(), 'INCLUDE': {'INPUT'},
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


def records(path, *, allowed_root, inventory, stack=(), _budget=None):
    if _budget is None: _budget = {'bytes': 64*1024*1024, 'files': 32}
    root = secure_fs.validate_managed_path(allowed_root, allow_missing_leaf=False).path
    located = secure_fs.validate_managed_path(path, allow_missing_leaf=False)
    if not located.path.is_relative_to(root) or str(located.path) in stack or len(stack) >= 16:
        raise AnalysisError('DECK_REFERENCE_UNSAFE', 'solver references must remain acyclic and inside their managed root')
    if _budget['files'] <= 0 or located.path.stat().st_size > _budget['bytes']:
        raise AnalysisError('DECK_REFERENCE_UNSAFE', 'expanded input exceeds the bounded reference or byte budget')
    _budget['files'] -= 1
    content = secure_fs.read_managed_file(located.path)
    _budget['bytes'] -= len(content.content)
    if content.identity != located.identity or content.link_count != 1 or _budget['bytes'] < 0:
        raise AnalysisError('DECK_REFERENCE_UNSAFE', 'solver reference identity, link count or size is invalid')
    reference = {'relative_path': located.path.relative_to(root).as_posix(), 'sha256': content.sha256}
    if reference not in inventory: inventory.append(reference)
    if len(inventory) > 32: raise ValueError('input reference count limit')
    result = []
    for raw in content.content.decode('utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('**'): continue
        if line.startswith('*'):
            cells = [v.strip() for v in line[1:].split(',')]
            keyword = cells.pop(0).upper(); options = {}
            if keyword not in OPTIONS: raise ValueError('unsupported card')
            for cell in cells:
                if not cell: continue
                key, sep, value = cell.partition('='); key = key.upper()
                if key not in OPTIONS[keyword] or key in options: raise ValueError('unsupported or duplicate card option')
                options[key] = value if keyword == 'INCLUDE' else (value.upper() if sep else None)
            if keyword == 'INCLUDE':
                token = options.get('INPUT')
                if not token or token.startswith(('/', '\\')) or PureWindowsPath(token).drive or ':' in token or any(part in ('', '.', '..') for part in token.replace('\\', '/').split('/')):
                    raise AnalysisError('DECK_REFERENCE_UNSAFE', 'include paths must be explicit local relative paths without traversal or drives')
                target = located.path.parent.joinpath(*token.replace('\\', '/').split('/'))
                result.extend(records(target, allowed_root=root, inventory=inventory, stack=(*stack, str(located.path)), _budget=_budget))
            else: result.append((keyword, options, []))
        else:
            if not result: raise ValueError('data before card')
            cells = [value.strip().upper() for value in line.split(',')]
            while cells and not cells[-1]: cells.pop()
            if not cells or any(not value for value in cells): raise ValueError('empty input cell')
            result[-1][2].append(cells)
    return result


def _parse_input(path, *, allowed_root):
    with localcontext() as context:
        context.prec = 256
        return _parse_input_exact(path, allowed_root=allowed_root)


def _parse_input_exact(path, *, allowed_root):
    inventory = []
    nodes={};elements={};sets={'NSET':defaultdict(list),'ELSET':defaultdict(list)}
    materials={};sections=[];boundary=[];loads=[];distributed=[];output=[];meta=[]
    material=None;step=0;static=0;output_cards=set()
    for keyword,options,rows in records(path, allowed_root=allowed_root, inventory=inventory):
        if keyword in {'NODE FILE','EL FILE','NODE PRINT','EL PRINT','OUTPUT'}:
            if keyword in output_cards:raise ValueError('repeated output card outside reviewed native subset')
            output_cards.add(keyword)
        if keyword=='STEP':
            if step!=0 or options.get('NLGEOM', 'NO') != 'NO':raise ValueError('one geometrically linear static step only')
            step=1
        elif keyword=='END STEP':
            if step!=1 or rows:raise ValueError('step closure')
            step=2
        elif keyword in {'STATIC','BOUNDARY','CLOAD','DLOAD','NODE FILE','EL FILE','NODE PRINT','EL PRINT','OUTPUT'}:
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
        elif keyword=='DLOAD':
            if not rows: raise ValueError('empty distributed loads')
            distributed.extend(rows)
        elif keyword in {'NODE FILE','EL FILE','NODE PRINT','EL PRINT'}:
            if not rows:raise ValueError('empty output request')
            output.append((keyword,options,tuple(sorted(v for row in rows for v in row))))
        else:
            if keyword=='STATIC':
                static+=1
                if len(rows)!=1 or len(rows[0])!=4:raise ValueError('static data')
            elif rows:raise ValueError('unexpected card data')
            meta.append((keyword,tuple(sorted((k,str(number(v).normalize()) if v is not None and k not in {'NLGEOM'} else v) for k,v in options.items())),tuple(tuple(map(number,row)) for row in rows)))
    if step!=2 or static!=1 or not all((nodes,elements,materials,sections,boundary,(loads or distributed),output)):
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
        if not 1<=first<=last<=3 or value != 0:raise ValueError('only fixed global translational DOFs are supported')
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
    canonical_distributed, raw_distributed = [], []
    for row in distributed:
        if len(row) < 3: raise ValueError('distributed load arity')
        ids = [integer(row[0])] if row[0].isdigit() else sorted(resolve('ELSET', row[0]))
        if any(element not in elements for element in ids): raise ValueError('distributed load element missing')
        if row[1] == 'GRAV':
            if len(row) != 6: raise ValueError('gravity arity')
            magnitude = number(row[2]); direction = list(map(number, row[3:]))
            if magnitude <= 0 or abs(sum(value*value for value in direction)-1) > Decimal('1e-10') or any('DENSITY' not in material for material in materials.values()): raise ValueError('gravity density or direction invalid')
            value = tuple(magnitude*component for component in direction)
            kind = 'gravity'
        elif re.fullmatch('P[1-4]', row[1]):
            if len(row) != 3: raise ValueError('pressure arity')
            value = (row[1], number(row[2])); kind = 'pressure'
        else: raise ValueError('unsupported distributed load')
        canonical_distributed.append((kind, tuple(sorted(element_keys[element] for element in ids)), value))
        raw_distributed.append((kind, tuple(map(str, ids)), value))
    canonical_output=[]
    rf_sets=[]
    for keyword,options,data in output:
        if keyword=='NODE PRINT' and 'RF' in data:
            rf_sets.append({'set_name':options['NSET'],'node_ids':sorted(resolve('NSET',options['NSET']))})
        values=[]
        for key,value in options.items():
            values.append((key,named(key,value) if key in sets else value))
        canonical_output.append((keyword,tuple(sorted(values)),data))
    try:canonical_sections=tuple(sorted((named('ELSET',e),material_keys[m]) for e,m in sections))
    except KeyError as exc:raise ValueError('undefined section material') from exc
    if len(materials) != 1:
        raise ValueError('this profile requires one homogeneous material')
    covered = [element for name, material in sections for element in resolve('ELSET', name)]
    if len(covered) != len(set(covered)) or set(covered) != set(elements):
        raise ValueError('material section must cover every volume exactly once')
    for material in materials.values():
        youngs, poisson = material['ELASTIC'][1]
        if youngs <= 0 or not -1 < poisson < Decimal('.5') or ('DENSITY' in material and material['DENSITY'][1][0] <= 0):
            raise ValueError('nonphysical material')
    return {'nodes':tuple(sorted(nodes.values())),'elements':tuple(sorted(element_keys.values())),
            'sets':{kind:tuple(sorted(value.values())) for kind,value in canonical_sets.items()},
            'materials':tuple(sorted(material_keys.values())),'sections':canonical_sections,
            'boundary':fixed,'loads':dict(forces),'output':tuple(sorted(canonical_output)),
            'steps_and_constants':tuple(meta), 'distributed':tuple(sorted(canonical_distributed)), '_distributed_ids':raw_distributed, '_files':inventory,
            '_nodes':nodes, '_elements':elements, '_material':next(iter(materials.values())), '_rf_sets':rf_sets}


def _json_value(value):
    if isinstance(value, Decimal): return canonical_decimal(str(value))
    if isinstance(value, (tuple, list)): return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if all(type(key) is str for key in value): return {key: _json_value(item) for key, item in value.items()}
        return [_json_value([*key, item]) for key, item in sorted(value.items())]
    return value


def inspect_native_deck(path, *, allowed_root):
    try:
        data = _parse_input(Path(path), allowed_root=Path(allowed_root))
        semantics = _json_value({key: value for key, value in data.items() if not key.startswith('_')})
        encoded = json.dumps(semantics, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
        rf = [options for keyword, options, variables in data['output'] if keyword == 'NODE PRINT' and 'RF' in variables]
        totals = [dict(options).get('TOTALS', 'NO') for options in rf]
        if any(value not in {'ONLY', 'YES', 'NO'} for value in totals): raise ValueError('unsupported RF output mode')
        rf_output = 'nodal_and_totals' if totals and all(value == 'YES' for value in totals) else ('totals_only' if totals and all(value == 'ONLY' for value in totals) else ('nodal_only' if totals and all(value == 'NO' for value in totals) else 'missing_or_mixed'))
        force, moment = [Decimal(0)]*3, [Decimal(0)]*3
        for (point, axis), value in data['loads'].items():
            x, y, z = point
            vector = [Decimal(0)]*3; vector[axis-1] = value
            for i, component in enumerate(vector): force[i] += component
            for i, component in enumerate((y*vector[2]-z*vector[1], z*vector[0]-x*vector[2], x*vector[1]-y*vector[0])): moment[i] += component
        return {'schema_version': 'AnalysisDeckInspection/v1', 'unit_system': 'N-mm-s-tonne', 'files': sorted(data['_files'], key=lambda item: item['relative_path']), 'semantic_sha256': hashlib.sha256(encoded).hexdigest(), 'semantics': semantics, 'node_count': len(data['nodes']), 'element_count': len(data['elements']), 'force_N': _json_value(force), 'moment_Nmm': _json_value(moment), 'force_scope': 'CLOAD only; DLOAD needs separate consistent integration', 'distributed_loads': _json_value(data['_distributed_ids']), 'rf_output': rf_output}
    except AnalysisError:
        raise
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, secure_fs.SecureFilesystemError) as exc:
        raise AnalysisError('DECK_INVALID', 'native input is outside the reviewed closed linear-static subset') from exc


def compare_deck_semantics(expected, actual):
    equivalent = expected['unit_system'] == actual['unit_system'] and expected['semantic_sha256'] == actual['semantic_sha256'] and expected['semantics'] == actual['semantics']
    return {'schema_version': 'AnalysisDeckComparison/v1', 'equivalent': equivalent, 'expected_semantic_sha256': expected['semantic_sha256'], 'actual_semantic_sha256': actual['semantic_sha256']}


def build_native_deck(spec, geometry, mesh, output_dir, settings, *, case_id, transaction=None):
    """Mesh input is a closed locator/receipt/geometry/bindings bundle.

    Returning a generated deck does not establish RF completeness, equilibrium,
    convergence or a solve pass. Those are separately gated downstream.
    """
    from .analysis_loads import build_load_ledger
    from .analysis_native import _read
    from .analysis_native_transaction import NativeTransaction
    from .analysis_protocol import canonical_record, parse_record, record_sha256
    if type(mesh) is not dict or set(mesh) != {'directory', 'receipt', 'geometry_manifest', 'region_bindings'}:
        raise AnalysisError('DECK_MESH_BINDING_INVALID', 'native deck construction requires a complete scoped mesh locator and review binding')
    spec = parse_record(canonical_record(spec, schema_name='spec').decode('utf-8'), schema_name='spec')
    if spec['status'] != 'approved':
        raise AnalysisError('ANALYSIS_SPEC_NOT_APPROVED', 'native solver input requires a frozen approved specification')
    receipt = parse_record(canonical_record(mesh['receipt'], schema_name='mesh-receipt').decode('utf-8'), schema_name='mesh-receipt')
    from .analysis_mesh import plan_mesh_levels
    level_reference = receipt['artifacts']['level']
    level_content = _read(Path(mesh['directory']) / level_reference['relative_path'])
    if level_content.sha256 != level_reference['sha256']:
        raise AnalysisError('DECK_MESH_CHANGED', 'serialized mesh level or budget evidence changed')
    actual_level = parse_record(level_content.content.decode('utf-8'), schema_name='mesh-level')
    planned = plan_mesh_levels(spec)
    if receipt['level'] >= len(planned) or actual_level != planned[receipt['level']]:
        raise AnalysisError('DECK_MESH_LEVEL_MISMATCH', 'mesh refinement and quality strategy differs from the frozen specification')
    bundle = {'geometry': mesh['geometry_manifest'], 'regions': mesh['region_bindings']}
    ledger = build_load_ledger(spec, bundle)
    if receipt['geometry_sha256'] != ledger['geometry_sha256'] or receipt['region_bindings_sha256'] != ledger['bindings_sha256'] or _read(geometry).sha256 != ledger['geometry_sha256']:
        raise AnalysisError('DECK_MESH_BINDING_INVALID', 'deck inputs differ from the approved geometry and physical region revision')
    selected = [case for case in ledger['cases'] if case['case_id'] == case_id]
    if len(selected) != 1: raise AnalysisError('DECK_CASE_INVALID', 'select exactly one declared load case')
    directory = secure_fs.validate_managed_path(Path(mesh['directory']), allow_missing_leaf=False).path
    reference = receipt['artifacts']['native_mesh']
    source_mesh = directory / reference['relative_path']
    if _read(source_mesh).sha256 != reference['sha256']:
        raise AnalysisError('DECK_MESH_CHANGED', 'native serialized mesh changed before input generation')
    contract = {'spec_sha256': record_sha256(spec, schema_name='spec'), 'geometry_sha256': ledger['geometry_sha256'], 'mesh_sha256': reference['sha256'], 'node_count': receipt['node_count'], 'element_count': receipt['element_count'], 'material': spec['material'], 'case': selected[0]}
    if transaction is None:
        with NativeTransaction(settings) as owned:
            return _build_with_transaction(contract, geometry, source_mesh, output_dir, settings, spec, mesh, owned)
    if transaction.settings != settings:
        raise AnalysisError('DECK_RUNTIME_CHANGED', 'native transaction belongs to a different pinned runtime')
    return _build_with_transaction(contract, geometry, source_mesh, output_dir, settings, spec, mesh, transaction)


def _build_with_transaction(contract, geometry, source_mesh, output_dir, settings, spec, mesh, transaction):
    from .analysis_deck_physics import check_declared_physics, writer_number_matches
    from .analysis_native import _read
    result = transaction.run('build', contract, {'geometry.brep': geometry, 'mesh.unv': source_mesh}, output_dir, timeout_seconds=spec['budget']['stage_seconds'], max_log_bytes=spec['budget']['max_disk_bytes'])
    if result['spec_sha256'] != contract['spec_sha256'] or set(result['artifacts']) != {'input'}:
        raise AnalysisError('DECK_NATIVE_BINDING_CHANGED', 'native writer returned different frozen inputs or undeclared files')
    inspection = inspect_native_deck(Path(output_dir) / result['artifacts']['input']['relative_path'], allowed_root=Path(output_dir))
    if inspection['node_count'] != contract['node_count'] or inspection['element_count'] != contract['element_count']:
        raise AnalysisError('DECK_MESH_INVENTORY_CHANGED', 'native input node or element inventory differs from the validated mesh')
    artifacts = {}
    for name in ('data', 'mapping'):
        reference = mesh['receipt']['artifacts'][name]
        content = _read(Path(mesh['directory']) / reference['relative_path'])
        if content.sha256 != reference['sha256']: raise AnalysisError('DECK_MESH_CHANGED', 'mesh data or semantic node mapping changed')
        artifacts[name] = json.loads(content.content)
    parsed = _parse_input(Path(output_dir) / result['artifacts']['input']['relative_path'], allowed_root=Path(output_dir))
    original_nodes = {row[0]: row[1:] for row in artifacts['data']['nodes']}
    original_elements = {row[0]: tuple(row[1:]) for row in artifacts['data']['elements']}
    if parsed['_elements'] != original_elements or set(parsed['_nodes']) != set(original_nodes) or any(not writer_number_matches(actual, expected) for node, point in parsed['_nodes'].items() for actual, expected in zip(point, original_nodes[node])):
        raise AnalysisError('DECK_MESH_CHANGED', 'native solver connectivity or coordinates differ beyond the documented writer representation')
    regions = {item['region_id']: item for item in artifacts['mapping']['regions']}
    fixed = {node for name in contract['case']['fixed_regions'] for node in regions[name]['node_ids']}
    force_nodes, pressures = set(), {}
    for load in contract['case']['native_objects']:
        if load['type'] == 'total_force': force_nodes.update(regions[load['region_id']]['node_ids'])
        elif load['type'] == 'pressure':
            for pair in regions[load['region_id']]['element_faces']:
                key = tuple(pair)
                if key in pressures: raise AnalysisError('DECK_PRESSURE_OVERLAP_UNSUPPORTED', 'this profile does not combine overlapping pressure objects')
                pressures[key] = Decimal(load['value_MPa'])*(-1 if load['reversed'] else 1)
    verified = check_declared_physics(parsed, material=spec['material'], expected_fixed_nodes=fixed, allowed_force_nodes=force_nodes, expected_case=contract['case'], validation=spec['validation'], expected_pressures=pressures, minimum_scaled_jacobian=float(spec['mesh']['minimum_scaled_jacobian']['value']), checkpoint=transaction.checkpoint)
    loads_path = Path(output_dir) / 'applied-loads.json'
    encoded = json.dumps(verified, ensure_ascii=True, allow_nan=False, sort_keys=True).encode('utf-8')
    secure_fs.atomic_publish_new(loads_path, encoded)
    secure_fs.set_managed_file_readonly(Path(output_dir) / result['artifacts']['input']['relative_path'])
    return {'schema_version': 'AnalysisCaseDeck/v1', 'spec_sha256': contract['spec_sha256'], 'case_id': contract['case']['case_id'], 'directory': str(Path(output_dir)), 'input_ref': result['artifacts']['input'], 'inspection': inspection, 'objects': result['objects'], 'contract': contract, 'applied_loads_ref': {'relative_path': loads_path.name, 'sha256': hashlib.sha256(encoded).hexdigest()}, 'moment_origin_mm':[value['value'] for value in spec['validation']['moment_origin']], 'budget':spec['budget'], 'numerical_acceptance': 'not_executed'}
