"""Conserved static loads in N-mm-s-tonne; no implicit physical inputs."""
from __future__ import annotations

from decimal import Decimal, localcontext

from .analysis_geometry import resolve_regions, validate_geometry_manifest, validate_region_bindings
from .analysis_protocol import canonical_record, parse_record, record_sha256
from .analysis_types import AnalysisError
from .analysis_units import canonical_decimal


def _value(quantity):
    return Decimal(quantity['value'])


def _vector(quantities):
    return [_value(item) for item in quantities]


def _strings(values):
    return [canonical_decimal(format(value, '.24g')) for value in values]


def _norm(vector):
    return sum(value * value for value in vector).sqrt()


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _spec(value):
    return parse_record(canonical_record(value, schema_name='spec').decode(), schema_name='spec')


def validate_materials(spec):
    selected = _spec(spec)
    material = selected['material']
    if material is None:
        raise AnalysisError('MATERIAL_UNRESOLVED', 'an explicit linear elastic material is required')
    if not material['evidence'] or any(not value.strip() for value in material['evidence']):
        raise AnalysisError('MATERIAL_EVIDENCE_MISSING', 'material properties require recorded evidence or an explicitly identified screening assumption')
    if _value(material['youngs_modulus']) <= 0 or not Decimal(-1) < _value(material['poisson_ratio']) < Decimal('.5'):
        raise AnalysisError('MATERIAL_ISOTROPIC_INVALID', 'isotropic elasticity requires E > 0 and -1 < Poisson ratio < 0.5')
    if material['density'] is not None and _value(material['density']) <= 0:
        raise AnalysisError('MATERIAL_DENSITY_INVALID', 'declared density must be positive')
    return {'status': 'defined', 'material': material, 'model': 'homogeneous_isotropic_linear_elastic', 'stiffness_inferred_from_mass_or_infill': False}


def _mass(spec, geometry):
    basis = spec['frame_mass']
    if basis is None:
        raise AnalysisError('FRAME_MASS_UNRESOLVED', 'self weight requires approved measured mass or an explicit density-volume basis')
    if any(not item.strip() for item in basis['evidence']):
        raise AnalysisError('FRAME_MASS_UNRESOLVED', 'mass evidence must not be blank')
    volume = _value(geometry['volume'])
    if basis['method'] == 'measured':
        mass = _value(basis['value'])
    else:
        density = spec['material']['density']
        if density is None:
            raise AnalysisError('FRAME_MASS_UNRESOLVED', 'density-volume mass requires explicit density')
        mass = _value(density) * volume
    if mass <= 0:
        raise AnalysisError('FRAME_MASS_INVALID', 'frame mass must be positive')
    return {'method': basis['method'], 'mass_tonne': _strings([mass])[0], 'equivalent_density_tonne_mm3': _strings([mass / volume])[0], 'distribution': 'uniform_volume', 'evidence': basis['evidence'], 'limitation': 'Uniform volume distribution calibrates total mass only; it does not establish local printed density or stiffness.'}


def build_load_ledger(spec, geometry):
    with localcontext() as context:
        context.prec = 256
        return _build_load_ledger(spec, geometry)


def _build_load_ledger(spec, geometry):
    selected = _spec(spec)
    material = validate_materials(selected)
    if type(geometry) is not dict or set(geometry) != {'geometry', 'regions'}:
        raise AnalysisError('LOAD_GEOMETRY_INVALID', 'load ledger requires an exact geometry manifest and its region bindings')
    manifest = validate_geometry_manifest(geometry['geometry'])
    bindings = validate_region_bindings(geometry['regions'])
    source = selected['source']
    if source is None or source['sha256'] != manifest['source_sha256'] or source['geometry_sha256'] != manifest['geometry_sha256'] or source['regions_sha256'] != bindings['bindings_sha256'] or bindings['source_sha256'] != manifest['source_sha256'] or bindings['geometry_sha256'] != manifest['geometry_sha256'] or source['object_names'] != manifest['included_objects'] or source['excluded_object_names'] != manifest['excluded_objects']:
        raise AnalysisError('LOAD_GEOMETRY_INVALID', 'loads must reference the exact selected source, geometry and region revision')
    rebound = resolve_regions(manifest, [entry['selector'] for entry in bindings['bindings']])
    if rebound != bindings:
        raise AnalysisError('LOAD_REGION_INVALID', 'stored region areas and load centers differ from the current geometry inventory')
    validation = selected['validation']
    if validation is None:
        raise AnalysisError('LOAD_MOMENT_ORIGIN_MISSING', 'declare the moment reference origin before generating the load ledger')
    origin = _vector(validation['moment_origin'])
    regions = {item['region_id']: item for item in bindings['bindings']}
    faces = {item['index']: item for item in manifest['faces']}
    for region in regions.values():
        if any(index not in faces for index in region['face_indices']) or _value(region['area']) <= 0:
            raise AnalysisError('LOAD_REGION_INVALID', 'load regions require complete positive boundary faces')
    case_ids = [case['case_id'] for case in selected['cases']]
    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise AnalysisError('LOAD_CASE_INVALID', 'at least one uniquely identified case is required')
    has_gravity = any(load['type'] == 'gravity' for case in selected['cases'] for load in case['loads'])
    mass = _mass(selected, manifest) if has_gravity else None
    result = {'schema_version': 'AnalysisLoadLedger/v1', 'spec_sha256': record_sha256(selected, schema_name='spec'), 'source_sha256': manifest['source_sha256'], 'geometry_sha256': manifest['geometry_sha256'], 'bindings_sha256': bindings['bindings_sha256'], 'material': material, 'frame_mass': mass, 'moment_origin_mm': _strings(origin), 'criteria_status': 'criteria_defined' if selected['criteria'] else 'criteria_not_evaluated', 'cases': []}
    for case in selected['cases']:
        meaning = case['load_meaning']
        if meaning == 'unresolved':
            raise AnalysisError('LOAD_MEANING_UNRESOLVED', 'historical force magnitude cannot be used until its physical meaning is resolved')
        if any(name not in regions for name in case['fixed_regions'] + case['free_regions']):
            raise AnalysisError('LOAD_REGION_INVALID', 'support and free regions must be bound to the selected manufacturing solid')
        fixed_faces = {index for name in case['fixed_regions'] for index in regions[name]['face_indices']}
        free_faces = {index for name in case['free_regions'] for index in regions[name]['face_indices']}
        if fixed_faces & free_faces or (case['operating_condition'] == 'lifted' and not free_faces):
            raise AnalysisError('SUPPORT_CONTRACT_INVALID', 'a lifted case must explicitly leave its declared free regions unconstrained')
        load_ids = [load['id'] for load in case['loads']]
        gravity = [load for load in case['loads'] if load['type'] == 'gravity']
        if len(load_ids) != len(set(load_ids)) or len(gravity) > 1:
            raise AnalysisError('LOAD_DUPLICATE', 'load identifiers and body gravity must not be duplicated')
        if any(_value(load['factor']) <= 0 for load in case['loads']):
            raise AnalysisError('LOAD_FACTOR_INVALID', 'each load requires an explicit positive equivalent-static factor')
        for load in case['loads']:
            if load['type'] != 'total_force':
                continue
            basis = load['basis']
            if any(not evidence.strip() for evidence in basis['evidence']):
                raise AnalysisError('LOAD_EVIDENCE_MISSING', 'each resultant requires a stated force or a mass-acceleration source')
            if basis['type'] == 'mass_acceleration':
                mass_value = _value(basis['mass'])
                expected = [mass_value * value for value in _vector(basis['acceleration'])]
                if mass_value <= 0 or expected != _vector(load['components']):
                    raise AnalysisError('LOAD_MASS_FORCE_MISMATCH', 'nominal payload force must equal its recorded positive mass times acceleration before equivalent-static factors')
        body = [Decimal(0)] * 3 if not gravity else [Decimal(mass['mass_tonne']) * value for value in _vector(gravity[0]['components'])]
        scale = Decimal(1)
        if meaning != 'direct_applied_loads':
            forces = [load for load in case['loads'] if load['type'] == 'total_force']
            if not forces or len(forces) + len(gravity) != len(case['loads']) or len(gravity) != 1 or case['screening_force'] is None or _value(case['screening_force']) <= 0:
                raise AnalysisError('LOAD_SCREENING_INVALID', 'screening interpretation requires positive reference force, parallel payload resultants and exactly one explicit frame gravity')
            first = _vector(forces[0]['components']); norm = _norm(first)
            if norm == 0:
                raise AnalysisError('LOAD_SCREENING_INVALID', 'payload allocation direction cannot be zero')
            direction = [value / norm for value in first]
            magnitudes = []
            for force in forces:
                vector = _vector(force['components']); magnitude = _norm(vector)
                if magnitude == 0 or any(abs(value / magnitude - target) > Decimal('1e-24') for value, target in zip(vector, direction)):
                    raise AnalysisError('LOAD_SCREENING_INVALID', 'screening payload allocations must share one global direction')
                magnitudes.append(magnitude)
            body_magnitude = _norm(body)
            if body_magnitude == 0 or any(abs(value / body_magnitude - target) > Decimal('1e-24') for value, target in zip(body, direction)):
                raise AnalysisError('LOAD_SCREENING_INVALID', 'frame weight and screening direction must agree')
            remaining = _value(case['screening_force']) - (body_magnitude if meaning == 'total_including_self_weight' else Decimal(0))
            if remaining < 0:
                raise AnalysisError('LOAD_NEGATIVE_REMAINDER', 'reference total force is smaller than the included frame self weight')
            scale = remaining / sum(magnitudes)
        elif case['screening_force'] is not None:
            raise AnalysisError('LOAD_SCREENING_INVALID', 'a reference screening force requires an explicit total or payload interpretation')
        total, total_moment, objects = [Decimal(0)] * 3, [Decimal(0)] * 3, []
        for load in case['loads']:
            factor = _value(load['factor'])
            native = {'type': load['type'], 'id': load['id'], 'factor': _strings([factor])[0]}
            if load['type'] == 'gravity':
                acceleration = [value * factor for value in _vector(load['components'])]
                force = [Decimal(mass['mass_tonne']) * value for value in acceleration]
                center = _vector(manifest['center_of_mass'])
                moment = _cross([value - base for value, base in zip(center, origin)], force)
                native.update(components_mm_s2=_strings(acceleration), resultant_N=_strings(force), equivalent_density_tonne_mm3=mass['equivalent_density_tonne_mm3'], center_mm=_strings(center))
            else:
                if load['region_id'] not in regions:
                    raise AnalysisError('LOAD_REGION_INVALID', 'surface load has an unknown or excluded region')
                region = regions[load['region_id']]
                if set(region['face_indices']) & fixed_faces:
                    raise AnalysisError('SUPPORT_LOAD_OVERLAP', 'a surface load must not overlap a fully fixed region')
                center = _vector(region['centroid'])
                native.update(region_id=load['region_id'], face_indices=region['face_indices'], center_mm=_strings(center))
                if load['type'] == 'total_force':
                    force = [value * factor * scale for value in _vector(load['components'])]
                    moment = _cross([value - base for value, base in zip(center, origin)], force)
                    native.update(components_N=_strings(force), distribution='native_uniform_surface', basis=load['basis'])
                else:
                    pressure = _value(load['value']) * factor
                    if pressure <= 0:
                        raise AnalysisError('LOAD_PRESSURE_INVALID', 'pressure magnitude must be positive; use the explicit reversed flag for direction')
                    force, moment = [Decimal(0)] * 3, [Decimal(0)] * 3
                    for index in region['face_indices']:
                        face = faces[index]
                        if face['surface_type'] != 'plane':
                            raise AnalysisError('CURVED_PRESSURE_NOT_VALIDATED', 'curved normal pressure requires independently validated surface integration; use an approved global total force where physically applicable')
                        component = [value * _value(face['area']) * pressure * (1 if load['reversed'] else -1) for value in _vector(face['normal'])]
                        part_moment = _cross([value - base for value, base in zip(_vector(face['centroid']), origin)], component)
                        force = [a + b for a, b in zip(force, component)]
                        moment = [a + b for a, b in zip(moment, part_moment)]
                    native.update(value_MPa=_strings([pressure])[0], reversed=load['reversed'], resultant_N=_strings(force))
            native['moment_Nmm'] = _strings(moment)
            total = [a + b for a, b in zip(total, force)]
            total_moment = [a + b for a, b in zip(total_moment, moment)]
            objects.append(native)
        result['cases'].append({'case_id': case['case_id'], 'load_meaning': meaning, 'operating_condition': case['operating_condition'], 'fixed_regions': case['fixed_regions'], 'free_regions': case['free_regions'], 'fixed_face_indices': sorted(fixed_faces), 'native_objects': objects, 'total_force_N': _strings(total), 'total_moment_Nmm': _strings(total_moment)})
    return parse_record(canonical_record(result, schema_name='load-ledger').decode(), schema_name='load-ledger')
