import asyncio
import inspect
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mechanical_design_agent.server import create_mcp
from mechanical_design_agent.analysis_types import AnalysisError

NAMES={'analysis_capabilities','analysis_prepare','analysis_approve','analysis_run','analysis_status','analysis_cancel','analysis_report','analysis_confirm'}


def call(server,name,*args,**kwargs):
    result=server._tool_manager._tools[name].fn(*args,**kwargs)
    return json.loads(asyncio.run(result) if inspect.isawaitable(result) else result)


def test_eight_analysis_tools_are_additive_design_only_and_preserve_old_schemas():
    baseline=json.loads((Path(__file__).parent/'fixtures/mcp_design_v071_parameters.json').read_text())
    design=create_mcp(tool_profile='design');admin=create_mcp(tool_profile='knowledge-admin')
    assert set(design._tool_manager._tools)==set(baseline)|NAMES
    assert not NAMES&set(admin._tool_manager._tools)
    assert {name:design._tool_manager._tools[name].parameters for name in baseline}==baseline


def test_analysis_service_is_lazy_and_errors_preserve_stable_codes_and_cas_details():
    factories=[]
    class Service:
        def status(self,**kwargs):raise AnalysisError('ANALYSIS_REVISION_CONFLICT','Synthetic stale revision.',details={'state_revision':9})
    def factory(*args):factories.append(args);return Service()
    server=create_mcp(analysis_service_factory=factory,tool_profile='design')
    assert factories==[]
    with pytest.raises(ToolError) as error:call(server,'analysis_status','fixture','static')
    value=json.loads(str(error.value));assert value['code']=='ANALYSIS_REVISION_CONFLICT'
    assert value['details']['state_revision']==9 and len(factories)==1


def test_duplicate_prepare_json_is_rejected_before_service_execution():
    class Service:
        def prepare(self,**kwargs):pytest.fail('duplicate input cannot reach producer')
    server=create_mcp(analysis_service=Service(),tool_profile='design')
    with pytest.raises(ToolError) as error:call(server,'analysis_prepare','fixture','static','{"schema_version":"x","schema_version":"y"}')
    assert json.loads(str(error.value))['code']=='ANALYSIS_JSON_DUPLICATE'


def test_report_has_closed_actions_and_cannot_accept_arbitrary_solver_results():
    class Service:
        def report(self,**kwargs):pytest.fail('unknown action or arbitrary result cannot reach publisher')
    server=create_mcp(analysis_service=Service(),tool_profile='design')
    with pytest.raises(ToolError):call(server,'analysis_report','fixture','static','run-1',action='upload_results',evidence_refs_json='[{"numerical_status":"passed"}]')
