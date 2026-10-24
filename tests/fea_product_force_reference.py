"""Independent qualification reference; no native R1 helper is imported.

Solve the six monomial interpolation conditions, then use a separately
constructed order-20 Gauss rule. This is test evidence, not a deck writer.
"""
from functools import lru_cache
import math


@lru_cache
def gauss20():
    roots = []
    for i in range(1, 21):
        z = math.cos(math.pi*(i-.25)/20.5)
        for _ in range(30):
            p, previous = 1., 0.
            for degree in range(1, 21):
                p, previous = ((2*degree-1)*z*p-(degree-1)*previous)/degree, p
            derivative = 20*(z*p-previous)/(z*z-1)
            updated = z-p/derivative
            if abs(updated-z) < 2e-16:
                z = updated
                break
            z = updated
        else: raise AssertionError('reference quadrature did not converge')
        # Recompute the derivative at the converged root.
        p, previous = 1., 0.
        for degree in range(1, 21):
            p, previous = ((2*degree-1)*z*p-(degree-1)*previous)/degree, p
        derivative = 20*(z*p-previous)/(z*z-1)
        roots.append(((z+1)/2, 1/((1-z*z)*derivative*derivative)))
    return tuple(sorted(roots))


def _features(r, s): return [1., r, s, r*r, r*s, s*s]


@lru_cache
def inverse_basis():
    locations = [(0,0),(1,0),(0,1),(.5,0),(.5,.5),(0,.5)]
    rows = [_features(*point) + [float(i == j) for j in range(6)] for i, point in enumerate(locations)]
    for i in range(6):
        pivot = max(range(i,6), key=lambda j: abs(rows[j][i]))
        rows[i], rows[pivot] = rows[pivot], rows[i]
        scale = rows[i][i]
        assert scale != 0
        rows[i] = [value/scale for value in rows[i]]
        for j in range(6):
            if j == i: continue
            scale = rows[j][i]
            rows[j] = [a-scale*b for a,b in zip(rows[j], rows[i])]
    return tuple(tuple(row[6:]) for row in rows)


def surface_weights(points):
    points = [list(map(float, point)) for point in points]
    inverse = inverse_basis()
    coefficients = [[sum(inverse[i][n]*(points[n][a]-points[0][a]) for n in range(6)) for a in range(3)] for i in range(6)]
    weights = [[] for _ in range(6)]
    areas = []
    for r, wr in gauss20():
        for t, wt in gauss20():
            s = (1-r)*t
            dr, ds = [0,1,0,2*r,s,0], [0,0,1,0,r,2*s]
            a,b = [[sum(vector[i]*coefficients[i][axis] for i in range(6)) for axis in range(3)] for vector in (dr,ds)]
            cross = [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
            measure = math.sqrt(sum(value*value for value in cross))*wr*wt*(1-r)
            assert math.isfinite(measure) and measure > 0
            areas.append(measure)
            values = _features(r,s)
            for n in range(6): weights[n].append(sum(values[i]*inverse[i][n] for i in range(6))*measure)
    return [math.fsum(row) for row in weights], math.fsum(areas)


def compare_force(data, faces, declared):
    by_node, areas = {}, []
    for face in faces:
        weights, area = surface_weights([data['_nodes'][node] for node in face])
        areas.append(area)
        for node, value in zip(face, weights): by_node.setdefault(node, []).append(value)
    total_area = math.fsum(areas)
    expected = {(node,axis+1): math.fsum(values)/total_area*value for node, values in by_node.items() for axis, value in enumerate(declared)}
    inverse = {point: node for node, point in data['_nodes'].items()}
    actual = {(inverse[point], axis): float(value) for (point,axis),value in data['loads'].items()}
    return {'reference_area_mm2': total_area, 'max_nodal_force_error_N': max(abs(actual.get(key,0)-expected.get(key,0)) for key in set(actual)|set(expected)), 'compared_nodes': len(by_node), 'boundary_faces': len(faces), 'quadrature_order': 20}
