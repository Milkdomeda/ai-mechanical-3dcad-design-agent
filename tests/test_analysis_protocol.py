from copy import deepcopy
import json
from pathlib import Path

import pytest


def spec():
    return json.loads((Path(__file__).parent/'fixtures/fea/spec_minimal.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('text', [
    '{"schema_version":"AnalysisSpec/v1","schema_version":"AnalysisSpec/v1"}',
    '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}',
    '[]', 'null', '{"x":{"a":1,"a":2}}',
])
def test_parser_rejects_duplicate_nonfinite_and_nonobject_json(text):
    from mechanical_design_agent.analysis_protocol import parse_record
    from mechanical_design_agent.analysis_types import AnalysisError
    with pytest.raises(AnalysisError):parse_record(text,schema_name='spec')


@pytest.mark.parametrize('escaped', [False, True])
def test_unpaired_unicode_surrogate_is_a_controlled_boundary_error(escaped):
    from mechanical_design_agent.analysis_protocol import canonical_record, parse_record
    from mechanical_design_agent.analysis_types import AnalysisError
    value = spec()
    value['assumptions'] = ['invalid \ud800 text']
    with pytest.raises(AnalysisError): canonical_record(value, schema_name='spec')
    with pytest.raises(AnalysisError): parse_record(json.dumps(value, ensure_ascii=escaped), schema_name='spec')


@pytest.mark.parametrize('defect',['unknown_root','unknown_nested','wrong_version','wrong_unit','bool_revision','nan_in_memory','nonstring_key'])
def test_strict_schema_rejects_unknown_fields_units_and_versions(defect):
    from mechanical_design_agent.analysis_protocol import canonical_record
    from mechanical_design_agent.analysis_types import AnalysisError
    value=spec()
    if defect=='unknown_root':value['python']='print(1)'
    if defect=='unknown_nested':value['material']['script']='untrusted'
    if defect=='wrong_version':value['schema_version']='AnalysisSpec/v2'
    if defect=='wrong_unit':value['material']['youngs_modulus']['unit']='kg'
    if defect=='bool_revision':value['revision']=True
    if defect=='nan_in_memory':value['material']['youngs_modulus']['value']=float('nan')
    if defect=='nonstring_key':value[1]='cannot silently stringify'
    with pytest.raises(AnalysisError):canonical_record(value,schema_name='spec')


def test_canonical_equivalent_decimals_have_same_hash_without_dropping_semantics():
    from mechanical_design_agent.analysis_protocol import canonical_record,record_sha256,parse_record
    first=spec();second=deepcopy(first)
    second['material']['youngs_modulus']['value']='1e3'
    second['material']['poisson_ratio']['value']='0.3000'
    original=deepcopy(second)
    assert record_sha256(first,schema_name='spec')==record_sha256(second,schema_name='spec')
    assert second==original
    encoded=canonical_record(second,schema_name='spec')
    assert parse_record(encoded.decode(),schema_name='spec')['material']['youngs_modulus']['value']=='1000'
    second['assumptions'][0]='Different physical assumption'
    assert record_sha256(first,schema_name='spec')!=record_sha256(second,schema_name='spec')


def test_only_registered_package_schemas_can_be_selected():
    from mechanical_design_agent.analysis_protocol import parse_record
    from mechanical_design_agent.analysis_types import AnalysisError
    for name in ('../spec','https://example.com/spec','file:///spec','unknown'):
        with pytest.raises(AnalysisError) as caught:parse_record('{}',schema_name=name)
        assert caught.value.code=='ANALYSIS_SCHEMA_UNKNOWN'


def test_registered_schemas_resolve_only_package_local_references():
    from mechanical_design_agent.analysis_protocol import schema_inventory
    schemas=schema_inventory()
    assert {'prepare-request','spec','session','run','region','result','gui-request','gui-receipt','manifest','completion','configuration'}<=set(schemas)
    for name,value in schemas.items():
        assert value['additionalProperties'] is False
        assert value['properties']['schema_version']['const'].endswith('/v1')


@pytest.mark.parametrize('defect',['unbounded_decimal','inconsistent_input','incomplete_approved'])
def test_quantity_and_approved_contracts_cannot_hide_missing_or_inconsistent_input(defect):
    from mechanical_design_agent.analysis_protocol import parse_record
    from mechanical_design_agent.analysis_types import AnalysisError
    value=spec()
    if defect=='unbounded_decimal':value['material']['youngs_modulus']['value']='1e99999'
    if defect=='inconsistent_input':value['material']['youngs_modulus']['input']={'value':'1','unit':'Pa'}
    if defect=='incomplete_approved':value['status']='approved'
    with pytest.raises(AnalysisError):parse_record(json.dumps(value),schema_name='spec')
