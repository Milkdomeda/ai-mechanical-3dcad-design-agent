import json

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def test_missing_qualification_does_not_enable_the_default_worker(tmp_path):
    from mechanical_design_agent.analysis_qualification import require_qualification
    settings={'configuration_path':str(tmp_path/'config.json'),'configuration_sha256':'a'*64,'runtime':{'manifest_sha256':'b'*64}}
    with pytest.raises(AnalysisError) as error:require_qualification(settings)
    assert error.value.code=='FEA_QUALIFICATION_REQUIRED'
    assert list(tmp_path.iterdir())==[]


def test_qualification_honors_checkpoint_before_reading_any_receipt(tmp_path):
    from mechanical_design_agent.analysis_qualification import require_qualification
    def cancelled():raise AnalysisError('ANALYSIS_CANCELLED','Synthetic preflight cancellation.')
    with pytest.raises(AnalysisError) as error:require_qualification({'configuration_path':str(tmp_path/'config.json')},checkpoint=cancelled)
    assert error.value.code=='ANALYSIS_CANCELLED'


def test_caller_pass_boolean_is_not_a_qualification_receipt(tmp_path):
    from mechanical_design_agent.analysis_qualification import require_qualification
    (tmp_path/'config.qualification.json').write_text(json.dumps({'status':'passed','gui':True,'numerical_status':'passed'}))
    settings={'configuration_path':str(tmp_path/'config.json'),'configuration_sha256':'a'*64,'runtime':{'manifest_sha256':'b'*64}}
    with pytest.raises(AnalysisError):require_qualification(settings)


def test_synthetic_qualification_spec_never_becomes_a_customer_default():
    from mechanical_design_agent.analysis_qualification_inputs import make_inputs
    from test_analysis_geometry import geometry
    value=geometry()
    # This protocol unit fixture has only the explicit two end caps.
    spec,regions=make_inputs(value,{'runtime':{'manifest_sha256':'b'*64}})
    assert spec['design_id']=='software-qualification'
    assert len(spec['cases'])==2 and spec['criteria']==[]
    assert 'not production material approval' in spec['material']['evidence'][0]
    assert spec['mesh']['minimum_thickness_elements']==2
    assert spec['validation']['stress_rtol']['value']=='0.02'


def test_large_hashed_native_artifact_does_not_use_the_caller_request_limit(tmp_path):
    import hashlib
    from mechanical_design_agent.analysis_qualification import _json
    from mechanical_design_agent.analysis_protocol import parse_json_value
    data=json.dumps({'native_output':'x'*(4*1024*1024)}).encode()
    (tmp_path/'native.json').write_bytes(data)
    reference={'relative_path':'native.json','sha256':hashlib.sha256(data).hexdigest()}
    assert len(_json(tmp_path,reference)['native_output'])==4*1024*1024
    with pytest.raises(AnalysisError) as error:parse_json_value(data.decode(),expected=dict)
    assert error.value.code=='ANALYSIS_JSON_LIMIT'
    (tmp_path/'native.json').write_bytes(data+b'changed')
    with pytest.raises(AnalysisError):_json(tmp_path,reference)
