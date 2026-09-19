"""Sealed weekly/monthly reports using the existing readers, artifacts and delivery ledger."""
from datetime import datetime,timezone
import hashlib,json
from . import legacy_workflow as wf,operations,slow_report_batch as batches
from . import workflow_io as io

UNKNOWN={'unknown','in_flight','unverified_success','not_delivered'}

def load_phase(store,head,phase):
    path=batches.batch_path(store.root,store.week,phase,head['content_generation'])
    if not path.exists() and phase not in head['phases']:return None
    manifest=store.load_phase(phase)
    raw=(path/'body.txt').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['body']['sha256']:raise io.IOErrorBoundary('REPORT_BODY_SEAL_CHANGED')
    try:body=json.loads(raw)
    except (ValueError,UnicodeError):raise io.IOErrorBoundary('REPORT_BODY_INVALID') from None
    required={'version','week','month','phase','period','documents'}
    if not isinstance(body,dict) or set(body)!=required or body['version']!=1 or (body['week'],body['month'],body['phase'])!=(store.week,head['month'],phase):raise io.IOErrorBoundary('REPORT_BODY_SCOPE_INVALID')
    if body['period']!=(store.week if phase=='weekly' else head['month']) or not isinstance(body['documents'],list) or not body['documents']:raise io.IOErrorBoundary('REPORT_BODY_PERIOD_INVALID')
    regions=[];accounts=set();files={item['name']:item for item in manifest['attachments']}
    for doc in body['documents']:
        if not isinstance(doc,dict) or set(doc)!={'region','text','accounts','attachment','evidence'}:raise io.IOErrorBoundary('REPORT_DOCUMENT_INVALID')
        if not isinstance(doc['region'],str) or not isinstance(doc['text'],str) or not doc['text'] or not isinstance(doc['accounts'],list) or not doc['accounts'] or any(not isinstance(a,str) or not a.strip() for a in doc['accounts']) or len(doc['accounts'])!=len(set(doc['accounts'])):raise io.IOErrorBoundary('REPORT_RECIPIENT_PLAN_INVALID')
        if doc['attachment'] not in files:raise io.IOErrorBoundary('REPORT_ATTACHMENT_BINDING_MISSING')
        regions.append(doc['region']);accounts.update(doc['accounts'])
    if regions!=manifest['regions'] or len(regions)!=len(set(regions)) or accounts!=set(manifest['logical_accounts']) or {d['attachment'] for d in body['documents']}!=set(files):raise io.IOErrorBoundary('REPORT_PHASE_POPULATION_MISMATCH')
    return {'manifest':manifest,'body':body,'directory':path}

def parts(data,head,phase):
    result=[]
    for doc in data['body']['documents']:
        scope=wf.digest(['sealed-slow-report-v1',data['body']['week'],head['content_generation'],head['delivery_generation'],phase,doc['region'],data['body']['period']])
        for account in doc['accounts']:
            result.extend([io.component(account,'text',doc['text'],scope,phase+'_text'),io.component(account,'file',data['directory']/'attachments'/doc['attachment'],scope,phase+'_file')])
    return result

def phase_complete(data,head,phase,transport,progress):
    if data is None or data['manifest']['status']!='published':return False
    items=parts(data,head,phase)
    if transport is not None and hasattr(transport,'normalize'):items=transport.normalize(items,progress)
    marker=f"{head['content_generation']}:{phase}"
    bound=progress.data.get('report_phase_bindings',{}).get(marker)
    if bound!=data['manifest']['metadata_seal']:
        if any(progress.status(item['key'])!='not_attempted' for item in items):raise io.IOErrorBoundary('REPORT_PHASE_RECEIPT_BINDING_MISSING')
        return False
    complete=bool(items) and all(progress.status(item['key'])=='provider_accepted' for item in items)
    if complete:
        groups={}
        for item in items:groups.setdefault(item['notification_key'],[]).append(item)
        for notification,group in groups.items():
            fingerprints=[progress.data['components'][i['key']].get('fingerprint') for i in group]
            if any(not isinstance(value,str) or not value for value in fingerprints):raise io.IOErrorBoundary('REPORT_ACCEPTED_FINGERPRINT_MISSING')
            expected=wf.digest([[item['key'],fp] for item,fp in zip(group,fingerprints)])
            if progress.data.get('notification_manifests',{}).get(notification)!=expected:raise io.IOErrorBoundary('REPORT_ACCEPTED_MANIFEST_MISMATCH')
    return complete

def check_targets(data,binding):
    current=binding.get('target_map') or {}
    if not isinstance(current,dict) or any(a not in current or current[a]!=v for a,v in data['manifest']['target_map'].items()):raise io.IOErrorBoundary('REPORT_SEALED_TARGET_MAPPING_CHANGED')

def prepare_phase(profile,store,head,phase,binding,out,snapshots,transport,progress,weekly=None):
    from . import report_evidence,workflow_inputs as inputs
    from .legacy_xlsx import gen_workbook_xlsx
    if weekly is not None:
        plan={d['region']:list(d['accounts']) for d in weekly['body']['documents']};targets=dict(weekly['manifest']['target_map'])
    else:
        roles=io.read_recipients(profile,binding)
        plan={region:wf.report_recipients(roles['regions'],region) for region in wf.policy()['regions']}
        if not plan or any(not accounts for accounts in plan.values()):raise io.IOErrorBoundary('REPORT_RECIPIENT_PLAN_EMPTY')
        current=binding.get('target_map') or {};accounts={a for values in plan.values() for a in values}
        if not isinstance(current,dict) or any(a not in current or not isinstance(current[a],dict) or not current[a] for a in accounts):raise io.IOErrorBoundary('REPORT_EXPLICIT_TARGET_MAPPING_REQUIRED')
        targets={a:current[a] for a in sorted(accounts)}
    period=store.week if phase=='weekly' else head['month'];reports={}
    for region in plan:
        evidence,labels=report_evidence.collect(region,period,phase,store.week,snapshots=snapshots)
        packets=evidence['packets'];adapted=inputs.legacy_report_packet(packets['pool'],packets['flow'],labels,region,period,evidence=evidence)
        if adapted.get('label_coverage',{}).get('complete') is not True:raise io.IOErrorBoundary('REPORT_LABEL_REVIEW_REQUIRED')
        reports[region]=adapted
    documents=[];attachments={};items=[]
    for region,adapted in reports.items():
        text=wf.report_draft(region,adapted['period'],adapted['summary'],adapted['sales_rows'],monthly=phase=='monthly',weekly_start=adapted.get('completeness',{}).get('frozen_at'),detail_semantics=adapted.get('detail_semantics'))
        text+='\n'+adapted['completeness']['as_of_label']+'：'+adapted['completeness']['observed_to']+'\nSKU counts use registered SKU/department/unit groups.'
        name=region+'_'+phase+'.xlsx';path=out/name
        gen_workbook_xlsx([('Detail',wf.REPORT_HEADERS,wf.legacy_detail_rows_for_xlsx(adapted['detail_rows'])),wf.REPORT_NOTES_SHEET],path,borders=True,landscape=True,legacy_layout=True)
        attachments[name]=path;documents.append({'region':region,'text':text,'accounts':plan[region],'attachment':name,'evidence':adapted})
        scope=wf.digest(['sealed-slow-report-v1',store.week,head['content_generation'],head['delivery_generation'],phase,region,period])
        for account in plan[region]:items.extend([io.component(account,'text',text,scope,phase+'_text'),io.component(account,'file',path,scope,phase+'_file')])
    if binding.get('send_enabled'):
        if any((binding.get('target_map') or {}).get(a)!=v for a,v in targets.items()):raise io.IOErrorBoundary('REPORT_SEALED_TARGET_MAPPING_CHANGED')
        io.preflight_components(items,transport,progress)
    body={'version':1,'week':store.week,'month':head['month'],'phase':phase,'period':period,'documents':documents}
    store.prepare_phase(phase,body=wf.canonical(body),attachments=attachments,logical_accounts=sorted(targets),target_map=targets,
        observed_at=datetime.now(timezone.utc).isoformat(),regions=list(plan),evidence={
            'observation_model':'independent_department_snapshots','meaning':'month_to_date_progress_not_month_end_close' if phase=='monthly' else 'weekly_progress',
            'department_observations':{r:d['completeness'] for r,d in reports.items()}})
    store.publish_phase(phase)
    return load_phase(store,store.head(),phase)

def run(profile,binding,out,week,month,progress,snapshots,transport):
    """Actual slow_report seam. No alternative sender, database writer or scheduler."""
    if progress.scope!={'job':'slow_report','period':week}:raise io.IOErrorBoundary('REPORT_PROGRESS_SCOPE_MISMATCH')
    if any(not isinstance(v,dict) or v.get('status') not in {'not_attempted','provider_accepted','failed'} for v in progress.data['components'].values()):raise io.IOErrorBoundary('DELIVERY_UNKNOWN_REVIEW_REQUIRED')
    if 'report_phase_bindings' in progress.data and not isinstance(progress.data['report_phase_bindings'],dict):raise io.IOErrorBoundary('REPORT_PHASE_BINDINGS_INVALID')
    store=batches.BatchStore(batches.batch_root(profile),week);head=store.head()
    if head is None:
        if binding.get('_resume_report_week'):raise io.IOErrorBoundary('REPORT_RESUME_BATCH_MISSING')
        if binding.get('_force_resend') or binding.get('_regenerate_report'):raise io.IOErrorBoundary('REPORT_SEALED_BATCH_REQUIRED')
        if any(progress.data.get(k) for k in ('components','notification_manifests','notification_history','report_phase_bindings')):raise io.IOErrorBoundary('LEGACY_REPORT_PROGRESS_REQUIRES_BINDING')
        head=store.ensure_head(month=month,observed_at=datetime.now(timezone.utc).isoformat())
    loaded={phase:load_phase(store,head,phase) for phase in ('weekly','monthly')}
    if binding.get('_force_resend') or binding.get('_regenerate_report'):
        if not all(phase_complete(loaded[p],head,p,transport,progress) for p in loaded):raise io.IOErrorBoundary('INCOMPLETE_REPORT_BATCH_CANNOT_BE_BYPASSED')
        reason=binding.get('_replay_reason')
        if not isinstance(reason,str) or not 5<=len(reason.strip())<=120:raise io.IOErrorBoundary('EXPLICIT_GENERATION_REASON_REQUIRED')
        if binding.get('_regenerate_report'):
            head=store.new_content_generation(month=head['month'],reason=reason);loaded={'weekly':None,'monthly':None}
        else:
            for phase,data in loaded.items():
                check_targets(data,binding)
                if binding.get('send_enabled'):io.preflight_components(parts(data,head,phase),transport,progress)
            head=store.new_delivery_generation(reason=reason)
    outputs=[];states={};failures=[];attempted_notifications=0
    for phase in ('weekly','monthly'):
        data=loaded[phase];marker=f"{head['content_generation']}:{phase}"
        if data is None:
            if marker in progress.data.get('report_phase_bindings',{}):raise io.IOErrorBoundary('REPORT_SEALED_PHASE_MISSING')
            data=prepare_phase(profile,store,head,phase,binding,out,snapshots,transport,progress,loaded['weekly'] if phase=='monthly' else None)
            loaded[phase]=data;head=store.head()
        elif data['manifest']['status']!='published' or head['phases'].get(phase,{}).get('status')!='published':
            if data['manifest']['status']!='published' and binding.get('send_enabled'):
                check_targets(data,binding);io.preflight_components(parts(data,head,phase),transport,progress)
            store.publish_phase(phase);data=load_phase(store,store.head(),phase);loaded[phase]=data;head=store.head()
        sealed=data['manifest']['metadata_seal'];bindings=progress.data.setdefault('report_phase_bindings',{})
        if marker in bindings and bindings[marker]!=sealed:raise io.IOErrorBoundary('REPORT_PHASE_BINDING_CHANGED')
        if marker not in bindings:
            check=parts(data,head,phase)
            if transport is not None and hasattr(transport,'normalize'):check=transport.normalize(check,progress)
            if any(progress.status(item['key'])!='not_attempted' for item in check):raise io.IOErrorBoundary('REPORT_PHASE_RECEIPT_BINDING_MISSING')
            bindings[marker]=sealed;operations._atomic(progress.path,progress.data)
        items=parts(data,head,phase);outputs.extend(i['path'] for i in items if i['kind']=='file')
        if not binding.get('send_enabled'):
            states[phase]='prepared_no_delivery_requested';continue
        if phase_complete(data,head,phase,transport,progress):
            states[phase]='provider_accepted';continue
        check_targets(data,binding)
        normalized=io.preflight_components(items,transport,progress);groups={}
        for item in normalized:groups.setdefault(item['notification_key'],[]).append(item)
        for notification,group in groups.items():
            if all(progress.status(item['key'])=='provider_accepted' for item in group):continue
            attempted_notifications+=1
            try:io.deliver_components(group,transport,progress,enabled=True)
            except io.IOErrorBoundary as exc:
                failures.append({'phase':phase,'account':group[0]['account'],'notification':notification,'code':str(exc)})
                operations._atomic(out/'report-failures.json',{'week':week,'content_generation':head['content_generation'],'delivery_generation':head['delivery_generation'],'failures':failures,'external_failure_notice_sent':False})
                if str(exc)!='DELIVERY_COMPONENT_FAILED' or any(v.get('status') in UNKNOWN for v in progress.data['components'].values()):raise
        if not phase_complete(data,head,phase,transport,progress):raise io.IOErrorBoundary('REPORT_'+phase.upper()+'_DELIVERY_INCOMPLETE')
        states[phase]='provider_accepted'
    return {'status':'success','job':'slow_report','week':week,'month':head['month'],'content_generation':head['content_generation'],
        'delivery_generation':head['delivery_generation'],'phases':states,'artifacts':list(dict.fromkeys(outputs)),
        'delivery':('provider_accepted_not_human_read' if attempted_notifications else 'previous_provider_acceptance_reused') if binding.get('send_enabled') else 'not_requested',
        'attempted_notifications':attempted_notifications,
        'prepared_only':not binding.get('send_enabled'),'observation_model':'sealed_independent_department_snapshots','format':'legacy_detail_and_notes'}
