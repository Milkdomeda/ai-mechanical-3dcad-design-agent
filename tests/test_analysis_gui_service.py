"""Registered GUI metadata never accepts caller-supplied numerical truth."""
from copy import deepcopy

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_session import session
from test_analysis_coordinator import setup,submit


def candidate(tmp_path):
    from mechanical_design_agent.analysis_gui_service import AnalysisGuiService
    root,service,store,request,settings=session(tmp_path)
    prepared=service.prepare(design_id='fixture-design',analysis_id='static',request=request,expected_revision=None)
    gui=AnalysisGuiService(store)
    envelope=gui.issue_request(design_id='fixture-design',analysis_id='static',kind='preview',spec_sha256=prepared['spec_sha256'])
    return root,service,store,request,prepared,gui,envelope


def test_registered_preview_binds_exact_candidate_and_rejects_mutated_envelope(tmp_path):
    root,service,store,request,prepared,gui,envelope=candidate(tmp_path)
    record,analysis,target,folder,revision,spec=gui._registered(envelope)
    assert record['revision_ref']==prepared['current_revision']
    assert record['spec_sha256']==prepared['spec_sha256']
    wrong=deepcopy(envelope);wrong['request']['spec_sha256']='a'*64
    with pytest.raises(AnalysisError) as error:gui._registered(wrong)
    assert error.value.code=='ANALYSIS_GUI_REQUEST_UNREGISTERED'
    assert not (target/'outputs').exists()


def test_changed_candidate_cannot_reuse_the_old_registered_preview(tmp_path):
    root,service,store,request,prepared,gui,envelope=candidate(tmp_path)
    request['draft_spec']['material']['youngs_modulus']['value']='2500'
    service.prepare(design_id='fixture-design',analysis_id='static',request=request,expected_revision=prepared['state_revision'])
    with pytest.raises(AnalysisError) as error:gui._registered(envelope)
    assert error.value.code=='ANALYSIS_GUI_SPEC_CHANGED'


def test_receipt_from_a_different_registration_is_rejected_before_read(tmp_path):
    root,service,store,request,prepared,gui,envelope=candidate(tmp_path)
    with pytest.raises(AnalysisError) as error:gui.verify_receipt(envelope,{'relative_path':'gui/other/receipt.json','sha256':'a'*64})
    assert error.value.code=='ANALYSIS_GUI_RECEIPT_INVALID'


def test_queued_run_cannot_issue_a_final_gui_request(tmp_path):
    from mechanical_design_agent.analysis_gui_service import AnalysisGuiService
    root,service,store,request,approved,coordinator,launcher=setup(tmp_path)
    queued=submit(coordinator,approved)
    with pytest.raises(AnalysisError) as error:AnalysisGuiService(store).issue_request(design_id='fixture-design',analysis_id='static',kind='final_result',run_id=queued['run_id'],spec_sha256=approved['spec_sha256'])
    assert error.value.code=='ANALYSIS_GUI_RUN_UNVALIDATED'
