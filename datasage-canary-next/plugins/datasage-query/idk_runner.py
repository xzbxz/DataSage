"""Weekly IDK plans; delivery stays in the shared Progress/transport boundary."""
import json,re
from datetime import datetime,timedelta
from . import workflow_io as io,legacy_workflow as wf,operations
from .idk_batch import IdkBatchStore,IdkBatchError,batch_root
from .idk_content import build_idk_parts,IdkContentError


def _validate_observation(doc,op):
    if not isinstance(doc,dict) or doc.get('status')!='success' or doc.get('kind')!='idk_unpriced' or doc.get('selection_complete') is not True:
        raise io.IOErrorBoundary('IDK_OBSERVATION_INCOMPLETE')
    rows=doc.get('records')
    if not isinstance(rows,list) or type(doc.get('source_rows')) is not int or doc['source_rows']!=len(rows):raise io.IOErrorBoundary('IDK_SOURCE_COUNT_INVALID')
    if doc.get('window_days')!=op.get('window_days',0):raise io.IOErrorBoundary('IDK_WINDOW_SCOPE_MISMATCH')
    identities=[]
    for row in rows:
        if not isinstance(row,dict) or type(row.get('source_ref')) not in (str,int) or not str(row['source_ref']).strip() or row.get('price_state') not in ('null','zero','negative'):
            raise io.IOErrorBoundary('IDK_SOURCE_IDENTITY_OR_PRICE_INVALID')
        quantity=wf.number(row.get('quantity'))
        if quantity is None or not quantity.is_finite() or quantity<=10:raise io.IOErrorBoundary('IDK_SOURCE_QUANTITY_INVALID')
        identities.append(wf.canonical(row['source_ref']))
    if len(set(identities))!=len(identities):raise io.IOErrorBoundary('IDK_SOURCE_IDENTITY_INVALID')
    try:
        at=datetime.fromisoformat(doc['observed_at'])
        if op.get('window_days') and datetime.fromisoformat(doc['window_start'])!=at-timedelta(days=op['window_days']):raise ValueError()
    except (KeyError,TypeError,ValueError):raise io.IOErrorBoundary('IDK_OBSERVATION_CLOCK_OR_WINDOW_INVALID') from None
    return rows


def _recipients(profile,binding):
    roles=io.read_recipients(profile,binding)
    executors=roles.get('regions',{}).get('IDK',{}).get('executors',[])
    if not isinstance(executors,list) or not executors:raise io.IOErrorBoundary('IDK_EXECUTOR_PLAN_EMPTY')
    accounts=[]
    for entry in executors:
        account=entry.get('account') if isinstance(entry,dict) else None
        if not isinstance(account,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}',account) or account.lower()=='@all':raise io.IOErrorBoundary('IDK_EXECUTOR_INVALID')
        if account not in accounts:accounts.append(account)
    current=binding.get('target_map') or {}
    if not isinstance(current,dict) or any(not isinstance(current.get(a),dict) or not current[a] for a in accounts):raise io.IOErrorBoundary('IDK_EXPLICIT_TARGET_MAPPING_REQUIRED')
    return accounts,{a:current[a] for a in accounts}


def _format(target):
    platform=target.get('platform')
    if platform not in ('wecom','wecom_callback','wecom_app_http'):raise io.IOErrorBoundary('IDK_TARGET_PLATFORM_INVALID')
    return 'text' if platform=='wecom_callback' else 'markdown'


def _components(content,week):
    scope=wf.digest(['sealed-idk-v1',week])
    items=[]
    for account in content['accounts']:
        for number,body in enumerate(content['parts'],1):
            item=io.component(account,'text',body,scope,'idk-part-'+str(number))
            item['message_format']=_format(content['target_map'][account]);items.append(item)
    return items


def _check_targets(content,binding):
    current=binding.get('target_map') or {}
    if not isinstance(current,dict) or any(current.get(a)!=v for a,v in content['target_map'].items()):raise io.IOErrorBoundary('IDK_SEALED_TARGET_MAPPING_CHANGED')


def _complete(items,progress,transport):
    if not items:return False
    io.preflight_components(items,transport,progress)
    if not all(progress.status(i['key'])=='provider_accepted' for i in items):return False
    groups={}
    for item in items:groups.setdefault(item['notification_key'],[]).append(item)
    for key,group in groups.items():
        pairs=[[item['key'],progress.data['components'][item['key']].get('fingerprint')] for item in group]
        if any(not isinstance(fp,str) or not fp for _,fp in pairs) or progress.data.get('notification_manifests',{}).get(key)!=wf.digest(pairs):raise io.IOErrorBoundary('IDK_ACCEPTED_RECEIPT_BINDING_INVALID')
    return True


def run(profile,binding,out,week,progress,transport):
    try:return _run(profile,binding,out,week,progress,transport)
    except (IdkBatchError,IdkContentError) as exc:raise io.IOErrorBoundary(str(exc)) from exc


def _run(profile,binding,out,week,progress,transport):
    if progress.scope!={'job':'idk','period':week}:raise io.IOErrorBoundary('IDK_PROGRESS_SCOPE_MISMATCH')
    if any(not isinstance(v,dict) or v.get('status') not in ('not_attempted','failed','provider_accepted') for v in progress.data['components'].values()):raise io.IOErrorBoundary('DELIVERY_UNKNOWN_REVIEW_REQUIRED')
    send=binding.get('send_enabled') is True;force=binding.get('_force_resend') is True
    store=IdkBatchStore(batch_root(profile),week);reused=store.exists()
    if force and (not send or not reused):raise io.IOErrorBoundary('IDK_SEALED_COMPLETED_BATCH_REQUIRED')
    if reused:
        envelope=store.load_envelope();content=envelope['content'];manifest=envelope['manifest']
        if not isinstance(content,dict) or set(content)!={'version','week','observation','operation','parts','summary','accounts','target_map'} or content['version']!=1 or content['week']!=week:raise io.IOErrorBoundary('IDK_SEALED_CONTENT_INVALID')
        _validate_observation(content['observation'],content['operation'])
        if content['accounts']!=manifest['logical_accounts'] or content['target_map']!=manifest['target_map'] or bool(content['observation']['source_rows']==0)!=manifest['zero'] or bool(content['parts'])==manifest['zero']:raise io.IOErrorBoundary('IDK_SEALED_CONTENT_BINDING_INVALID')
        if send:_check_targets(content,binding)
    else:
        if any(progress.data.get(k) for k in ('components','notification_manifests','notification_history','idk_batch_seal')):raise io.IOErrorBoundary('IDK_LEGACY_PROGRESS_WITHOUT_BATCH')
        accounts,targets=_recipients(profile,binding) if send else ([],{})
        probes=[{**io.component(a,'text','',week,'idk'),'message_format':_format(targets[a])} for a in accounts]
        if send:transport.preflight(probes)
        op=operations.validate_binding(binding.get('operation') or {'kind':'idk_unpriced','limit':10000})
        if op['kind']!='idk_unpriced':raise io.IOErrorBoundary('OPERATION_KIND_MISMATCH')
        document=operations.execute(profile,wf.policy()['jobs']['idk']['report_id'],op)
        rows=_validate_observation(document,op)
        budget=min([2000 if _format(t)=='text' else 4000 for t in targets.values()] or [4000])
        rendered=build_idk_parts(rows,observed_at=document['observed_at'],window_days=op.get('window_days',0),window_start=document.get('window_start'),max_bytes=budget)
        content={'version':1,'week':week,'observation':document,'operation':op,'parts':list(rendered.parts),'summary':rendered.summary,'accounts':accounts,'target_map':targets}
        items=_components(content,week)
        if send:
            normalized=io.preflight_components(items,transport,progress) if items else []
            if normalized!=items:raise io.IOErrorBoundary('IDK_UNEXPECTED_TRANSPORT_RESEGMENTATION')
            store.prepare(wf.canonical(content),observed_at=document['observed_at'],logical_accounts=accounts,target_map=targets,evidence=rendered.summary,zero=not rows,progress=progress)
            manifest=store.load()
    items=_components(content,week)
    if send:
        seal=manifest['metadata_seal'];old=progress.data.get('idk_batch_seal')
        if old is not None and old!=seal:raise io.IOErrorBoundary('IDK_PROGRESS_SEAL_CHANGED')
        if old is None:
            if progress.data['components']:raise io.IOErrorBoundary('IDK_RECEIPT_BINDING_MISSING')
            progress.data['idk_batch_seal']=seal;operations._atomic(progress.path,progress.data)
    state='prepared_no_delivery_requested'
    if send and not content['observation']['source_rows']:
        if force:raise io.IOErrorBoundary('IDK_ZERO_BATCH_NOT_RESENDABLE')
        state='empty_no_task'
    elif send:
        complete=_complete(items,progress,transport)
        if force:
            if not complete:raise io.IOErrorBoundary('IDK_INCOMPLETE_BATCH_CANNOT_BE_BYPASSED')
            reason=binding.get('_replay_reason')
            if not isinstance(reason,str) or not 5<=len(reason.strip())<=120:raise io.IOErrorBoundary('EXPLICIT_GENERATION_REASON_REQUIRED')
        if complete and not force:state='previous_provider_acceptance_reused'
        else:
            io.deliver_components(items,transport,progress,enabled=True,force=force)
            state='provider_accepted'
    operations._atomic(out/'idk-preview.json',content)
    return {'status':'success','job':'idk','period':week,'source_rows':content['observation']['source_rows'],'components':len(items),'delivery_state':state,'delivery':state if send else 'not_requested','sealed':send or reused,'reused':reused,'prepared_only':not send,'outputs':[str(out/'idk-preview.json')],'baseline_state':'not_used_for_idk_reminder','baseline_accepted':False}
