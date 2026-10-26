"""Independent consistent DLOAD accounting, never a solver input writer.

CalculiX RF includes both reaction and applied nodal loads. These integrals
are separately compared with native solves before enabling a load profile.
No total-force shortcut may substitute for missing native per-node RF.
Reference: official ccx 2.23 manual sections 6.11.5 and 7.99.
"""
from __future__ import annotations

import math

from .analysis_types import AnalysisError
from .resources.freecad.analysis_mesh_math import EDGES, _jacobian, cross, det, dot, norm, sub


def _points(points, count):
    if len(points) != count or any(len(point) != 3 or any(type(value) not in (int, float) or not math.isfinite(value) for value in point) for point in points):
        raise AnalysisError('DECK_LOAD_GEOMETRY_INVALID', 'consistent load integration needs complete finite quadratic geometry')


def tetra_gravity(points, *, density, acceleration):
    _points(points, 10)
    if type(density) not in (int, float) or not math.isfinite(density) or density <= 0:
        raise AnalysisError('DECK_DENSITY_INVALID', 'gravity needs a finite positive tonne/mm^3 density')
    _points([acceleration], 1)
    abscissae = (.06943184420297371, .33000947820757187, .6699905217924281, .9305681557970262)
    weights = (.17392742256872692, .32607257743127307, .32607257743127307, .17392742256872692)
    volumes, integrals = [], [[] for _ in range(10)]
    # Duffy maps the cube to a tetrahedron. Four-point Gauss in each axis
    # integrates N_i (degree2) * det(J) (degree3) exactly for a T10 map;
    # the transformed maximum univariate degree, including Duffy J, is7.
    for a, wa in zip(abscissae, weights):
        for b, wb in zip(abscissae, weights):
            for c, wc in zip(abscissae, weights):
                x, y, z = a, (1-a)*b, (1-a)*(1-b)*c
                bary = [1-x-y-z, x, y, z]
                jacobian = det(_jacobian(points, bary))
                if jacobian <= 0 or not math.isfinite(jacobian):
                    raise AnalysisError('MESH_NONPOSITIVE_JACOBIAN', 'body-load integration encountered a nonpositive quadratic map')
                scale = wa*wb*wc*(1-a)**2*(1-b)*jacobian
                volumes.append(scale)
                shape = [value*(2*value-1) for value in bary] + [4*bary[i]*bary[j] for i, j in EDGES]
                for values, value in zip(integrals, shape): values.append(value*scale)
    return {'volume_mm3': math.fsum(volumes), 'nodal_forces_N': [[density*math.fsum(values)*value for value in acceleration] for values in integrals], 'method': 'T10 consistent volume integration, Duffy Gauss4x4x4'}


def native_tetra_gravity(points, *, density, acceleration):
    """Reconstruct ccx2.22/2.23's discrete RHS, separately from exact64.

    Fixed reference: official ccx_2.22/2.23.src.tar.bz2, e_c3d.f and gauss.f.
    The native four-point scheme can differ from the independent degree5
    integral on curved maps. Both values are retained and checked, not mixed.
    """
    _points(points, 10)
    _points([acceleration], 1)
    if type(density) not in (int, float) or not math.isfinite(density) or density <= 0:
        raise AnalysisError('DECK_DENSITY_INVALID', 'native gravity accounting needs positive consistent density')
    small, large = .138196601125011, .585410196624968
    locations = [(small, small, small), (large, small, small), (small, large, small), (small, small, large)]
    volume, integrals = [], [[] for _ in range(10)]
    for x, y, z in locations:
        bary = [1-x-y-z, x, y, z]
        determinant = det(_jacobian(points, bary))
        if determinant <= 0 or not math.isfinite(determinant): raise AnalysisError('MESH_NONPOSITIVE_JACOBIAN', 'native body-load map is nonpositive or nonfinite')
        scale = determinant*.041666666666667
        volume.append(scale)
        shape = [value*(2*value-1) for value in bary]+[4*bary[i]*bary[j] for i, j in EDGES]
        for values, value in zip(integrals, shape): values.append(value*scale)
    return {'volume_mm3': math.fsum(volume), 'nodal_forces_N': [[density*math.fsum(values)*value for value in acceleration] for values in integrals], 'method': 'ccx2.22/2.23 T10 native four-point gravity RHS'}


def triangle_pressure(points, *, pressure, opposite, _native_rule=False):
    _points(points, 6)
    _points([opposite], 1)
    if type(pressure) not in (int, float) or not math.isfinite(pressure):
        raise AnalysisError('DECK_PRESSURE_INVALID', 'pressure needs a finite signed N/mm^2 value')
    normal = cross(sub(points[1], points[0]), sub(points[2], points[0]))
    length = norm(normal)
    if length == 0 or not math.isfinite(length) or any(abs(dot(sub(point, points[0]), normal)) > 1e-10*length*max(1., norm(sub(point, points[0]))) for point in points[3:]):
        raise AnalysisError('CURVED_PRESSURE_NOT_VALIDATED', 'this native load profile is qualified for planar pressure only')
    # The face ordering comes from C3D10; choose the physical outward normal
    # independently using its opposite corner. Positive pressure is inward.
    orientation = -1 if dot(normal, sub(opposite, points[0])) > 0 else 1
    quadrature = [((1/3,)*3, .225/2)]
    for small, large, weight in ((.470142064105115, .059715871789770, .132394152788506/2), (.101286507323456, .797426985353087, .125939180544827/2)):
        for i in range(3): quadrature.append((tuple(large if j == i else small for j in range(3)), weight))
    if _native_rule:
        small, large = .166666666666667, .666666666666667
        quadrature = [((1-x-y, x, y), .166666666666667) for x, y in ((small, small), (large, small), (small, large))]
    local = [sub(point, points[0]) for point in points]
    forces = [[[] for _ in range(3)] for _ in range(6)]
    area = []
    gradients = ((-1, -1), (1, 0), (0, 1))
    edges = ((0, 1), (1, 2), (0, 2))
    for bary, weight in quadrature:
        shape = [value*(2*value-1) for value in bary] + [4*bary[i]*bary[j] for i, j in edges]
        derivative = [[(4*bary[i]-1)*value for value in gradients[i]] for i in range(3)]
        derivative.extend([[4*(bary[i]*gradients[j][k]+bary[j]*gradients[i][k]) for k in range(2)] for i, j in edges])
        tangent = [[sum(local[n][axis]*derivative[n][k] for n in range(6)) for axis in range(3)] for k in range(2)]
        vector_area = cross(*tangent)
        if any(not math.isfinite(value) for value in vector_area) or dot(vector_area, normal) <= 0:
            raise AnalysisError('DECK_PRESSURE_MAPPING_INVALID', 'planar quadratic face mapping folds or reverses')
        area.append(norm(vector_area)*weight)
        for node, value in enumerate(shape):
            for axis in range(3): forces[node][axis].append(-pressure*orientation*vector_area[axis]*weight*value)
    return {'area_mm2': math.fsum(area), 'nodal_forces_N': [[math.fsum(values) for values in node] for node in forces], 'method': 'ccx2.22/2.23 T6 native three-point pressure RHS' if _native_rule else 'planar T6 consistent pressure, degree5 triangle rule'}


def native_triangle_pressure(points, *, pressure, opposite):
    return triangle_pressure(points, pressure=pressure, opposite=opposite, _native_rule=True)


def recover_support_reactions(*, nodes, fixed_node_ids, raw_rf, applied_nodal_forces, moment_origin):
    fixed = set(fixed_node_ids)
    if not fixed or len(fixed) != len(fixed_node_ids) or set(raw_rf) != fixed or not fixed <= set(nodes):
        raise AnalysisError('RESULT_RF_INCOMPLETE', 'every constrained node needs a native RF vector with an exact mesh identity')
    _points([moment_origin], 1)
    corrected, moments = {}, []
    for node in sorted(fixed):
        point, raw, applied = nodes[node], raw_rf[node], applied_nodal_forces.get(node, [0., 0., 0.])
        _points([point, raw, applied], 3)
        force = [a-b for a, b in zip(raw, applied)]
        corrected[node] = force
        moments.append(cross(sub(point, moment_origin), force))
    return {'force_N': [math.fsum(value[i] for value in corrected.values()) for i in range(3)], 'moment_Nmm': [math.fsum(value[i] for value in moments) for i in range(3)], 'nodal_reactions_N': corrected, 'accounting': 'native RF - all CLOAD and consistent DLOAD at each constrained node'}
