"""Fresh product GUI from qualified safe inputs; no customer FCStd restore."""
import json
import os

import pytest

from test_analysis_deck_live import native_decks,declared_spec
from test_analysis_geometry_live import native_geometry


@pytest.mark.live_fea
def test_product_native_gui_cloud_and_standard_button_export(native_decks):
    if os.environ.get('FEA_PRODUCT_GUI_LIVE')!='1':pytest.skip('requires explicit visible isolated product GUI opt-in')
    from mechanical_design_agent.analysis_runtime_patch import PROFILE
    from mechanical_design_agent.analysis_deck import build_native_deck
    from mechanical_design_agent.analysis_solver import solve_native
    from mechanical_design_agent.analysis_results import extract_fields
    from mechanical_design_agent.analysis_gui import rebuild_native
    directory,settings,geometry,mesh,level,native=native_decks
    if settings['configuration']['runtime']['profile']!=PROFILE:pytest.skip('requires separately reviewed R2 runtime')
    spec=declared_spec(mesh,level,'force',settings)
    spec['material']['youngs_modulus']['value']='200000';spec['material']['poisson_ratio']['value']='0'
    spec['cases'][0]['loads'][0]['components']=[{'value':str(value),'unit':'N'} for value in (40,0,0)]
    deck=build_native_deck(spec,geometry,mesh,directory/'GUI deck 中文',settings,case_id='static',transaction=native)
    solver=solve_native(deck,mesh,settings,directory/'GUI solver 中文',native=native)
    fields=extract_fields(deck,solver,settings,directory/'GUI fields 中文',transaction=native)
    result=rebuild_native(deck,mesh,fields,solver,geometry,settings,directory/'GUI product 中文',transaction=native)
    assert result['native_automation_status']=='passed'
    assert result['status']=='awaiting_visual_review'
    assert result['native_input_comparison']['equivalent'] is True
    assert result['source_fcstd_open_count']==0
    assert (directory/'GUI product 中文/analysis.FCStd').is_file()
    from fea_product_gui_roundtrip import verify_roundtrip_fixture
    restored=verify_roundtrip_fixture(native,directory/'GUI product 中文',deck,settings)
    assert restored['source_unchanged'] and restored['native_write_button']


@pytest.mark.live_fea
def test_product_preview_uses_the_exact_candidate_and_all_cases(native_decks):
    if os.environ.get('FEA_PRODUCT_GUI_LIVE')!='1':pytest.skip('requires explicit visible isolated product GUI opt-in')
    from mechanical_design_agent.analysis_gui import preview_native
    from mechanical_design_agent.analysis_protocol import record_sha256
    from mechanical_design_agent.analysis_revision import read_artifact
    directory,settings,geometry,mesh,level,native=native_decks
    stage=directory/'review candidate 中文';stage.mkdir();target=stage/'geometry';target.mkdir()
    for ref in mesh['geometry_manifest']['artifacts'].values():
        (target/ref['relative_path']).write_bytes(read_artifact(geometry.parent,ref).content)
    spec=declared_spec(mesh,level,'force',settings)
    spec.update(status='ready_for_review')
    receipt=preview_native({'spec':spec,'spec_sha256':record_sha256(spec,schema_name='spec'),'geometry':mesh['geometry_manifest'],'regions':mesh['region_bindings'],'settings':settings,'output_dir':str(stage/'preview')},transaction=native)
    assert receipt['status']=='completed'
    assert len(receipt['images'])==len(spec['cases'])
    assert receipt['visible_observable_ids']==sorted(item['id'] for item in spec['evaluation']['observables'])
    assert receipt['spec_sha256']==record_sha256(spec,schema_name='spec')
