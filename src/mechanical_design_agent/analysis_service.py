"""Lazy shared service facade for CLI and MCP, without MCP-to-MCP calls."""
from pathlib import Path

from .analysis_types import AnalysisError


class AnalysisService:
    def __init__(self,runtime,context_loader=None):
        self.runtime=runtime;self.context_loader=context_loader or self._knowledge_context

    def _knowledge_context(self,query,features):
        from .knowledge_repository import KnowledgeRepository,KnowledgeScope
        from .knowledge_service import KnowledgeService
        from .projection import Neo4jProjection
        settings=self.runtime.knowledge_settings();scope=self.runtime.design_knowledge_scope()
        repository=KnowledgeRepository(settings.database_url,KnowledgeScope(settings.organization_id,settings.design_group_id))
        service=KnowledgeService(repository,Neo4jProjection(settings.neo4j_uri,settings.neo4j_user,settings.neo4j_password),settings.workspace)
        return service.design_context_build(organization_id=scope['organization_id'],design_group_id=scope['design_group_id'],requested_family_id=features.pop('requested_family_id',None),design_features=features,lesson_query=query)

    def _store(self):
        from .analysis_store import AnalysisStore
        return AnalysisStore(self.runtime._manifest().workspace)

    def _configuration(self):
        from .analysis_configuration import load_analysis_configuration
        return load_analysis_configuration(self.runtime)

    def _session(self):
        from .analysis_session import AnalysisSessionService
        from .analysis_geometry import inspect_geometry
        from .analysis_knowledge import AnalysisKnowledgeService
        from .analysis_gui import preview_native
        knowledge=AnalysisKnowledgeService(self.runtime,self.context_loader)
        return AnalysisSessionService(self._store(),configuration_loader=self._configuration,geometry_inspector=lambda ctx:inspect_geometry(Path(ctx['source_path']),ctx['request'],ctx['settings'],Path(ctx['output_dir'])),knowledge_loader=lambda ctx:knowledge.retrieve(**ctx),gui_previewer=preview_native)

    def _coordinator(self):
        from .analysis_coordinator import AnalysisCoordinator
        from .analysis_process import ProcessController
        return AnalysisCoordinator(self._store(),ProcessController())

    def _delivery(self):
        from .analysis_delivery_service import AnalysisDeliveryService
        return AnalysisDeliveryService(self._store())

    def capabilities(self,*,design_id=None):
        from .analysis_configuration import analysis_capabilities
        return analysis_capabilities(self.runtime,design_id=design_id,include_qualification=True)

    def schema(self,*,name):
        from .analysis_protocol import _schemas
        schemas,_=_schemas()
        if name not in schemas:raise AnalysisError('ANALYSIS_SCHEMA_UNKNOWN','select a registered analysis schema',details={'available':sorted(schemas)})
        return schemas[name]

    def skill(self,*,destination):
        from importlib.resources import files,as_file
        from . import secure_fs
        import hashlib
        resource=files('mechanical_design_agent').joinpath('resources/skills/mechanical-fea')
        names=['SKILL.md','agents/openai.yaml','references/workflow.md']
        try:
            with as_file(resource) as source:contents={name:(source/name).read_bytes() for name in names}
        except (OSError,FileNotFoundError) as exc:raise AnalysisError('ANALYSIS_SKILL_UNAVAILABLE','install the built wheel to export its bundled FEA skill') from exc
        target=secure_fs.ensure_managed_directory(Path(destination),parents=False,exist_ok=False).path
        for name,content in contents.items():
            secure_fs.ensure_managed_directory((target/name).parent,parents=True,exist_ok=True)
            secure_fs.atomic_publish_new(target/name,content)
        return {'status':'ready','skill_directory':str(target),'files':[{'relative_path':name,'sha256':hashlib.sha256(content).hexdigest()} for name,content in contents.items()]}

    def prepare(self,**kwargs):return self._session().prepare(**kwargs)

    def approve(self,**kwargs):return self._session().approve(**kwargs)

    def run(self,**kwargs):
        from .analysis_qualification import require_qualification
        require_qualification(self._configuration())
        return self._coordinator().submit(**kwargs)

    def doctor(self,*,qualify=False):
        from .analysis_qualification import require_qualification,qualify_runtime
        from . import secure_fs
        import uuid
        try:
            settings=self._configuration()
            if not qualify:return require_qualification(settings)
            parent=secure_fs.ensure_managed_directory(Path(settings['workspace'])/'data/analysis-qualification',parents=True,exist_ok=True).path
            return qualify_runtime(settings,parent/('q-'+uuid.uuid4().hex))
        except AnalysisError as exc:return {**exc.as_dict(),'status':'setup_required','next_action':'Run explicit analysis setup from an official distribution, then analysis doctor --qualify; inspect preserved diagnostics for a failed check.'}

    def setup(self,*,source_root,destination,native_scratch_root,tools=None,software_opengl=False):
        from .analysis_setup import prepare_runtime
        from . import secure_fs
        configuration=prepare_runtime(source_root,destination,native_scratch_root=native_scratch_root,tools=tools,software_opengl=software_opengl)
        workspace=self.runtime._manifest().workspace;target=workspace/'config/fea.json'
        with secure_fs.exclusive_file_lock(target.with_suffix('.lock')):
            if target.exists():
                raise AnalysisError('FEA_CONFIG_EXISTS','a workspace FEA configuration already exists; preserve it and select the new explicit configuration with MECH_DESIGN_FEA_CONFIG',details={'configuration_path':str(configuration)})
            secure_fs.atomic_publish_new(target,secure_fs.read_managed_file(configuration).content)
        return {'status':'runtime_configured','configuration_path':str(target),'isolated_configuration_path':str(configuration),'qualification':'required','next_action':'Run analysis doctor --qualify for the installed package; no current FreeCAD installation was modified.'}

    def status(self,**kwargs):return self._coordinator().status(**kwargs)

    def open(self,**kwargs):
        from .analysis_viewer import open_viewer
        return open_viewer(self._store(),**kwargs)

    def open_status(self,**kwargs):
        from .analysis_viewer import viewer_status
        return viewer_status(self._store(),**kwargs)

    def close(self,**kwargs):
        from .analysis_viewer import close_viewer
        return close_viewer(self._store(),**kwargs)

    def cancel(self,**kwargs):return self._coordinator().cancel(**kwargs)

    def report(self,*,design_id,analysis_id,run_id,action='get',evidence_refs=None,expected_revision=None):
        delivery=self._delivery();identity={'design_id':design_id,'analysis_id':analysis_id,'run_id':run_id}
        if action=='get':
            if evidence_refs:raise AnalysisError('ANALYSIS_REPORT_ACTION_INVALID','read-only report queries cannot provide visual mutation evidence')
            return delivery.get_report(**identity)
        if action=='rebuild_figures':
            if evidence_refs:raise AnalysisError('ANALYSIS_REPORT_ACTION_INVALID','reconstruction does not accept caller result evidence')
            return delivery.rebuild_figures(**identity,expected_revision=expected_revision)
        if action!='finalize_visual':raise AnalysisError('ANALYSIS_REPORT_ACTION_INVALID','report action must be get or finalize_visual')
        from .analysis_runs import read_run
        run,_=read_run(delivery.store,**identity)
        operation=delivery.revise_delivery if run['status']=='completed' and evidence_refs else delivery.finalize_visual
        return operation(**identity,evidence_refs=evidence_refs or [],expected_revision=expected_revision)

    def confirm(self,**kwargs):return self._delivery().confirm(**kwargs)
