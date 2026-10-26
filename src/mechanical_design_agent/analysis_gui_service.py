"""Register immutable GUI requests and verify only their owned evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from . import secure_fs
from .analysis_gui import _bytes,preview_native,rebuild_native,verify_native_receipt
from .analysis_protocol import canonical_record,parse_record,record_sha256
from .analysis_revision import read_artifact,read_frozen_revision,inventory
from .analysis_runs import read_run,run_root
from .analysis_session import _tools
from .analysis_store import _timestamp
from .analysis_types import AnalysisError
from .analysis_native_transaction import NativeTransaction
from .workspace_bootstrap import read_workspace_manifest


def _load(root,reference,schema=None):
    text=read_artifact(root,reference).content.decode('utf-8')
    return parse_record(text,schema_name=schema) if schema else json.loads(text)


def _put(root,name,record,schema=None):
    content=canonical_record(record,schema_name=schema) if schema else _bytes(record)
    secure_fs.atomic_publish_new(root/name,content)
    return {'relative_path':name,'sha256':hashlib.sha256(content).hexdigest()}


def _authorize(store,scope):
    identity=read_workspace_manifest(store.workspace).raw['identity']
    if any(scope[key]!=identity[key] for key in ('organization_id','design_group_id')):raise AnalysisError('ANALYSIS_AUTHORITY_CHANGED','GUI evidence belongs to another authorized workspace scope')


class AnalysisGuiService:
    def __init__(self,store,*,configuration_loader=None,controller=None,guard=None):
        self.store=store;self.configuration_loader=configuration_loader
        self.controller=controller;self.guard=guard

    def issue_request(self,*,design_id,analysis_id,kind,spec_sha256,run_id=None):
        if kind not in ('preview','final_result'):raise AnalysisError('ANALYSIS_GUI_KIND_INVALID','only preview and final_result are supported')
        state=self.store.read_state(design_id,analysis_id);attempt_ref=None
        if kind=='preview':
            if run_id is not None:raise AnalysisError('ANALYSIS_GUI_KIND_INVALID','candidate preview has no solver run')
            revision_ref=state['current_revision']
        else:
            if run_id is None:raise AnalysisError('ANALYSIS_GUI_RUN_REQUIRED','final result requires a validated run')
            run,execution=read_run(self.store,design_id,analysis_id,run_id)
            owned_validation=run['status']=='validating' and run['worker'] is not None and run['worker']['pid']==os.getpid()
            if (not owned_validation and run['status'] not in ('awaiting_visual_review','completed')) or run.get('attempt_ref') is None:raise AnalysisError('ANALYSIS_GUI_RUN_UNVALIDATED','numerical and integrity evidence must exist before final GUI reconstruction')
            revision_ref=execution['revision_ref'];attempt_ref=run['attempt_ref']
        folder,revision=read_frozen_revision(self.store,design_id,analysis_id,revision_ref)
        _authorize(self.store,_load(folder,revision['artifacts']['knowledge_receipt'],'knowledge-receipt')['scope'])
        spec=_load(folder,revision['artifacts']['spec'],'spec')
        if record_sha256(spec,schema_name='spec')!=spec_sha256 or revision['issues'] or spec['status'] not in ('ready_for_review','approved') or kind=='final_result' and spec['status']!='approved':raise AnalysisError('ANALYSIS_GUI_SPEC_CHANGED','GUI request must bind the exact ready candidate or approved result specification')
        if kind=='final_result':self._attempt(design_id,analysis_id,run_id,attempt_ref,execution)
        root=self.store._root(design_id,analysis_id)
        identifier=uuid.uuid4().hex;target=secure_fs.ensure_managed_directory(root/'gui'/('g-'+identifier),parents=True,exist_ok=False).path
        record={'schema_version':'AnalysisGuiRequest/v1','design_id':design_id,'analysis_id':analysis_id,'run_id':run_id,'spec_sha256':spec_sha256,'request_id':identifier,'kind':kind,'status':'prepared','revision_ref':revision_ref,'attempt_ref':attempt_ref,'created_at':_timestamp()}
        reference=_put(target,'request.json',record,'gui-request');reference['relative_path']=(target/'request.json').relative_to(root).as_posix()
        return {'request':record,'request_ref':reference}

    def _registered(self,envelope):
        request=parse_record(canonical_record(envelope['request'],schema_name='gui-request').decode(),schema_name='gui-request')
        root=self.store._root(request['design_id'],request['analysis_id'])
        if request['kind']=='preview' and self.store.read_state(request['design_id'],request['analysis_id'])['current_revision']!=request['revision_ref']:raise AnalysisError('ANALYSIS_GUI_SPEC_CHANGED','a changed candidate needs a new registered preview')
        expected='gui/g-'+request['request_id']+'/request.json'
        if envelope['request_ref']['relative_path']!=expected or _load(root,envelope['request_ref'],'gui-request')!=request:raise AnalysisError('ANALYSIS_GUI_REQUEST_UNREGISTERED','only the immutable preregistered GUI request may produce or verify evidence')
        folder,revision=read_frozen_revision(self.store,request['design_id'],request['analysis_id'],request['revision_ref'])
        _authorize(self.store,_load(folder,revision['artifacts']['knowledge_receipt'],'knowledge-receipt')['scope'])
        spec=_load(folder,revision['artifacts']['spec'],'spec')
        if record_sha256(spec,schema_name='spec')!=request['spec_sha256']:raise AnalysisError('ANALYSIS_GUI_SPEC_CHANGED','registered GUI request belongs to another exact specification')
        return request,root,root/Path(expected).parent,folder,revision,spec

    def _attempt(self,design_id,analysis_id,run_id,reference,execution=None,*,complete=True):
        run,stored=read_run(self.store,design_id,analysis_id,run_id);execution=stored if execution is None else execution
        _authorize(self.store,execution['scope'])
        root=run_root(self.store,design_id,analysis_id,run_id)
        attempt=_load(root,reference)
        if run.get('attempt_ref')!=reference or stored!=execution or attempt.get('schema_version')!='AnalysisAttemptEvidence/v1' or attempt.get('native_integrity_sealed') is not True or attempt.get('request_sha256')!=run['request_ref']['sha256'] or any(attempt.get(key)!=execution[key] for key in ('spec_sha256','executor_sha256','runtime_manifest_sha256')) or attempt.get('numerical',{}).get('numerical_status')!='passed' or attempt['numerical']['applicability_status']=='outside_scope':raise AnalysisError('ANALYSIS_GUI_RUN_UNVALIDATED','GUI cannot replace missing, failed or unsealed numerical evidence')
        attempt_root=root/Path(reference['relative_path']).parent
        if complete and [ref for ref in inventory(attempt_root) if ref['relative_path']!='attempt.json']!=attempt.get('files'):raise AnalysisError('ANALYSIS_ATTEMPT_CHANGED','complete sealed attempt file inventory changed')
        return attempt_root,attempt

    def _case_inputs(self,request,case_id,folder,revision):
        root,attempt=self._attempt(request['design_id'],request['analysis_id'],request['run_id'],request['attempt_ref'],complete=False)
        rows=[item for item in attempt['numerical']['finest'] if item['case_id']==case_id]
        if len(rows)!=1:raise AnalysisError('ANALYSIS_GUI_CASE_CHANGED','each declared case needs one validated finest mesh')
        row=rows[0];level=row['level']
        deck=_load(root,row['artifacts']['deck']);fields=_load(root,row['artifacts']['fields']);solver=_load(root,row['artifacts']['solver'])
        mesh=attempt['meshes'][str(level)]
        path=root/'cases'/case_id/('level-'+str(level).zfill(3))
        if Path(deck['directory'])!=path/'deck' or Path(solver['directory'])!=path/'solver' or Path(mesh['directory'])!=root/'meshes'/('level-'+str(level).zfill(3)) or deck['spec_sha256']!=request['spec_sha256'] or fields['spec_sha256']!=request['spec_sha256'] or fields['case_id']!=case_id:raise AnalysisError('ANALYSIS_GUI_ARTIFACT_UNREGISTERED','GUI inputs must remain within their exact validated case and level')
        for reference in mesh['receipt']['artifacts'].values():read_artifact(Path(mesh['directory']),reference)
        read_artifact(Path(deck['directory']),deck['input_ref'])
        geometry=_load(folder,revision['artifacts']['geometry'],'geometry');regions=_load(folder,revision['artifacts']['regions'],'region-bindings')
        if mesh['geometry_manifest']!=geometry or mesh['region_bindings']!=regions:raise AnalysisError('ANALYSIS_GUI_GEOMETRY_CHANGED','result mesh represents another prepared geometry or physical mapping')
        return deck,mesh,fields,solver,folder/revision['artifacts']['geometry_brep']['relative_path']

    def execute_request(self,envelope):
        request,root,target,folder,revision,spec=self._registered(envelope)
        if self.guard is not None:self.guard.track_root(target)
        if (target/'receipt.json').exists():
            reference={'relative_path':(target/'receipt.json').relative_to(root).as_posix(),'sha256':secure_fs.read_managed_file(target/'receipt.json').sha256}
            return self.verify_receipt(envelope,reference)
        if self.configuration_loader is None:raise AnalysisError('FEA_CONFIG_MISSING','native GUI execution requires the qualified configuration loader')
        settings=self.configuration_loader()
        if _tools(settings,spec)!=_load(folder,revision['artifacts']['tools']):raise AnalysisError('ANALYSIS_TOOL_CONSTRAINT_CHANGED','GUI must use the exact reviewed isolated native tools')
        cases=[];preview_ref=None
        output=secure_fs.ensure_managed_directory(target/'outputs',parents=False,exist_ok=False).path
        if request['kind']=='preview':
            geometry=_load(folder,revision['artifacts']['geometry'],'geometry');regions=_load(folder,revision['artifacts']['regions'],'region-bindings')
            preview_native({'spec':spec,'spec_sha256':request['spec_sha256'],'geometry':geometry,'regions':regions,'settings':settings,'output_dir':str(output/'preview')},guard=self.guard,geometry_directory=folder/'geometry')
            preview_ref={'relative_path':(output/'preview/preview.json').relative_to(root).as_posix(),'sha256':secure_fs.read_managed_file(output/'preview/preview.json').sha256}
        else:
            pending=[];checkpoint=self.guard.checkpoint if self.guard is not None else lambda:None
            with NativeTransaction(settings,controller=self.controller,checkpoint=checkpoint,budget_guard=self.guard) as native:
                for case in spec['cases']:
                    inputs=self._case_inputs(request,case['case_id'],folder,revision)
                    binding={'request_id':request['request_id'],'request_sha256':envelope['request_ref']['sha256'],'run_id':request['run_id'],'design_id':request['design_id'],'analysis_id':request['analysis_id']}
                    evidence=rebuild_native(*inputs[:4],inputs[4],settings,output/case['case_id'],transaction=native,guard=self.guard,binding=binding)
                    pending.append((case['case_id'],{key:value for key,value in evidence.items() if key!='evidence_ref'}))
                native.seal();checkpoint()
            for case_id,evidence in pending:
                evidence['native_integrity_sealed']=True
                reference=_put(output/case_id,'evidence.json',evidence)
                reference['relative_path']=(output/case_id/'evidence.json').relative_to(root).as_posix()
                cases.append({'case_id':case_id,'evidence_ref':reference})
        receipt={key:request[key] for key in ('design_id','analysis_id','run_id','spec_sha256','request_id','kind')}
        receipt.update(schema_version='AnalysisGuiReceipt/v1',status='completed',request_ref=envelope['request_ref'],cases=cases,preview_ref=preview_ref)
        reference=_put(target,'receipt.json',receipt,'gui-receipt');reference['relative_path']=(target/'receipt.json').relative_to(root).as_posix()
        return self.verify_receipt(envelope,reference)

    def verify_receipt(self,envelope,receipt_ref):
        request,root,target,folder,revision,spec=self._registered(envelope)
        if type(receipt_ref) is str:
            if receipt_ref!=(target/'receipt.json').relative_to(root).as_posix():raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','receipt belongs to another registered GUI request')
            receipt_ref={'relative_path':receipt_ref,'sha256':secure_fs.read_managed_file(root/receipt_ref).sha256}
        if receipt_ref['relative_path']!=(target/'receipt.json').relative_to(root).as_posix():raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','receipt belongs to another registered GUI request')
        receipt=_load(root,receipt_ref,'gui-receipt')
        if receipt['status']!='completed' or receipt['request_ref']!=envelope['request_ref'] or any(receipt[key]!=request[key] for key in ('design_id','analysis_id','run_id','spec_sha256','request_id','kind')):raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','GUI receipt does not represent the exact registered request')
        if request['kind']=='preview':
            preview=_load(root,receipt['preview_ref'],'preview-receipt')
            if receipt['cases'] or preview['spec_sha256']!=request['spec_sha256'] or preview['status']!='completed':raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','candidate preview is incomplete')
            image_root=root/Path(receipt['preview_ref']['relative_path']).parent
            for reference in preview['images']:read_artifact(image_root,reference)
        else:
            self._attempt(request['design_id'],request['analysis_id'],request['run_id'],request['attempt_ref'])
            if receipt['preview_ref'] is not None or [item['case_id'] for item in receipt['cases']]!=[case['case_id'] for case in spec['cases']]:raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','result GUI must cover all and only declared cases')
            for case in receipt['cases']:
                expected=target/'outputs'/case['case_id']/'evidence.json'
                if case['evidence_ref']['relative_path']!=expected.relative_to(root).as_posix():raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','case evidence is outside its registered producer')
                evidence=_load(root,case['evidence_ref']);directory=expected.parent
                deck,mesh,fields,solver,geometry=self._case_inputs(request,case['case_id'],folder,revision)
                native_request=json.loads(secure_fs.read_managed_file(directory/'request.json').content)
                report=json.loads(secure_fs.read_managed_file(directory/'response.json').content)
                binding={'request_id':request['request_id'],'request_sha256':envelope['request_ref']['sha256'],'run_id':request['run_id'],'design_id':request['design_id'],'analysis_id':request['analysis_id']}
                if native_request.get('identity',{}).get('registration')!=binding:raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','native GUI evidence belongs to another request or solver run')
                checked=verify_native_receipt(report,request=native_request,root=directory,deck=deck,fields=fields)
                if evidence!={**checked,'native_integrity_sealed':True}:raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','sealed native evidence or displayed fields changed')
        return {'receipt':receipt,'receipt_ref':receipt_ref}
