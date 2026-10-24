"""Fixed Python 3.11-safe quadratic tetrahedron checks; no CAD dependencies.

The unchanged P0 fifteen sample points are retained. An additional conservative
Bernstein bound rejects curved maps whose determinant/scale cannot be bounded.
Thickness rays intersect the actual quadratic boundary, never just its corners.
"""
import itertools
import math

EDGES = ((0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3))
GRADIENTS = ((-1, -1, -1), (1, 0, 0), (0, 1, 0), (0, 0, 1))
FACES = ((0, 1, 2, 4, 5, 6), (0, 1, 3, 4, 8, 7), (1, 2, 3, 5, 9, 8), (0, 2, 3, 6, 9, 7))


def freecad_to_ccx_nodes(nodes):
    # Exact pinned FreeCAD writeABAQUS/getccxVolumesByFace convention, not a
    # sign-dependent repair: src/Mod/Fem/App/FemMesh.cpp at
    # 145529fe741292ff0b3977a01195bf0247425794, tetra10 order map.
    if len(nodes) != 10 or len(set(nodes)) != 10:
        raise ValueError('MESH_C3D10_REQUIRED')
    return [nodes[i] for i in (1, 0, 2, 3, 4, 6, 5, 8, 7, 9)]


def dot(a, b): return sum(x * y for x, y in zip(a, b))
def sub(a, b): return [x - y for x, y in zip(a, b)]
def cross(a, b): return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
def norm(a): return math.sqrt(dot(a, a))
def det(a): return dot(a[0], cross(a[1], a[2]))


def _jacobian(points, barycentric):
    derivatives = [[(4 * barycentric[i] - 1) * x for x in GRADIENTS[i]] for i in range(4)]
    derivatives.extend([[4 * (barycentric[i] * GRADIENTS[j][k] + barycentric[j] * GRADIENTS[i][k]) for k in range(3)] for i, j in EDGES])
    # Columns, translated first to avoid cancellation at a large placement.
    local = [sub(point, points[0]) for point in points]
    return [[sum(local[n][axis] * derivatives[n][k] for n in range(10)) for axis in range(3)] for k in range(3)]


def _bernstein_bound(matrices, required, depth=0):
    grouped, magnitudes = {}, {}
    for indices in itertools.product(range(4), repeat=3):
        key = tuple(sorted(indices))
        columns = [matrices[indices[column]][column] for column in range(3)]
        terms = [columns[0][0]*columns[1][1]*columns[2][2], columns[0][1]*columns[1][2]*columns[2][0], columns[0][2]*columns[1][0]*columns[2][1], -columns[0][2]*columns[1][1]*columns[2][0], -columns[0][1]*columns[1][0]*columns[2][2], -columns[0][0]*columns[1][2]*columns[2][1]]
        grouped.setdefault(key, []).append(math.fsum(terms))
        magnitudes.setdefault(key, []).append(math.fsum(abs(value) for value in terms))
    # Fixed roundoff margin, well above double precision error in these small
    # sums. This is a numerical bound, not exact arithmetic certification.
    lower = min((math.fsum(values) - 1e-12 * math.fsum(magnitudes[key])) / len(values) for key, values in grouped.items())
    scale = math.prod(max(norm(matrix[column]) for matrix in matrices) for column in range(3))
    if scale <= 0 or not math.isfinite(scale): raise ValueError('MESH_JACOBIAN_INVALID')
    bound = lower / scale
    if lower > 0 and bound >= required: return bound, depth
    if depth >= 5: raise ValueError('MESH_JACOBIAN_BOUND_INCONCLUSIVE')
    mids = {(i, j): [[(a+b)/2 for a, b in zip(matrices[i][k], matrices[j][k])] for k in range(3)] for i, j in EDGES}
    a, b, c, d = matrices
    ab, bc, ac, ad, bd, cd = [mids[edge] for edge in EDGES]
    children = ((a, ab, ac, ad), (ab, b, bc, bd), (ac, bc, c, cd), (ad, bd, cd, d), (ab, ac, ad, cd), (ab, ac, bc, cd), (ab, ad, bd, cd), (ab, bc, bd, cd))
    results = [_bernstein_bound(child, required, depth+1) for child in children]
    return min(value[0] for value in results), max(value[1] for value in results)


def tet10_quality(points, minimum_scaled):
    if len(points) != 10 or any(len(point) != 3 or any(not math.isfinite(value) for value in point) for point in points) or not 0 < minimum_scaled <= 1:
        raise ValueError('MESH_C3D10_REQUIRED')
    vertices = [tuple(1 if i == j else 0 for j in range(4)) for i in range(4)]
    samples = [*vertices, (.25,) * 4]
    samples.extend(tuple(.5 if k in (i, j) else 0 for k in range(4)) for i, j in EDGES)
    samples.extend(tuple(.5854101966249685 if j == i else .1381966011250105 for j in range(4)) for i in range(4))
    determinants, scaled = [], []
    for sample in samples:
        matrix = _jacobian(points, sample)
        determinant = det(matrix)
        scale = math.prod(norm(column) for column in matrix)
        value = determinant / scale if scale else 0
        if determinant <= 0 or value < minimum_scaled or not math.isfinite(value):
            raise ValueError('MESH_NONPOSITIVE_OR_POOR_JACOBIAN')
        determinants.append(determinant)
        scaled.append(value)
    lower, depth = _bernstein_bound([_jacobian(points, vertex) for vertex in vertices], minimum_scaled)
    return {'min_det_mm3': min(determinants), 'min_scaled_jacobian': min(scaled), 'sample_points': len(samples), 'bernstein_scaled_lower_bound': lower, 'bound_subdivision_depth': depth}


def _poly_add(a, b, factor=1):
    return [(a[i] if i < len(a) else 0) + factor*(b[i] if i < len(b) else 0) for i in range(max(len(a), len(b)))]


def _poly_mul(a, b):
    out = [0.] * (len(a)+len(b)-1)
    for i, x in enumerate(a):
        for j, y in enumerate(b): out[i+j] += x*y
    return out


def _evaluate(coefficients, x):
    result = 0.
    for coefficient in reversed(coefficients): result = result*x+coefficient
    return result


def _roots_unit(coefficients):
    scale = max(abs(value) for value in coefficients)
    if scale == 0: raise ValueError('MESH_RAY_DEGENERATE')
    values = [value/scale for value in coefficients]
    while len(values) > 1 and abs(values[-1]) < 1e-14: values.pop()
    if len(values) == 1: return []
    if len(values) == 2:
        root = -values[0]/values[1]
        return [max(0., min(1., root))] if -1e-10 <= root <= 1+1e-10 else []
    turning = _roots_unit([i*values[i] for i in range(1, len(values))])
    boundaries = sorted(set([0., *turning, 1.]))
    roots = [point for point in boundaries if abs(_evaluate(values, point)) < 1e-11]
    for low, high in zip(boundaries, boundaries[1:]):
        first, last = _evaluate(values, low), _evaluate(values, high)
        if first*last >= 0: continue
        for _ in range(60):
            middle = (low+high)/2
            value = _evaluate(values, middle)
            if first*value <= 0: high = middle
            else: low, first = middle, value
        roots.append((low+high)/2)
    return sorted(set(round(root, 13) for root in roots))


def _tri_coefficients(points):
    p0, p1, p2, p3, p4, p5 = points
    return [p0, [-3*p0[k]-p1[k]+4*p3[k] for k in range(3)], [-3*p0[k]-p2[k]+4*p5[k] for k in range(3)], [2*p0[k]+2*p1[k]-4*p3[k] for k in range(3)], [4*(p0[k]-p3[k]+p4[k]-p5[k]) for k in range(3)], [2*p0[k]+2*p2[k]-4*p5[k] for k in range(3)]]


def _face_hits(points, origin, direction):
    points = [sub(point, origin) for point in points]
    coefficients = _tri_coefficients(points)
    helper = [0., 0., 0.]
    helper[min(range(3), key=lambda i: abs(direction[i]))] = 1
    first = cross(direction, helper)
    first = [value/norm(first) for value in first]
    second = cross(direction, first)
    projected = [[dot(coefficient, axis) for coefficient in coefficients] for axis in (first, second)]
    # Each projected equation is a*u^2 + b(v)*u + c(v).
    f, g = projected
    a, b, c = [f[3]], [f[1], f[4]], [f[0], f[2], f[5]]
    d, e, h = [g[3]], [g[1], g[4]], [g[0], g[2], g[5]]
    linear = abs(a[0])+abs(d[0]) < 1e-13 * max(1., *(abs(value) for row in projected for value in row))
    A = _poly_add(_poly_mul(a, e), _poly_mul(b, d), -1)
    B = _poly_add(_poly_mul(a, h), _poly_mul(c, d), -1)
    C = _poly_add(_poly_mul(b, h), _poly_mul(c, e), -1)
    resultant = C if linear else _poly_add(_poly_mul(B, B), _poly_mul(A, C), -1)
    if max(abs(value) for value in resultant) < 1e-24:
        # A ruled curved face parallel to the ray can lose u completely in
        # projection. The zero resultant then says nothing about intersection.
        # Resolve the remaining two quadratics in v; a common root is an
        # ambiguous tangent/ruling and remains a failure, not an empty hit.
        projected_scale = max(1., *(abs(value) for row in projected for value in row))
        if max(abs(value) for value in [*a, *b, *d, *e]) <= 1e-13*projected_scale:
            stronger, other = (c, h) if max(map(abs, c)) >= max(map(abs, h)) else (h, c)
            if max(map(abs, stronger)) == 0: raise ValueError('MESH_RAY_DEGENERATE')
            if not any(abs(_evaluate(other, root)) <= 1e-9*projected_scale for root in _roots_unit(stronger)):
                return []
            raise ValueError('MESH_RAY_TANGENT')
        normal = cross(sub(points[1], points[0]), sub(points[2], points[0]))
        if norm(normal) and all(abs(dot(sub(point, points[0]), normal)) <= 1e-10*norm(normal) for point in points) and abs(dot(direction, normal)) < 1e-12*norm(normal) and abs(dot(points[0], normal)) > 1e-10*norm(normal):
            return []
        raise ValueError('MESH_RAY_DEGENERATE')
    hits = []
    scale = max(1., *(norm(point) for point in points))
    for v in _roots_unit(resultant):
        if linear:
            bv, ev = _evaluate(b, v), _evaluate(e, v)
            if max(abs(bv), abs(ev)) < 1e-12: raise ValueError('MESH_RAY_DEGENERATE')
            candidates = [-_evaluate(c, v)/bv] if abs(bv) >= abs(ev) else [-_evaluate(h, v)/ev]
        elif abs(_evaluate(A, v)) > 1e-12*scale**2:
            candidates = [-_evaluate(B, v)/_evaluate(A, v)]
        else:
            # Exceptional elimination root: solve the stronger remaining
            # quadratic, then independently check both original equations.
            row = f if abs(f[3]) >= abs(g[3]) else g
            aa, bb, cc = row[3], row[1]+row[4]*v, row[0]+row[2]*v+row[5]*v*v
            discriminant = bb*bb-4*aa*cc
            if abs(aa) < 1e-14 or discriminant < 0: continue
            candidates = [(-bb+sign*math.sqrt(discriminant))/(2*aa) for sign in (-1, 1)]
        for u in candidates:
            if u < -1e-9 or v < -1e-9 or u+v > 1+1e-9: continue
            weights = [1, u, v, u*u, u*v, v*v]
            point = [sum(coefficient[k]*weight for coefficient, weight in zip(coefficients, weights)) for k in range(3)]
            if max(abs(dot(point, axis)) for axis in (first, second)) > 1e-8*scale:
                raise ValueError('MESH_RAY_ROOT_INCONCLUSIVE')
            tangent_u = [coefficients[1][k]+2*u*coefficients[3][k]+v*coefficients[4][k] for k in range(3)]
            tangent_v = [coefficients[2][k]+u*coefficients[4][k]+2*v*coefficients[5][k] for k in range(3)]
            normal = cross(tangent_u, tangent_v)
            if abs(dot(normal, direction)) <= 1e-9*norm(normal):
                raise ValueError('MESH_RAY_TANGENT')
            hits.append(dot(point, direction))
    return hits


def tetra_ray_intervals(points, start, end):
    length = norm(sub(end, start))
    if length <= 0: raise ValueError('MESH_RAY_ZERO_LENGTH')
    direction = [value/length for value in sub(end, start)]
    hits = sorted(value for face in FACES for value in _face_hits([points[i] for i in face], start, direction))
    unique = []
    for value in hits:
        if not unique or abs(value-unique[-1]) > 1e-8*max(1., length): unique.append(value)
    if len(unique) % 2: raise ValueError('MESH_RAY_BOUNDARY_INCONCLUSIVE')
    # A regular, positively bounded T10 map has an oriented closed boundary.
    # Transverse intersections alternate inside/outside along the full line.
    return [(max(0., low), min(length, high)) for low, high in zip(unique[::2], unique[1::2]) if min(length, high)-max(0., low) > 1e-8]


def ray_coverage(intervals):
    merged = []
    for low, high in sorted(intervals):
        if high <= low: raise ValueError('MESH_RAY_INTERVAL_INVALID')
        if merged and low <= merged[-1][1]+1e-8:
            merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
        else: merged.append((low, high))
    count = 0
    for low, high in merged:
        cursor = low
        while cursor < high-1e-8:
            end = max(b for a, b in intervals if a <= cursor+1e-8)
            if end <= cursor+1e-8: raise ValueError('MESH_RAY_NONADVANCING')
            count, cursor = count+1, end
    return {'covered_mm': sum(high-low for low, high in merged), 'elements': count}
