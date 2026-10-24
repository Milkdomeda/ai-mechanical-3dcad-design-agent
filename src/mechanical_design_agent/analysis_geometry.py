"""Source-bound geometry inspection and deterministic semantic face binding."""
from __future__ import annotations

from decimal import Decimal
import hashlib
from pathlib import Path

from .analysis_protocol import canonical_record, parse_record
from .analysis_types import AnalysisError, JsonObject
from .analysis_units import canonical_decimal


def _validated(value, name):
    return parse_record(canonical_record(value, schema_name=name).decode('utf-8'), schema_name=name)


def _number(quantity):
    return Decimal(quantity['value'])


def _quantity(value, unit):
    return {'value': canonical_decimal(str(value)), 'unit': unit}


def validate_geometry_request(request: JsonObject) -> JsonObject:
    result = _validated(request, 'geometry-request')
    if result['included_object'] in result['excluded_objects'] or _number(result['volume_tolerance']) < 0:
        raise AnalysisError('GEOMETRY_REQUEST_INVALID', 'included objects and numerical tolerances must be explicit and consistent')
    return result


def validate_geometry_manifest(manifest: JsonObject) -> JsonObject:
    result = _validated(manifest, 'geometry')
    if _number(result['volume']) <= 0 or _number(result['original_volume']) <= 0:
        raise AnalysisError('GEOMETRY_INVALID', 'a positive manufacturing solid is required')
    indices = [face['index'] for face in result['faces']]
    if sorted(indices) != list(range(1, len(indices) + 1)):
        raise AnalysisError('GEOMETRY_INVALID', 'face inventory must be complete and unique')
    if any(_number(face['area']) <= 0 for face in result['faces']):
        raise AnalysisError('GEOMETRY_INVALID', 'all boundary faces must have positive area')
    if result['geometry_sha256'] != result['artifacts']['brep']['sha256']:
        raise AnalysisError('GEOMETRY_INVALID', 'geometry digest and boundary representation disagree')
    if result['artifacts']['brep']['relative_path'] != 'geometry.brep' or result['artifacts']['fcstd']['relative_path'] != 'geometry.FCStd':
        raise AnalysisError('GEOMETRY_INVALID', 'fixed geometry artifact names are required')
    return result


def inspect_geometry(source: Path, request: JsonObject, settings: JsonObject, output_dir: Path) -> JsonObject:
    from .analysis_native import run_geometry_phase
    contract = validate_geometry_request(request)
    manifest = run_geometry_phase(Path(source), contract, settings, Path(output_dir))
    result = validate_geometry_manifest(manifest)
    if result['source_sha256'] != contract['source_sha256'] or result['included_objects'] != [contract['included_object']] or result['excluded_objects'] != contract['excluded_objects'] or result['partitions'] != contract['partitions']:
        raise AnalysisError('GEOMETRY_BINDING_CHANGED', 'native geometry does not match the requested source and object selection')
    if abs(_number(result['volume']) - _number(result['original_volume'])) > _number(contract['volume_tolerance']):
        raise AnalysisError('GEOMETRY_VOLUME_CHANGED', 'derived partitions changed the manufacturing volume beyond the declared numerical tolerance')
    return result


def resolve_regions(manifest: JsonObject, selectors: list[JsonObject]) -> JsonObject:
    geometry = validate_geometry_manifest(manifest)
    selected = _validated({'schema_version': 'AnalysisRegionSelectors/v1', 'selectors': selectors}, 'region-selectors')['selectors']
    ids = [item['region_id'] for item in selected]
    if len(ids) != len(set(ids)):
        raise AnalysisError('REGION_ID_DUPLICATE', 'region identifiers must be unique')
    bindings = []
    for selector in selected:
        if selector['object_name'] not in geometry['included_objects']:
            raise AnalysisError('REGION_OBJECT_EXCLUDED', 'region belongs to an excluded or unknown object')
        area, area_tolerance, tolerance = map(_number, (selector['area'], selector['area_tolerance'], selector['centroid_tolerance']))
        if area <= 0 or area_tolerance < 0 or tolerance < 0:
            raise AnalysisError('REGION_SELECTOR_INVALID', 'positive area and nonnegative numerical tolerances are required')
        center = [_number(v) for v in selector['centroid']]
        normal = None if selector['normal'] is None else [_number(v) for v in selector['normal']]
        # Normal is a direction contract; no angular tolerance is invented.
        # Exact axis-aligned directions are common. FreeCAD roundoff only is
        # bounded here, independently of engineering/manufacturing tolerances.
        if normal is not None and abs(sum(v * v for v in normal) - 1) > Decimal('1e-12'):
            raise AnalysisError('REGION_NORMAL_INVALID', 'region normal must be a unit direction')
        matches = []
        for face in geometry['faces']:
            if face['surface_type'] != selector['surface_type']:
                continue
            if abs(_number(face['area']) - area) > area_tolerance:
                continue
            delta = [_number(v) - target for v, target in zip(face['centroid'], center)]
            if sum(v * v for v in delta) > tolerance * tolerance:
                continue
            if normal is not None and any(abs(_number(v) - target) > Decimal('1e-12') for v, target in zip(face['normal'], normal)):
                continue
            matches.append(face)
        if len(matches) != selector['expected_faces']:
            code = 'REGION_AMBIGUOUS' if len(matches) > selector['expected_faces'] else 'REGION_NOT_FOUND'
            raise AnalysisError(code, 'semantic selector did not identify exactly the declared face count', details={'region_id': selector['region_id'], 'matched': len(matches)})
        total_area = sum(_number(face['area']) for face in matches)
        centroid = [sum(_number(face['centroid'][axis]) * _number(face['area']) for face in matches) / total_area for axis in range(3)]
        bindings.append(_validated({
            'schema_version': 'RegionBinding/v1', 'source_sha256': geometry['source_sha256'],
            'region_id': selector['region_id'], 'selector': selector,
            'face_indices': sorted(face['index'] for face in matches), 'geometry_sha256': geometry['geometry_sha256'],
            'area': _quantity(total_area, 'mm^2'), 'centroid': [_quantity(v, 'mm') for v in centroid],
        }, 'region'))
    bindings.sort(key=lambda item: item['region_id'])
    digest = hashlib.sha256(b''.join(canonical_record(item, schema_name='region') + b'\n' for item in bindings)).hexdigest()
    return _validated({'schema_version': 'AnalysisRegionBindings/v1', 'geometry_sha256': geometry['geometry_sha256'], 'source_sha256': geometry['source_sha256'], 'bindings': bindings, 'bindings_sha256': digest}, 'region-bindings')


def verify_region_mapping(bindings: JsonObject, mesh_regions: JsonObject) -> JsonObject:
    bound = validate_region_bindings(bindings)
    def invalid():
        raise AnalysisError('REGION_MESH_MAPPING_INVALID', 'mesh region mapping must be complete, nonempty and bound to the exact geometry')
    if type(mesh_regions) is not dict or set(mesh_regions) != {'geometry_sha256', 'regions'} or mesh_regions['geometry_sha256'] != bound['geometry_sha256'] or type(mesh_regions['regions']) is not list:
        invalid()
    inventory = {}
    for entry in mesh_regions['regions']:
        if type(entry) is not dict or set(entry) != {'region_id', 'face_indices', 'node_ids', 'element_faces'}:
            invalid()
        if type(entry['region_id']) is not str or entry['region_id'] in inventory:
            invalid()
        for key in ('node_ids', 'face_indices'):
            values = entry[key]
            if type(values) is not list or not values or any(type(v) is not int or v <= 0 for v in values) or len(values) != len(set(values)):
                invalid()
        pairs = entry['element_faces']
        if type(pairs) is not list or not pairs or any(type(pair) is not list or len(pair) != 2 or any(type(v) is not int or v <= 0 for v in pair) or pair[1] > 4 for pair in pairs):
            invalid()
        if len({tuple(pair) for pair in pairs}) != len(pairs):
            invalid()
        inventory[entry['region_id']] = entry
    if set(inventory) != {item['region_id'] for item in bound['bindings']}:
        invalid()
    for binding in bound['bindings']:
        if sorted(inventory[binding['region_id']]['face_indices']) != binding['face_indices']:
            invalid()
    return {'status': 'passed', 'geometry_sha256': bound['geometry_sha256'], 'bindings_sha256': bound['bindings_sha256']}


def validate_region_bindings(bindings: JsonObject) -> JsonObject:
    bound = _validated(bindings, 'region-bindings')
    entries = bound['bindings']
    identifiers = [item['region_id'] for item in entries]
    digest = hashlib.sha256(b''.join(canonical_record(item, schema_name='region') + b'\n' for item in entries)).hexdigest()
    if identifiers != sorted(set(identifiers)) or digest != bound['bindings_sha256'] or any(item['source_sha256'] != bound['source_sha256'] or item['geometry_sha256'] != bound['geometry_sha256'] for item in entries):
        raise AnalysisError('REGION_BINDING_INTEGRITY', 'region collection contains stale, duplicate or mixed source bindings')
    return bound
