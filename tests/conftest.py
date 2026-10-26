from __future__ import annotations

import os
from pathlib import Path

from mechanical_design_agent.config import load_env_file


_requested_env_file = os.environ.get("MECH_DESIGN_ENV_FILE", "").strip()
if _requested_env_file:
    load_env_file(Path(_requested_env_file).expanduser().resolve())

# Shared explicit-live fixture stays lazy when the native environment is unset.
from fea_product_native_fixture import native_product_session, native_transaction

import pytest

@pytest.fixture(scope='session')
def native_decks(native_product_session):
    import hashlib
    from test_analysis_mesh_live import mesh_inputs
    from mechanical_design_agent.analysis_geometry import inspect_geometry
    from mechanical_design_agent.analysis_mesh import generate_mesh
    directory, fixtures, settings = native_product_session[:3]
    native = native_product_session[3]
    source = fixtures / 'box.FCStd'
    request = {'schema_version': 'AnalysisGeometryRequest/v1', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'included_object': 'Solid', 'excluded_objects': ['Ball1', 'Ball2', 'Ball3', 'Ball4'], 'partitions': [], 'volume_tolerance': {'value': '.000001', 'unit': 'mm^3'}}
    geometry_dir = directory / 'deck geometry 中文'
    manifest = inspect_geometry(source, request, settings, geometry_dir, transaction=native)
    bindings, levels = mesh_inputs(manifest, 'box')
    mesh_dir = directory / 'deck mesh 中文'
    receipt = generate_mesh(geometry_dir / 'geometry.brep', bindings, levels[0], settings, mesh_dir, transaction=native)
    bundle = {'directory': str(mesh_dir), 'receipt': receipt, 'geometry_manifest': manifest, 'region_bindings': bindings}
    yield directory, settings, geometry_dir / 'geometry.brep', bundle, levels[0], native

