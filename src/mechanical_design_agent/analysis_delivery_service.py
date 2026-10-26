"""Complete verified reports after explicit assistant inspection of figures."""
import hashlib
import json
import re
from pathlib import Path
import uuid

from . import secure_fs
from .analysis_delivery import verify_completion,_inventory,validate_dependency_graph
from .analysis_budget import BudgetGuard
from .analysis_process import ProcessController
from .analysis_execution import verify_execution
from .analysis_executor_identity import verify_executor_identity
from .analysis_gui_service import _load,_put,_authorize
from .analysis_gui_service import AnalysisGuiService
from .analysis_protocol import canonical_record,parse_json_value
from .analysis_queue import scheduler_guard
from .analysis_reporting import build_report
from .analysis_revision import read_artifact
from .analysis_runs import change_run_unlocked,read_run,read_run_unlocked,run_root
from .analysis_store import _timestamp
from .analysis_types import AnalysisError
from .approval_semantics import APPROVE,classify_approval
from .workspace_bootstrap import read_workspace_manifest


def _copy(source,reference,destination,name,*,checkpoint=None):
    if checkpoint is not None:checkpoint()
    data=read_artifact(source,reference);target=destination/name
    secure_fs.ensure_managed_directory(target.parent,parents=True,exist_ok=True)
    secure_fs.atomic_publish_new(target,data.content)
    if checkpoint is not None:checkpoint()
    return {'relative_path':name,'sha256':data.sha256}


class AnalysisDeliveryService:
    def __init__(self,store):self.store=store

    @staticmethod
    def _selected(root,run):
        path=root/'delivery-head.json'
        if not path.exists():
            # Initial publication can crash between terminal run and head writes.
            # A later presentation cannot fall back to that historical run ref.
            published=list(root.glob('deliveries/v*/completion.json'))
            if len(published)>1:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','presentation selection is missing; preserve all versions for explicit recovery')
            return run['completion_ref']
        try:head=parse_json_value(secure_fs.read_managed_file(path).content.decode('utf-8'),expected=dict)
        except (AnalysisError,OSError,UnicodeError,secure_fs.SecureFilesystemError) as exc:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','presentation revision pointer cannot be verified') from exc
        if set(head)!={'schema_version','run_id','completion_ref','manifest_sha256'} or head['schema_version']!='AnalysisDeliveryHead/v1' or head['run_id']!=run['run_id']:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','presentation revision pointer has changed identity')
        completion=_load(root,head['completion_ref'],'completion')
        if completion['manifest_ref']['sha256']!=head['manifest_sha256'] or completion['run_id']!=run['run_id'] or completion['spec_sha256']!=run['spec_sha256']:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','presentation revision points to different physical evidence')
        return head['completion_ref']

    def _pending(self,design_id,analysis_id,run_id):
        run,request=read_run(self.store,design_id,analysis_id,run_id)
        if run['status']!='awaiting_visual_review' or run['worker'] is not None or run.get('execution_ref') is None or run['cancel_requested']:raise AnalysisError('ANALYSIS_DELIVERY_NOT_READY','sealed numerical and GUI evidence must pause before visual finalization')
        context=verify_execution(self.store,design_id,analysis_id,run_id,run['execution_ref'])
        current=self.store.currentness_for_source(design_id,analysis_id,request['source_binding_ref'])
        if current['snapshot_integrity']!='passed':raise AnalysisError('ANALYSIS_SOURCE_SNAPSHOT_CHANGED','the exact source snapshot must remain intact')
        state=self.store.read_state(design_id,analysis_id)
        if current['currentness']=='current' and state['current_revision']!=request['revision_ref']:current={**current,'currentness':'spec_changed'}
        context.update(currentness=current,state=state)
        return context

    @staticmethod
    def _images(context,*,portable=False):
        root=context['gui_root'];result=[]
        for case in context['gui']['cases']:
            directory=root/'outputs'/case['case_id'];evidence=_load(root.parents[1],case['evidence_ref'])
            names=[field['figure'] for field in evidence['display_fields'].values()]+['mesh-native.png']
            for name in names:
                data=secure_fs.read_managed_file(directory/name)
                relative='gui/outputs/'+case['case_id']+'/'+name if portable else (directory/name).relative_to(root.parents[1]).as_posix()
                result.append({'relative_path':relative,'sha256':data.sha256})
        return sorted(result,key=lambda item:item['relative_path'])

    def get_report(self,design_id,analysis_id,run_id):
        run,request=read_run(self.store,design_id,analysis_id,run_id);root=run_root(self.store,design_id,analysis_id,run_id)
        _authorize(self.store,request['scope'])
        if run['status']!='completed':
            if run['status']=='awaiting_visual_review' and run.get('execution_ref') is not None and run['worker'] is None:
                context=self._pending(design_id,analysis_id,run_id);analysis=self.store._root(design_id,analysis_id)
                images=self._images(context)
                return {'status':'awaiting_visual_review','run_id':run_id,'state_revision':context['state']['state_revision'],'numerical_status':run['numerical_status'],'criteria_status':run['criteria_status'],'applicability_status':run['applicability_status'],'currentness':context['currentness']['currentness'],'required_visual_evidence':images,'images':[{'path':str(analysis/item['relative_path']),**item} for item in images],'completion':'not_published'}
            return {'status':run['status'],'run_id':run_id,'completion':'not_published','diagnostic':run['diagnostic']}
        completion_ref=self._selected(root,run);checked=verify_completion(root,completion_ref)
        current=self.store.currentness_for_source(design_id,analysis_id,request['source_binding_ref'])
        if current['snapshot_integrity']!='passed':raise AnalysisError('ANALYSIS_SOURCE_SNAPSHOT_CHANGED','published source snapshot binding needs recovery')
        state=self.store.read_state(design_id,analysis_id)
        if current['currentness']=='current' and state['current_revision']!=request['revision_ref']:current={**current,'currentness':'spec_changed'}
        manifest_sha=checked['completion']['manifest_ref']['sha256']
        acceptance='not_requested'
        if state['accepted_delivery']==manifest_sha:
            if state.get('acceptance_ref') is None:acceptance='unverified'
            else:
                receipt=_load(self.store._root(design_id,analysis_id),state['acceptance_ref'],'acceptance')
                if any(receipt[key]!=value for key,value in {'design_id':design_id,'analysis_id':analysis_id,'run_id':run_id,'manifest_sha256':manifest_sha,'acceptance_state':'APPROVE'}.items()):raise AnalysisError('ANALYSIS_ACCEPTANCE_INTEGRITY','selected acceptance receipt belongs to different evidence')
                _authorize(self.store,receipt['scope']);acceptance='accepted'
        return {'status':'completed','run_id':run_id,'completion_ref':completion_ref,'manifest_sha256':manifest_sha,'result':checked['result'],'report_path':str(Path(checked['directory'])/'report.md'),'directory':checked['directory'],'currentness':current['currentness'],'state_revision':state['state_revision'],'delivery_version':checked['completion']['delivery_version'],'acceptance':acceptance}

    def finalize_visual(self,*,design_id,analysis_id,run_id,evidence_refs,expected_revision):
        observed,_=read_run(self.store,design_id,analysis_id,run_id)
        if observed['status']=='completed':return self._recover_published(design_id,analysis_id,run_id,expected_revision)
        context=self._pending(design_id,analysis_id,run_id)
        return self._publish(context,evidence_refs,expected_revision,revising=False)

    def _publish(self,context,evidence_refs,expected_revision,*,revising):
        request=context['request'];design_id,analysis_id,run_id=(request[key] for key in ('design_id','analysis_id','run_id'))
        if type(expected_revision) is not int or context['state']['state_revision']!=expected_revision:raise AnalysisError('ANALYSIS_REVISION_CONFLICT','analysis changed before report construction')
        verify_executor_identity(request['executor_sha256'])
        images=self._images(context)
        if type(evidence_refs) is not list or any(type(ref) is not str for ref in evidence_refs) or sorted(evidence_refs)!=[item['relative_path'] for item in images]:raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','finalization declares actual inspection of every exact registered figure and mesh; missing or foreign references are not accepted')
        root=context['root'];deliveries=secure_fs.ensure_managed_directory(root/'deliveries',parents=False,exist_ok=True).path
        budget=dict(context['spec']['budget']);remaining=budget['wall_seconds']-context['execution']['budget']['wall_seconds']
        if remaining<=0:raise AnalysisError('ANALYSIS_TIMED_OUT','the declared compute wall budget has no publication time remaining')
        budget['wall_seconds']=min(remaining,budget['stage_seconds'])
        def cancelled():
            current,_=read_run(self.store,design_id,analysis_id,run_id)
            return current['cancel_requested'] or current['status']!=('completed' if revising else 'awaiting_visual_review')
        guard=BudgetGuard(budget,roots=lambda:[root,context.get('gui_root')],cancelled=cancelled,memory_observer=ProcessController().observed_memory_bytes,integrity_checkpoint=lambda:verify_executor_identity(request['executor_sha256']))
        guard.checkpoint(force=True)
        stage=secure_fs.ensure_managed_directory(deliveries/('.staging-'+uuid.uuid4().hex),parents=False,exist_ok=False).path
        version=1+sum(1 for path in deliveries.glob('v*/completion.json') if path.is_file())
        target=deliveries/('v'+str(version).zfill(6)+'-'+uuid.uuid4().hex)
        try:
            self._build(stage,context,version,guard=guard)
            completion_ref={'relative_path':'completion.json','sha256':secure_fs.read_managed_file(stage/'completion.json').sha256}
            checked=verify_completion(stage,completion_ref)
            guard.checkpoint(force=True)
            # All expensive checks finish before the short lifecycle locks.
            with scheduler_guard(self.store.workspace,create=True):
                analysis=self.store._root(design_id,analysis_id)
                with secure_fs.exclusive_file_lock(analysis/'.analysis.lock'),secure_fs.exclusive_file_lock(root/'.run.lock'):
                    state=self.store._state(analysis,design_id,analysis_id);run,stored=read_run_unlocked(root,design_id,analysis_id,run_id)
                    if state['state_revision']!=expected_revision or run['status']!=('completed' if revising else 'awaiting_visual_review') or run['worker'] is not None or run['cancel_requested'] or run.get('execution_ref')!=context['run']['execution_ref'] or stored!=request or run['completion_ref']!=context['run']['completion_ref']:raise AnalysisError('ANALYSIS_REVISION_CONFLICT','cancellation or changed evidence preceded final publication')
                    secure_fs.atomic_publish_directory(stage,target)
                    completion_ref['relative_path']=(target/'completion.json').relative_to(root).as_posix()
                    index_root=secure_fs.ensure_managed_directory(analysis/'deliveries/by-manifest',parents=True,exist_ok=True).path
                    manifest_sha=checked['completion']['manifest_ref']['sha256']
                    _put(index_root,manifest_sha+'.json',{'schema_version':'AnalysisDeliveryIndex/v1','run_id':run_id,'completion_ref':completion_ref,'manifest_sha256':manifest_sha,'delivery_version':version})
                    if not revising:change_run_unlocked(root,run,{'status':'completed','completion_ref':completion_ref},message='Verified reports and exact native figures published after assistant visual inspection.')
                    head={'schema_version':'AnalysisDeliveryHead/v1','run_id':run_id,'completion_ref':completion_ref,'manifest_sha256':manifest_sha}
                    head_path=root/'delivery-head.json';head_bytes=json.dumps(head,sort_keys=True).encode()
                    if head_path.exists():secure_fs.atomic_replace(head_path,head_bytes)
                    else:secure_fs.atomic_publish_new(head_path,head_bytes)
                    candidate={**state,'state_revision':expected_revision+1,'updated_at':_timestamp()}
                    if not revising:candidate.update(status='completed',active_run_id=None,latest_run_id=run_id,latest_delivery=manifest_sha)
                    elif state['latest_run_id']==run_id and state['current_revision']==request['revision_ref']:candidate['latest_delivery']=manifest_sha
                    secure_fs.atomic_replace(analysis/'analysis.json',canonical_record(candidate,schema_name='session'))
            return self.get_report(design_id,analysis_id,run_id)
        except AnalysisError:raise
        except (OSError,secure_fs.SecureFilesystemError) as exc:
            raise AnalysisError('ANALYSIS_REPORT_PUBLICATION_FAILED','Report publication failed. Keep staged evidence and retry report finalization; the solver is not rerun.') from exc

    def _recover_published(self,design_id,analysis_id,run_id,expected_revision):
        root=run_root(self.store,design_id,analysis_id,run_id);run,request=read_run(self.store,design_id,analysis_id,run_id)
        _authorize(self.store,request['scope'])
        reference=self._selected(root,run);checked=verify_completion(root,reference);analysis=self.store._root(design_id,analysis_id)
        manifest_sha=checked['completion']['manifest_ref']['sha256']
        with scheduler_guard(self.store.workspace,create=True),secure_fs.exclusive_file_lock(analysis/'.analysis.lock'),secure_fs.exclusive_file_lock(root/'.run.lock'):
            state=self.store._state(analysis,design_id,analysis_id);current,_=read_run_unlocked(root,design_id,analysis_id,run_id)
            if current['completion_ref']!=run['completion_ref'] or self._selected(root,current)!=reference:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','selected completion changed during recovery')
            if current['status']!='completed':raise AnalysisError('ANALYSIS_DELIVERY_NOT_READY','only an immutable completed run can recover publication metadata')
            if state['latest_run_id']==run_id and state['active_run_id'] in (None,run_id) and state['current_revision']==request['revision_ref'] and (state['active_run_id']==run_id or state['latest_delivery']!=manifest_sha):
                if type(expected_revision) is not int or state['state_revision']!=expected_revision:raise AnalysisError('ANALYSIS_REVISION_CONFLICT','analysis changed before selected completion recovery')
                candidate={**state,'status':'completed','active_run_id':None,'latest_delivery':manifest_sha,'state_revision':expected_revision+1,'updated_at':_timestamp()}
                secure_fs.atomic_replace(analysis/'analysis.json',canonical_record(candidate,schema_name='session'))
        return self.get_report(design_id,analysis_id,run_id)

    def revise_delivery(self,*,design_id,analysis_id,run_id,evidence_refs,expected_revision):
        previous=self._recover_published(design_id,analysis_id,run_id,expected_revision)
        if previous['status']!='completed':raise AnalysisError('ANALYSIS_DELIVERY_NOT_READY','a presentation revision requires an existing verified completion')
        run,request=read_run(self.store,design_id,analysis_id,run_id)
        if type(evidence_refs) is not list or not evidence_refs or any(type(ref) is not str for ref in evidence_refs):raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','new presentation evidence must name one registered native GUI request')
        matches=[re.fullmatch(r'gui/g-([0-9a-f]{32})/outputs/.+',ref) for ref in evidence_refs]
        if any(match is None for match in matches) or len({match[1] for match in matches})!=1:raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','foreign or mixed GUI registrations cannot revise a delivery')
        analysis=self.store._root(design_id,analysis_id);relative='gui/g-'+matches[0][1]+'/request.json'
        ref={'relative_path':relative,'sha256':secure_fs.read_managed_file(analysis/relative).sha256}
        envelope={'request':_load(analysis,ref,'gui-request'),'request_ref':ref}
        gui=AnalysisGuiService(self.store);registered,_,gui_root,_,_,_=gui._registered(envelope)
        if registered['kind']!='final_result' or any(registered[key]!=request[key] for key in ('design_id','analysis_id','run_id','spec_sha256')) or registered['revision_ref']!=request['revision_ref'] or registered['attempt_ref']!=run.get('attempt_ref'):raise AnalysisError('ANALYSIS_PHYSICS_CHANGED','changing physical inputs requires a new approved run, not a report revision')
        receipt_path='gui/g-'+matches[0][1]+'/receipt.json'
        receipt_ref={'relative_path':receipt_path,'sha256':secure_fs.read_managed_file(analysis/receipt_path).sha256}
        checked=gui.verify_receipt(envelope,receipt_ref)
        context=verify_execution(self.store,design_id,analysis_id,run_id,run['execution_ref'])
        context['execution']={**context['execution'],'gui_request':envelope,'gui_receipt_ref':receipt_ref}
        context.update(gui=checked['receipt'],gui_root=gui_root,state=self.store.read_state(design_id,analysis_id),currentness=self.store.currentness_for_source(design_id,analysis_id,request['source_binding_ref']))
        if context['currentness']['snapshot_integrity']!='passed':raise AnalysisError('ANALYSIS_SOURCE_SNAPSHOT_CHANGED','the original source snapshot must remain intact')
        if context['currentness']['currentness']=='current' and context['state']['current_revision']!=request['revision_ref']:context['currentness']={**context['currentness'],'currentness':'spec_changed'}
        if sorted(evidence_refs)!=[item['relative_path'] for item in self._images(context)]:raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','inspect all exact figures and meshes in the new registration')
        previous_result=previous['result']
        if previous_result['gui_ref']['sha256']==receipt_ref['sha256']:return previous
        return self._publish(context,evidence_refs,expected_revision,revising=True)

    def rebuild_figures(self,*,design_id,analysis_id,run_id,expected_revision):
        from .analysis_execution import execution_settings
        from types import SimpleNamespace
        identity={'design_id':design_id,'analysis_id':analysis_id,'run_id':run_id}
        run,request=read_run(self.store,**identity)
        _authorize(self.store,request['scope'])
        state=self.store.read_state(design_id,analysis_id)
        if type(expected_revision) is not int or state['state_revision']!=expected_revision:raise AnalysisError('ANALYSIS_REVISION_CONFLICT','analysis changed before presentation reconstruction')
        if run['status']!='completed' or run['worker'] is not None or run.get('execution_ref') is None:raise AnalysisError('ANALYSIS_DELIVERY_NOT_READY','a presentation revision requires an existing completed numerical run')
        verify_executor_identity(request['executor_sha256'])
        context=verify_execution(self.store,**identity,reference=run['execution_ref'])
        controller=ProcessController();settings=execution_settings(SimpleNamespace(store=self.store,request=request,_identity=lambda:identity))
        budget={**context['spec']['budget'],'wall_seconds':context['spec']['budget']['stage_seconds']}
        guard=BudgetGuard(budget,roots=lambda:[context['root']],cancelled=lambda:False,memory_observer=controller.observed_memory_bytes,integrity_checkpoint=lambda:verify_executor_identity(request['executor_sha256']))
        gui=AnalysisGuiService(self.store,configuration_loader=lambda:settings,controller=controller,guard=guard)
        envelope=gui.issue_request(**identity,kind='final_result',spec_sha256=request['spec_sha256'])
        try:checked=gui.execute_request(envelope);guard.checkpoint(force=True)
        finally:controller.terminate_owned(reason='interrupted')
        context.update(gui=checked['receipt'],gui_root=self.store._root(design_id,analysis_id)/Path(envelope['request_ref']['relative_path']).parent)
        images=self._images(context);analysis=self.store._root(design_id,analysis_id)
        return {'status':'awaiting_visual_review','run_status':run['status'],'revision_kind':'presentation','run_id':run_id,'state_revision':self.store.read_state(design_id,analysis_id)['state_revision'],'required_visual_evidence':images,'images':[{'path':str(analysis/item['relative_path']),**item} for item in images],'solver_rerun':False,'completion':'not_published'}

    def confirm(self,*,design_id,analysis_id,run_id,manifest_sha256,decision_text,expected_revision):
        if type(manifest_sha256) is not str or re.fullmatch('[0-9a-f]{64}',manifest_sha256) is None or type(decision_text) is not str or not decision_text.strip() or len(decision_text)>4096:raise AnalysisError('ANALYSIS_ACCEPTANCE_INVALID','acceptance needs an exact manifest and bounded explicit decision')
        decision=classify_approval(decision_text)
        if decision!=APPROVE:return {'acceptance_state':decision,'acceptance':'not_requested','manifest_sha256':manifest_sha256}
        report=self.get_report(design_id,analysis_id,run_id)
        if report['status']!='completed' or report['manifest_sha256']!=manifest_sha256 or report['currentness']!='current':raise AnalysisError('ANALYSIS_ACCEPTANCE_STALE','only the exact current completed report may be accepted as current design evidence')
        analysis=self.store._root(design_id,analysis_id);root=run_root(self.store,design_id,analysis_id,run_id)
        actor=read_workspace_manifest(self.store.workspace).actor_id
        _,request=read_run(self.store,design_id,analysis_id,run_id)
        receipt={'schema_version':'AnalysisAcceptance/v1','design_id':design_id,'analysis_id':analysis_id,'run_id':run_id,'manifest_sha256':manifest_sha256,'actor_id':actor,'scope':request['scope'],'decision_text':decision_text,'acceptance_state':'APPROVE','accepted_at':_timestamp(),'engineering_certification':False}
        with scheduler_guard(self.store.workspace,create=True),secure_fs.exclusive_file_lock(analysis/'.analysis.lock'),secure_fs.exclusive_file_lock(root/'.run.lock'):
            state=self.store._state(analysis,design_id,analysis_id);run,request=read_run_unlocked(root,design_id,analysis_id,run_id)
            if type(expected_revision) is not int or run['status']!='completed' or state['state_revision']!=expected_revision or state['active_run_id'] is not None or state['latest_run_id']!=run_id or state['latest_delivery']!=manifest_sha256 or state['current_revision']!=request['revision_ref'] or self._selected(root,run)!=report['completion_ref']:raise AnalysisError('ANALYSIS_REVISION_CONFLICT','the selected delivery or analysis revision changed before acceptance')
            folder=secure_fs.ensure_managed_directory(analysis/'acceptances',parents=False,exist_ok=True).path
            reference=_put(folder,'a-'+uuid.uuid4().hex+'.json',receipt,'acceptance');reference['relative_path']='acceptances/'+reference['relative_path']
            candidate={**state,'accepted_delivery':manifest_sha256,'acceptance_ref':reference,'state_revision':expected_revision+1,'updated_at':_timestamp()}
            secure_fs.atomic_replace(analysis/'analysis.json',canonical_record(candidate,schema_name='session'))
        return {'acceptance_state':'APPROVE','acceptance':'accepted','manifest_sha256':manifest_sha256,'state_revision':candidate['state_revision'],'acceptance_ref':reference,'engineering_certification':False}

    def _build(self,stage,context,version,*,guard=None):
        def copy(source,reference,destination,name):return _copy(source,reference,destination,name,checkpoint=None if guard is None else guard.checkpoint)
        request=context['request'];identity={key:request[key] for key in ('design_id','analysis_id','run_id','spec_sha256')}
        revision=context['revision'];folder=context['revision_root'];analysis=self.store._root(request['design_id'],request['analysis_id'])
        spec_ref=copy(folder,revision['artifacts']['spec'],stage,'spec.json')
        for key,name in [('approval','approval.json'),('knowledge_receipt','knowledge-receipt.json'),('summary','review-summary.json'),('regions','source/regions.json'),('load_ledger','source/load-ledger.json'),('tools','tool-bindings.json'),('geometry_brep','source/geometry.brep')]:
            copy(folder,revision['artifacts'][key],stage,name)
        source=self.store.read_source_binding(request['design_id'],request['analysis_id'],request['source_binding_ref'])
        copy(analysis,source['snapshot_ref'],stage,'source/snapshot.FCStd')
        copy(analysis,request['source_binding_ref'],stage,'source/binding.json')
        copy(context['root'],context['run']['request_ref'],stage,'execution-request.json')
        copy(context['root'],request['executor_identity_ref'],stage,'executor.json')
        attempt_root=context['attempt_root']
        for ref in context['attempt']['files']:copy(attempt_root,ref,stage,'attempt/'+ref['relative_path'])
        attempt_ref=copy(context['root'],context['execution']['attempt_ref'],stage,'attempt/attempt.json')
        registration=context['execution']['gui_request'];gui_ref=copy(analysis,context['execution']['gui_receipt_ref'],stage,'gui/receipt.json')
        copy(analysis,registration['request_ref'],stage,'gui/request.json')
        for case in context['gui']['cases']:
            evidence=_load(analysis,case['evidence_ref']);case_root=analysis/Path(case['evidence_ref']['relative_path']).parent
            copy(analysis,case['evidence_ref'],stage,'gui/outputs/'+case['case_id']+'/evidence.json')
            for reference in [*evidence['artifacts'].values(),*evidence['producer'].values()]:copy(case_root,reference,stage,'gui/outputs/'+case['case_id']+'/'+reference['relative_path'])
        preview=_load(folder,revision['artifacts']['preview_receipt'],'preview-receipt')
        copy(folder,revision['artifacts']['preview_receipt'],stage,'preview/receipt.json')
        for reference in preview['images']:copy(folder/'preview',reference,stage,'preview/'+reference['relative_path'])
        visual={'schema_version':'AnalysisVisualReview/v1',**identity,'operator':'assistant','status':'reviewed','gui_receipt_sha256':gui_ref['sha256'],'images':self._images(context,portable=True),'statement':'I inspected the exact registered native figures, their framing, fields, units, legends, step, recovery and deformation display.','human_mouse_actions':'not_claimed','reviewed_at':_timestamp()}
        visual_ref=_put(stage,'visual.json',visual,'visual-review')
        attempt=context['attempt']
        conclusion='criteria_evaluated' if attempt['criteria']['checks'] and attempt['criteria']['criteria_status'] in ('passed','failed') else 'screening_only'
        result={'schema_version':'AnalysisResult/v1',**identity,'status':'validated','numerical_status':attempt['numerical']['numerical_status'],'criteria_status':attempt['criteria']['criteria_status'],'applicability_status':attempt['numerical']['applicability_status'],'currentness':context['currentness']['currentness'],'snapshot_integrity':'passed','spec_ref':spec_ref,'attempt_ref':attempt_ref,'gui_ref':gui_ref,'visual_ref':visual_ref,'conclusion_level':conclusion,'engineering_certification':False}
        result_ref=_put(stage,'report.json',result,'result')
        image_map={case['case_id']:[ref['relative_path'] for ref in visual['images'] if '/'+case['case_id']+'/' in ref['relative_path']] for case in context['spec']['cases']}
        secure_fs.atomic_publish_new(stage/'report.md',build_report(context['spec'],result,{'attempt':attempt,'images':image_map}).encode('utf-8'))
        artifacts=_inventory(stage)
        dependencies={'report.json':['spec.json','attempt/attempt.json','gui/receipt.json','visual.json'],'report.md':['report.json','attempt/attempt.json'], 'visual.json':[ref['relative_path'] for ref in visual['images']]}
        validate_dependency_graph(artifacts,dependencies)
        manifest={'schema_version':'AnalysisManifest/v1',**identity,'status':'complete','delivery_version':version,'artifacts':artifacts,'dependencies':dependencies,'case_ids':[case['case_id'] for case in context['spec']['cases']],'result_ref':result_ref,'created_at':_timestamp()}
        manifest_ref=_put(stage,'manifest.json',manifest,'manifest')
        # This is the final file written into the immutable staged delivery.
        _put(stage,'completion.json',{'schema_version':'AnalysisCompletion/v1',**identity,'status':'complete','manifest_ref':manifest_ref,'result_ref':result_ref,'delivery_version':version,'completed_at':_timestamp()},'completion')
