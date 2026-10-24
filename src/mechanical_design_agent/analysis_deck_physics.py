"""Independent checks of the actual frozen native input, before any solve."""
from __future__ import annotations

from decimal import Decimal
import math

from .analysis_equivalent_loads import native_tetra_gravity, native_triangle_pressure, tetra_gravity, triangle_pressure
from .analysis_types import AnalysisError
from .resources.freecad.analysis_mesh_math import FACES, cross, sub, tet10_quality


def consistent_deck_loads(data, *, moment_origin, checkpoint=None):
    """Keep the native quadrature RHS and independent physical integral apart."""
    nodes = {node: list(map(float, point)) for node, point in data['_nodes'].items()}
    by_point = {point: node for node, point in data['_nodes'].items()}
    contributions = {name: {} for name in ('native', 'independent')}
    def add(target, node, vector):
        rows = contributions[target].setdefault(node, [[], [], []])
        for values, value in zip(rows, vector): values.append(value)
    for (point, axis), force in data['loads'].items():
        vector = [0., 0., 0.]; vector[axis-1] = float(force)
        for target in contributions: add(target, by_point[point], vector)
    for kind, element_ids, values in data['_distributed_ids']:
        for element_id in element_ids:
            if checkpoint is not None: checkpoint()
            ids = data['_elements'][int(element_id)]
            points = [nodes[node] for node in ids]
            if kind == 'gravity':
                density = float(data['_material']['DENSITY'][1][0])
                arguments = {'density': density, 'acceleration': list(map(float, values))}
                for target, integrate in (('native', native_tetra_gravity), ('independent', tetra_gravity)):
                    for node, vector in zip(ids, integrate(points, **arguments)['nodal_forces_N']): add(target, node, vector)
            else:
                face = FACES[int(values[0][1])-1]
                opposite = next(i for i in range(4) if i not in face[:3])
                arguments = {'pressure': float(values[1]), 'opposite': points[opposite]}
                for target, integrate in (('native', native_triangle_pressure), ('independent', triangle_pressure)):
                    for index, vector in zip(face, integrate([points[i] for i in face], **arguments)['nodal_forces_N']): add(target, ids[index], vector)
    result = {}
    for name, table in contributions.items():
        nodal = {node: [math.fsum(values) for values in rows] for node, rows in table.items()}
        moments = [cross(sub(nodes[node], moment_origin), force) for node, force in nodal.items()]
        force = [math.fsum(row[i] for row in nodal.values()) for i in range(3)]
        moment = [math.fsum(row[i] for row in moments) for i in range(3)]
        if any(not math.isfinite(value) for row in nodal.values() for value in row) or any(not math.isfinite(value) for value in [*force, *moment]):
            raise AnalysisError('DECK_NONFINITE_INTEGRAL', 'discrete applied-force accounting is nonfinite')
        result[name] = {'nodal_forces_N': nodal, 'force_N': force, 'moment_Nmm': moment}
    return result


def writer_number_matches(actual, expected):
    """Native writer uses 13 significant digits; no physical tolerance change."""
    actual, expected = Decimal(str(actual)), Decimal(str(expected))
    if actual == expected: return True
    if expected == 0: return False
    resolution = Decimal(10) ** (expected.copy_abs().adjusted()-12)
    return abs(actual-expected) <= resolution*Decimal('.500001')


def check_declared_physics(data, *, material, expected_fixed_nodes, allowed_force_nodes, expected_case, validation, expected_pressures=None, minimum_scaled_jacobian=None, checkpoint=None):
    native_material = data['_material']
    if not all(writer_number_matches(actual, material[field]['value']) for actual, field in zip(native_material['ELASTIC'][1], ('youngs_modulus', 'poisson_ratio'))):
        raise AnalysisError('DECK_MATERIAL_MISMATCH', 'actual native elastic constants differ from the frozen declaration')
    inverse = {point: node for node, point in data['_nodes'].items()}
    fixed = {(inverse[point], axis): value for (point, axis), value in data['boundary'].items()}
    expected_boundary = {(node, axis): Decimal(0) for node in expected_fixed_nodes for axis in (1, 2, 3)}
    if fixed != expected_boundary:
        raise AnalysisError('DECK_SUPPORT_MISMATCH', 'actual input adds, removes or changes a declared fixed translation')
    if any(inverse[point] not in allowed_force_nodes for point, axis in data['loads']):
        raise AnalysisError('DECK_FORCE_REGION_MISMATCH', 'native concentrated loads leave the declared physical force regions')
    gravity = [item for item in expected_case['native_objects'] if item['type'] == 'gravity']
    actual_gravity = [item for item in data['_distributed_ids'] if item[0] == 'gravity']
    if len(gravity) != len(actual_gravity):
        raise AnalysisError('DECK_GRAVITY_MISMATCH', 'native self weight was omitted or duplicated')
    if gravity:
        if set(map(int, actual_gravity[0][1])) != set(data['_elements']) or not writer_number_matches(native_material['DENSITY'][1][0], gravity[0]['equivalent_density_tonne_mm3']):
            raise AnalysisError('DECK_GRAVITY_MISMATCH', 'native body density or volume coverage differs from the approved self weight')
        actual, expected = list(map(float, actual_gravity[0][2])), list(map(float, gravity[0]['components_mm_s2']))
        if any(abs(a-b) > 2e-12*max(abs(a), abs(b)) for a, b in zip(actual, expected)):
            raise AnalysisError('DECK_GRAVITY_MISMATCH', 'native acceleration differs beyond combined 13-digit writer rounding')
    elif material['density'] is not None and ('DENSITY' not in native_material or not writer_number_matches(native_material['DENSITY'][1][0], material['density']['value'])):
        raise AnalysisError('DECK_MATERIAL_MISMATCH', 'actual native density differs from the frozen declaration')
    if expected_pressures is not None:
        actual = {}
        for kind, ids, values in data['_distributed_ids']:
            if kind != 'pressure': continue
            for element in ids:
                key = (int(element), int(values[0][1]))
                if key in actual: raise AnalysisError('DECK_PRESSURE_MISMATCH', 'repeated pressure on a face is outside the native profile')
                actual[key] = values[1]
        if set(actual) != set(expected_pressures) or any(not writer_number_matches(actual[key], value) for key, value in expected_pressures.items()):
            raise AnalysisError('DECK_PRESSURE_MISMATCH', 'native pressure magnitude, sign or face coverage differs from the approved contract')
    quality = []
    if minimum_scaled_jacobian is not None:
        for ids in data['_elements'].values():
            if checkpoint is not None: checkpoint()
            try:
                quality.append(tet10_quality([list(map(float, data['_nodes'][node])) for node in ids], minimum_scaled_jacobian))
            except ValueError as exc:
                raise AnalysisError('DECK_MESH_QUALITY_FAILED', 'actual serialized solver nodes fail the approved quadratic quality gate') from exc
    origin = [float(value['value']) for value in validation['moment_origin']]
    integrals = consistent_deck_loads(data, moment_origin=origin, checkpoint=checkpoint)
    comparisons = []
    for route, actual in integrals.items():
        for key, prefix, expected in [('force_N', 'force', expected_case['total_force_N']), ('moment_Nmm', 'moment', expected_case['total_moment_Nmm'])]:
            atol, rtol = float(validation[prefix+'_atol']['value']), float(validation[prefix+'_rtol']['value'])
            checks = [abs(a-float(b)) <= atol+rtol*max(abs(a), abs(float(b))) for a, b in zip(actual[key], expected)]
            comparisons.append({'integration': route, 'quantity': key, 'actual': actual[key], 'expected': list(map(float, expected)), 'passed': all(checks)})
    if not all(item['passed'] for item in comparisons):
        raise AnalysisError('DECK_LOAD_LEDGER_MISMATCH', 'actual native RHS or its independent integral differs from the approved force/moment ledger', details={'comparisons': comparisons})
    return {'status': 'passed', 'comparisons': comparisons, 'integrals': integrals, 'checked_solver_elements': len(quality), 'rf_accounting': 'Subtract native discrete CLOAD/DLOAD at fixed nodes from native RF; independent integration checks physical consistency.'}
