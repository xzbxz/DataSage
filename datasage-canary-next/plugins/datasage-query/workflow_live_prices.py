"""Real-source observations with strict basis gates and owned acceptance snapshots."""
from datetime import datetime
from collections import Counter
from decimal import Decimal
import json,uuid,hashlib
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle
from . import workflow_inputs as inputs,legacy_price_bridge as bridge,operations as op
from . import reminder_acceptance as session,acceptance_delivery as delivery,workflow_io as io

def snapshot_digest(side,rows):return base.digest(live.normalized(side+'_snapshot',cycle.clean(rows)))
def issues(side,before,current,document):
    result=Counter();spec=op.policy()[side];old={bridge.key_of(side,r):r for r in before}
    for event in document['events']:
        if event['event'].startswith('unresolved') or event['event'] in ('recorded_basis_changed','absent_from_current_selection'):result[event['event']]+=1
    for row in current:
        try:key=bridge.key_of(side,row)
        except op.OperationError:continue
        if any(row.get(k) in (None,'') for k in spec['basis']):result['current_basis_missing']+=1;continue
        if side=='sales':
            price=op._number(row.get('ddp_price'))
            if price is not None and price!=price.quantize(Decimal('0.01')):result['snapshot_decimal_precision_insufficient']+=1
        previous=old.get(key)
        if previous is None:continue
        original=previous.get('current_record')
        if isinstance(original,str):original=json.loads(original)
        if original is None:
            # Historic unit/tax values cannot be reconstructed from today's quote.
            if set(spec['basis'])-set(bridge.SPECS[side]['basis']):result['legacy_historical_basis_missing']+=1
        elif any(original.get(k) in (None,'') for k in spec['basis']):result['previous_basis_missing']+=1
        elif any(str(original[k])!=str(row[k]) for k in spec['basis']):result['observed_unit_tax_currency_changed']+=1
    return dict(result)
def observe(store,side):
    spec=bridge.SPECS[side]
    with base.tools._ConsistentSnapshotExecutor(deadline_at=base.tools._call_deadline(None)) as db:
        start=inputs.complete(db,'SELECT NOW(6) AS observed_at,UTC_TIMESTAMP(6) AS observed_utc',limit=1)[0]
        existing=store.rows(side+'_snapshot',side)
        old=None
        if not existing:old=inputs.complete(db,'SELECT '+','.join(spec['fields'])+' FROM '+spec['table']+' ORDER BY id LIMIT 10001')
        sql,args=op.build_observation({'kind':side+'_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000})
        current=inputs.complete(db,sql,args)
        end=inputs.complete(db,'SELECT NOW(6) AS observed_at,UTC_TIMESTAMP(6) AS observed_utc',limit=1)[0]
        at=datetime.fromisoformat(str(end['observed_at']))
        if not current or any(datetime.fromisoformat(str(r['observed_at'])).date()!=at.date() for r in current):raise ValueError('LIVE_PRICE_EMPTY_OR_DAY_CHANGED')
        metadata={'started_at':str(start['observed_at']),'observed_at':at.isoformat(),'observed_utc':str(end['observed_utc']),'snapshot_marker':db.marker,'clock':'database_observation','source_rows':len(current),'source_limit':10000,'regions':['HCM','HN','BKK','IDK']}
    if old is not None:
        if not old:raise ValueError('LEGACY_STARTING_REFERENCE_EMPTY')
        bridge.reference(side,old,at)
        if any(r['cycle_id']=='origin-'+side for r in store.rows('cycles')):raise ValueError('MISSING_SNAPSHOT_WITH_EXISTING_ORIGIN')
        with store.transaction():
            store.insert(side+'_snapshot',[{**r,'test_scope':side,'current_record':None,'reference_kind':'legacy'} for r in old])
            written=[{k:r[k] for k in spec['fields']} for r in store.rows(side+'_snapshot',side)]
            if base.normalized(side+'_snapshot',written)!=base.normalized(side+'_snapshot',old):raise ValueError('LEGACY_REFERENCE_COPY_READBACK_MISMATCH')
            store.cycle('origin-'+side,side,'committed',{'reference':spec['table'],'rows':len(old),'digest':base.digest(old),'snapshot_times':sorted({str(r['snapshot_at']) for r in old}),'copied_at':at.isoformat(),'historical_fields_missing':spec['missing']})
        existing=store.rows(side+'_snapshot',side)
    return cycle.clean(existing),current,at,metadata
def source_notices(side,key,doc):
    folder=io.private_root(delivery.runtime_home(base.profile()))/'wlv'/uuid.uuid4().hex[:8];folder.mkdir(parents=True)
    items,evidence=session.price_event_plan(base.profile(),side+'-observation',doc,{'changes':bridge.changes(doc)},session._reference(base.profile()),folder,all_notices=True)
    for item in items:
        item['body']='【真实来源验收，仅发送到测试目标】\n'+doc['scope_notice']+'\n'+item['body']
        item['role']='真实来源；按旧角色规则生成，测试路由；'+item['role']
    if side=='purchase':items.append({**items[0],'logical_id':items[0]['logical_id']+'-group','channel':'group'})
    live.save(key+'-role-plan.json',evidence)
    return items
def seal_notices(notices):
    files={}
    for notice in notices:
        for path in notice['attachments']:
            _,raw,_,_=delivery.file_snapshot(path,delivery.runtime_home(base.profile()))
            files[path]=hashlib.sha256(raw).hexdigest()
    return {'notice_digest':base.digest(notices),'file_digests':files}
def bounded_dispatch(store,key,scope,data):
    def dispatch(_key,notices,state,_send):
        receipts=data.setdefault('batch_receipts',{});sent=0
        for index in range(0,len(notices),2):
            batch_key=str(index//2)
            if batch_key in receipts:continue
            if sent:raise cycle.DeliveryDeferred('NEXT_BOUNDED_DELIVERY_BATCH_REQUIRED')
            pending_state='sending' if state=='sending' and data.get('active_batch')==batch_key else 'planned'
            data['active_batch']=batch_key;cycle.persist(store,key,scope,'sending',data)
            receipt=cycle.dispatch(key+'-b'+batch_key,notices[index:index+2],pending_state)
            receipts[batch_key]=receipt;sent+=1;cycle.persist(store,key,scope,'planned',data);state='planned'
        return {'status':'provider_accepted_not_human_read','batch_receipts':receipts,'logical_notifications':sum(r['logical_notifications'] for r in receipts.values()),'components':sum(r['components'] for r in receipts.values())}
    return dispatch
def run(side,*,manual=False,allow_send=False):
    if side not in ('sales','purchase'):raise ValueError('LIVE_PRICE_SIDE_INVALID')
    store=live.Store()
    try:
        with live.lock(store,'prices-'+side):
            history=[r for r in store.rows('cycles',side) if r['cycle_id'].startswith('lp-'+side+'-')]
            pending=[r for r in history if r['status'] not in ('committed','blocked')]
            if len(pending)>1:raise ValueError('MULTIPLE_LIVE_PENDING_CYCLES')
            if pending:
                row=pending[0];key=row['cycle_id'];data=cycle.payload(row);state=row['status']
                if state=='unknown':return {'status':'blocked','cycle':key,'reason':'DELIVERY_UNKNOWN_REVIEW_REQUIRED','snapshot_advanced':False}
            else:
                if allow_send:return {'status':'no_prepared_price_delivery','side':side,'new_source_query':False}
                clock=store._read('SELECT NOW(6) AS at')[0]['at']
                hour=clock.strftime('%Y%m%d%H');key='lp-'+side+'-'+(clock.strftime('%Y%m%d%H%M%S%f') if manual else hour)
                done=next((r for r in history if r['cycle_id']==key),None)
                if done:return {'status':'same_hour_already_observed','cycle':key,'previous_status':done['status'],'new_source_query':False,'sent':False}
                try:before,current,at,evidence=observe(store,side)
                except Exception as exc:
                    code=getattr(exc,'code',type(exc).__name__)
                    data={'document':{'event_counts':{}},'observation':{'observed_at':clock.isoformat(),'clock':'database_observation','complete':False},'blockers':{'observation_not_completed':1},'error_code':code,'before_digest':snapshot_digest(side,store.rows(side+'_snapshot',side))}
                    cycle.persist(store,key,side,'blocked',data)
                    result={'status':'blocked','cycle':key,'code':code,'sent':False,'snapshot_advanced':False};live.save(key+'-result.json',result);return result
                committed=[r for r in history if r['status']=='committed']
                if committed:
                    last=max(committed,key=lambda r:cycle.payload(r)['observation']['observed_at'])
                    if snapshot_digest(side,before)!=cycle.payload(last)['after_digest']:raise ValueError('LIVE_BASELINE_CHANGED_OUTSIDE_CYCLE')
                doc=bridge.compare(side,before,current,at)
                doc['baseline_source']='live_acceptance_snapshot';doc['reference']['table']=live.TABLES[side+'_snapshot'];doc['observation_clock']='database_observation'
                reasons=issues(side,before,current,doc)
                data={'document':doc,'observation':evidence,'before_digest':snapshot_digest(side,before),'blockers':reasons,'current':current,'real_transport':True}
                with store.transaction():
                    store.replace_scope('price_input',side,[{'side':side,'ordinal':i,'payload':base.canonical(r)} for i,r in enumerate(current)])
                    written=[cycle.payload(r) for r in sorted(store.rows('price_input',side),key=lambda r:r['ordinal'])]
                    if written!=current:raise ValueError('LIVE_PRICE_INPUT_READBACK_MISMATCH')
                    if reasons:store.cycle(key,side,'blocked',data)
                if reasons:
                    result={'status':'blocked','cycle':key,'observation':evidence,'event_counts':doc['event_counts'],'blockers':reasons,'sent':False,'snapshot_advanced':False,'baseline_digest':data['before_digest']}
                    live.save(key+'-result.json',result);return result
                data['after']=[{**r,'current_record':base.canonical(current[i]),'reference_kind':'observed'} for i,r in enumerate(cycle.project(side,current,at))]
                data['after_digest']=snapshot_digest(side,data['after']);data['notices']=source_notices(side,key,doc) if doc['deliverable_event_count'] else []
                data['notice_manifest']=seal_notices(data['notices'])
                cycle.persist(store,key,side,'planned',data);state='planned'
            if data['notices'] and not allow_send:
                result={'status':'prepared_not_sent','cycle':key,'observation':data['observation'],'events':data['document']['event_counts'],'logical_notices':len(data['notices']),'snapshot_advanced':False}
                live.save(key+'-result.json',result);return result
            if data['notices'] and seal_notices(data['notices'])!=data.get('notice_manifest'):raise ValueError('PREPARED_PRICE_NOTICE_OR_ATTACHMENT_CHANGED')
            return cycle.complete_plan(store,side,side,key,data,state,hasher=snapshot_digest,dispatcher=bounded_dispatch(store,key,side,data),saver=live.save)
    finally:store.close()
