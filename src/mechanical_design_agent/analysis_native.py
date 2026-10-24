"""Packaged fixed native phases, private W1 scratch and owned processes."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

from . import secure_fs
from .analysis_configuration import load_analysis_configuration
from .analysis_types import AnalysisError
from .fcstd_security import inspect_fcstd_bytes


def verify_settings(settings, *, checkpoint=None):
    """Recheck the complete isolated inventory without executing any source."""
    runtime = SimpleNamespace(environ={'MECH_DESIGN_FEA_CONFIG': settings['configuration_path']}, _manifest=lambda: SimpleNamespace(workspace=Path(settings['workspace'])))
    actual = load_analysis_configuration(runtime, checkpoint=checkpoint)
    if actual != settings:
        raise AnalysisError('FEA_RUNTIME_CHANGED', 'configured native runtime changed during analysis')


def _read(path):
    located = secure_fs.validate_managed_path(path, allow_missing_leaf=False)
    data = secure_fs.read_managed_file(located.path)
    if data.identity != located.identity or data.link_count != 1:
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'native file identity or link count changed')
    return data


def _native_path(path):
    # Qt/FreeCAD resource discovery cannot use the Windows extended prefix.
    value = str(path)
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


def _environment(home, scratch):
    env = {key: value for key, value in os.environ.items() if key in {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT'}}
    env.update(HOME=_native_path(home), USERPROFILE=_native_path(home), FREECAD_USER_HOME=_native_path(home), FREECAD_USER_DATA=_native_path(home), FREECAD_USER_TEMP=_native_path(scratch), TMPDIR=_native_path(scratch), TEMP=_native_path(scratch), TMP=_native_path(scratch), PATH=os.defpath, PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', QT_QPA_PLATFORM='offscreen')
    return env


def _with_transaction(settings, transaction, operation):
    from .analysis_native_transaction import NativeTransaction
    if transaction is None:
        with NativeTransaction(settings) as owned:
            return operation(owned)
    if transaction.settings != settings:
        raise AnalysisError('FEA_RUNTIME_CHANGED', 'native transaction belongs to different pinned settings')
    return operation(transaction)


def _unchanged(path, original):
    actual = _read(path)
    if actual.identity != original.identity or actual.sha256 != original.sha256:
        raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'captured source changed during the fixed native phase')


def run_geometry_phase(source, contract, settings, output_dir, *, timeout_seconds=120, transaction=None):
    from .analysis_geometry import validate_geometry_manifest, validate_geometry_request
    contract = validate_geometry_request(contract)
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 600:
        raise AnalysisError('ANALYSIS_BUDGET_INVALID', 'native geometry timeout must be bounded')
    original = _read(source)
    if original.sha256 != contract['source_sha256']:
        raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'geometry source differs from the captured revision')
    inspect_fcstd_bytes(original.content)
    def operation(native):
        result = native.run('geometry', contract, {'source.FCStd': source}, output_dir, timeout_seconds=timeout_seconds, max_log_bytes=64*1024*1024)
        geometry = validate_geometry_manifest(result['geometry'])
        _unchanged(source, original)
        for reference in geometry['artifacts'].values():
            data = _read(Path(output_dir) / reference['relative_path'])
            if data.sha256 != reference['sha256']:
                raise AnalysisError('ANALYSIS_NATIVE_RECEIPT_INVALID', 'native geometry artifact hash changed')
            if reference['relative_path'].endswith('.FCStd'): inspect_fcstd_bytes(data.content)
        return geometry
    return _with_transaction(settings, transaction, operation)


def run_mesh_phase(geometry, bindings, level, settings, output_dir, *, transaction=None):
    from .analysis_geometry import validate_region_bindings, verify_region_mapping
    from .analysis_mesh import numerical_policy, validate_mesh, validate_native_mesh_recipe, validate_region_topology
    from .analysis_protocol import canonical_record, parse_record
    level = parse_record(canonical_record(level, schema_name='mesh-level').decode('utf-8'), schema_name='mesh-level')
    if level['policy_sha256'] != numerical_policy()[1]:
        raise AnalysisError('MESH_POLICY_CHANGED', 'mesh level belongs to a different fixed numerical policy')
    bindings = validate_region_bindings(bindings)
    if bindings['geometry_sha256'] != level['geometry_sha256'] or bindings['bindings_sha256'] != level['region_bindings_sha256']:
        raise AnalysisError('MESH_BINDING_INVALID', 'mesh and physical regions need the same reviewed geometry')
    original = _read(geometry)
    if original.sha256 != level['geometry_sha256']:
        raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'mesh geometry differs from its approved BREP')
    def operation(native):
        result = native.run('mesh', {'bindings': bindings, 'level': level, 'gmsh_path': _native_path(Path(settings['tools']['gmsh']['path']))}, {'geometry.brep': geometry}, output_dir, timeout_seconds=level['budget']['stage_seconds'], max_log_bytes=level['budget']['max_disk_bytes'])
        _unchanged(geometry, original)
        receipt = parse_record(canonical_record(result['mesh'], schema_name='mesh-receipt').decode('utf-8'), schema_name='mesh-receipt')
        if any(receipt.get(key) != level[key] for key in ('geometry_sha256', 'region_bindings_sha256', 'level', 'element_type')):
            raise AnalysisError('MESH_BINDING_INVALID', 'native mesh belongs to different physical inputs')
        directory = Path(output_dir)
        def artifact(name):
            reference = receipt['artifacts'][name]
            data = _read(directory / reference['relative_path'])
            if data.sha256 != reference['sha256']:
                raise AnalysisError('MESH_ARTIFACT_INVALID', 'native mesh artifact checksum changed')
            return data.content
        if artifact('level') != canonical_record(level, schema_name='mesh-level'):
            raise AnalysisError('MESH_LEVEL_CHANGED', 'native mesh belongs to a different frozen level or budget')
        validate_native_mesh_recipe(artifact('gmsh_geometry').decode('utf-8'))
        mapping = json.loads(artifact('mapping'))
        verify_region_mapping(bindings, mapping)
        data = json.loads(artifact('data'))
        if type(data) is not dict or set(data) != {'node_order', 'nodes', 'elements'} or data['node_order'] != 'CalculiX_C3D10':
            raise AnalysisError('MESH_MAPPING_INCOMPLETE', 'explicit C3D10 node ordering is required')
        nodes = {row[0] for row in data['nodes']}
        elements = {row[0] for row in data['elements']}
        if len(nodes) != receipt['node_count'] or len(data['nodes']) != len(nodes) or len(elements) != receipt['element_count'] or len(data['elements']) != len(elements) or any(len(row) != 4 or type(row[0]) is not int or row[0] <= 0 or any(type(value) not in (int,float) or not math.isfinite(value) for value in row[1:]) for row in data['nodes']) or any(len(row) != 11 or type(row[0]) is not int or row[0] <= 0 or len(set(row[1:])) != 10 or any(type(node) is not int or node not in nodes for node in row[1:]) for row in data['elements']) or any(not set(region['node_ids']) <= nodes or any(pair[0] not in elements for pair in region['element_faces']) for region in mapping['regions']):
            raise AnalysisError('MESH_MAPPING_INCOMPLETE', 'mesh counts, connectivity or physical region IDs are incomplete')
        validate_region_topology(data, mapping)
        validation = validate_mesh(receipt, level)
        if validation['status'] != 'passed':
            raise AnalysisError(validation['code'], 'mesh budget exhausted; quality is inconclusive')
        secure_fs.atomic_publish_new(directory / 'mesh.json', canonical_record(receipt, schema_name='mesh-receipt'))
        return receipt
    return _with_transaction(settings, transaction, operation)
