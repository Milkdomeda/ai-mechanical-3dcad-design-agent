"""Fixed multi-level C3D10 policy and explicit quality/budget gates."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
from importlib.resources import files
import json
import math
import re

from .analysis_types import AnalysisError
from .analysis_units import canonical_decimal, validate_normalized_quantity


def numerical_policy():
    data = files('mechanical_design_agent').joinpath('resources/analysis/policies/native-solid-static-v1.json').read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def _quantity(value, unit, *, positive=False):
    if type(value) is not dict or value.get('unit') != unit:
        raise AnalysisError('MESH_POLICY_INVALID', 'mesh quantities require explicit normalized units')
    validate_normalized_quantity(value)
    result = Decimal(value['value'])
    if not math.isfinite(float(result)) or (positive and (result <= 0 or float(result) <= 0)):
        raise AnalysisError('MESH_POLICY_INVALID', 'mesh quantities must be finite and positive where required')
    return result


def plan_mesh_levels(spec):
    policy, policy_sha = numerical_policy()
    mesh, budget = spec.get('mesh'), spec.get('budget')
    if type(mesh) is not dict or type(budget) is not dict or mesh.get('element_type') != 'C3D10':
        raise AnalysisError('MESH_POLICY_UNRESOLVED', 'declare the C3D10 mesh strategy and execution budget')
    size = _quantity(mesh['initial_size'], 'mm', positive=True)
    ratio = _quantity(mesh['refinement_ratio'], '1', positive=True)
    minimum = _quantity(mesh['minimum_scaled_jacobian'], '1', positive=True)
    if ratio >= 1 or minimum > 1 or type(mesh['levels']) is not int or not policy['minimum_levels'] <= mesh['levels'] <= policy['maximum_levels'] or type(mesh['minimum_thickness_elements']) is not int or mesh['minimum_thickness_elements'] < policy['thickness']['minimum_elements']:
        raise AnalysisError('MESH_POLICY_INVALID', 'mesh refinement, quality and thickness limits must meet the fixed policy')
    rays = mesh.get('critical_rays', [])
    if len(rays) < 3:
        raise AnalysisError('MESH_CRITICAL_RAYS_REQUIRED', 'declare at least three evidence-backed full-thickness rays before review')
    identifiers = []
    for ray in rays:
        identifiers.append(ray['id'])
        start = [_quantity(value, 'mm') for value in ray['start']]
        end = [_quantity(value, 'mm') for value in ray['end']]
        atol = _quantity(ray['coverage_atol'], 'mm', positive=True)
        length = math.sqrt(sum(float(b-a)**2 for a, b in zip(start, end)))
        if len(start) != 3 or len(end) != 3 or length <= 0 or float(atol) >= length/2 or not ray['evidence'].strip():
            raise AnalysisError('MESH_CRITICAL_RAY_INVALID', 'each critical ray needs nonzero thickness, evidence and an explicit coverage tolerance')
    if len(identifiers) != len(set(identifiers)):
        raise AnalysisError('MESH_CRITICAL_RAY_INVALID', 'critical ray identities must be unique')
    limits = {'wall_seconds': 86400, 'stage_seconds': 86400, 'max_nodes': 2000000, 'max_elements': 10000000, 'max_disk_bytes': 2**40, 'max_attempts': 3, 'max_memory_mb': 1048576}
    if any(type(budget.get(key)) is not int or not 1 <= budget[key] <= maximum for key, maximum in limits.items()) or budget.get('threads') != policy['threads'] or type(budget.get('threads')) is not int or budget['stage_seconds'] > budget['wall_seconds']:
        raise AnalysisError('ANALYSIS_BUDGET_INVALID', 'declare positive bounded resource budgets and one deterministic native thread')
    if budget.get('memory_mode') != 'observed_only':
        raise AnalysisError('ANALYSIS_MEMORY_MODE_UNSUPPORTED', 'this profile monitors memory and stops on an observed excess; it does not claim a hard OS memory reservation')
    source = spec.get('source')
    if not source or not source.get('geometry_sha256') or not source.get('regions_sha256'):
        raise AnalysisError('MESH_GEOMETRY_UNBOUND', 'mesh policy requires exact approved geometry and region hashes')
    local_sizes = mesh.get('local_sizes', [])
    if len({item['region_id'] for item in local_sizes}) != len(local_sizes):
        raise AnalysisError('MESH_LOCAL_SIZE_INVALID', 'each local region size must be unique')
    for item in local_sizes:
        if _quantity(item['size'], 'mm', positive=True) > size or not item['evidence'].strip():
            raise AnalysisError('MESH_LOCAL_SIZE_INVALID', 'local refinement must be no larger than the global size and have evidence')
    levels = []
    with localcontext() as context:
        context.prec = 256
        for index in range(mesh['levels']):
            factor = ratio**index
            levels.append({'schema_version': 'AnalysisMeshLevel/v1', 'level': index, 'size': {'value': canonical_decimal(str(size*factor)), 'unit': 'mm'}, 'element_type': 'C3D10', 'threads': 1, 'geometry_sha256': source['geometry_sha256'], 'region_bindings_sha256': source['regions_sha256'], 'policy_sha256': policy_sha, 'minimum_scaled_jacobian': deepcopy(mesh['minimum_scaled_jacobian']), 'minimum_thickness_elements': mesh['minimum_thickness_elements'], 'critical_rays': deepcopy(rays), 'local_sizes': [{**deepcopy(item), 'size': {'value': canonical_decimal(str(Decimal(item['size']['value'])*factor)), 'unit': 'mm'}} for item in local_sizes], 'budget': deepcopy(budget)})
    return levels


def validate_mesh(receipt, policy):
    """Do not infer physical resolution from total counts or a success flag."""
    if receipt.get('element_type') != 'C3D10' or receipt.get('node_count', 0) <= 0 or receipt.get('element_count', 0) <= 0:
        raise AnalysisError('MESH_EMPTY_OR_WRONG_ORDER', 'a nonempty ten-node tetrahedral mesh is required')
    budget = policy['budget']
    if receipt['node_count'] > budget['max_nodes'] or receipt['element_count'] > budget['max_elements'] or receipt['disk_bytes'] > budget['max_disk_bytes']:
        return {'status': 'inconclusive', 'code': 'MESH_BUDGET_EXCEEDED'}
    quality = receipt['quality']
    if quality['checked_elements'] != receipt['element_count'] or quality['sample_points_per_element'] != 15 or quality['min_det_mm3'] <= 0 or quality['min_scaled_jacobian'] < float(policy['minimum_scaled_jacobian']['value']) or quality['bernstein_scaled_lower_bound'] < float(policy['minimum_scaled_jacobian']['value']) or not all(math.isfinite(quality[key]) for key in ('min_det_mm3', 'min_scaled_jacobian', 'bernstein_scaled_lower_bound')):
        raise AnalysisError('MESH_QUALITY_FAILED', 'every quadratic map must pass the frozen sampled and bounded Jacobian checks')
    rays = quality['thickness_rays']
    expected = policy['critical_rays']
    if len(rays) != len(expected) or {ray['id'] for ray in rays} != {ray['id'] for ray in expected}:
        raise AnalysisError('MESH_THICKNESS_UNPROVEN', 'every declared critical ray needs actual quadratic-boundary coverage evidence')
    by_id = {ray['id']: ray for ray in rays}
    for ray in expected:
        actual = by_id[ray['id']]
        length = math.sqrt(sum((float(b['value'])-float(a['value']))**2 for a, b in zip(ray['start'], ray['end'])))
        if not math.isfinite(actual['covered_mm']) or abs(actual['covered_mm']-length) > float(ray['coverage_atol']['value']) or actual['elements'] < policy['minimum_thickness_elements']:
            raise AnalysisError('MESH_THICKNESS_UNRESOLVED', 'a full critical thickness lacks the approved minimum element resolution')
    return {'status': 'passed', 'code': None}


def generate_mesh(geometry, bindings, level, settings, output_dir, *, transaction=None):
    from .analysis_native import run_mesh_phase
    return run_mesh_phase(geometry, bindings, level, settings, output_dir, transaction=transaction)


def validate_region_topology(data, mapping):
    """A physical surface needs complete six-node exterior element faces."""
    from .resources.freecad.analysis_mesh_math import FACES
    elements = {row[0]: row[1:] for row in data['elements']}
    owners = {}
    for element, ids in elements.items():
        for face, local in enumerate(FACES, 1):
            key = tuple(sorted(ids[i] for i in local))
            owners.setdefault(key, []).append((element,face))
    if any(len(value) > 2 for value in owners.values()):
        raise AnalysisError('MESH_NONMANIFOLD_BOUNDARY', 'an element face has more than two owners')
    for region in mapping['regions']:
        nodes = set()
        for element, face in region['element_faces']:
            key = tuple(sorted(elements[element][i] for i in FACES[face-1]))
            if owners[key] != [(element,face)]:
                raise AnalysisError('REGION_MESH_MAPPING_INVALID', 'physical regions cannot contain interior element faces')
            nodes.update(key)
        if nodes != set(region['node_ids']):
            raise AnalysisError('REGION_MESH_MAPPING_INVALID', 'physical node and full T6 exterior-face inventories disagree')


def validate_native_mesh_recipe(text):
    """Check effective fixed options before the native generation command."""
    commands = re.sub(r'//[^\n]*', '', text)
    starts = list(re.finditer(r'\bMesh\s+3\s*;', commands))
    if len(starts) != 1:
        raise AnalysisError('MESH_NATIVE_RECIPE_INVALID', 'exactly one native 3D mesh generation command is required')
    before = commands[:starts[0].start()]
    expected = {'General.NumThreads': '1', 'Mesh.ElementOrder': '2', 'Mesh.SecondOrderLinear': '0', 'Mesh.Algorithm': '5', 'Mesh.Algorithm3D': '1', 'Mesh.RandomSeed': '1'}
    for name, value in expected.items():
        matches = re.findall(r'(?<![\w.])'+re.escape(name)+r'\s*=\s*([0-9.+eE-]+)\s*;', before)
        if not matches or Decimal(matches[-1]) != Decimal(value):
            raise AnalysisError('MESH_NATIVE_RECIPE_INVALID', 'fixed mesh options must take effect before generation', details={'option': name})
    return {'status': 'passed', 'options': expected}
