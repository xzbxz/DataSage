"""Persistent price cycle; shared comparator and delivery, no production activation."""
from contextlib import contextmanager
from datetime import datetime,timedelta
import json,re
from . import workflow_storage as storage,legacy_price_bridge as bridge
from . import acceptance_delivery as delivery,workflow_io as io,legacy_workflow as wf

SCOPES=('main','fault-partial','fault-unknown','fault-interrupt','fault-beforecommit','fault-aftercommit')
class DeliveryDeferred(ValueError):pass
def payload(row):return json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
def clean(rows):return [{k:v for k,v in r.items() if k!='test_scope'} for r in rows]
def snapshot_digest(side,rows):return storage.digest(storage.normalized(side+'_snapshot',clean(rows)))
def inputs(store,side,scope):
    return [payload(r) for r in sorted(store.rows('price_input',scope),key=lambda r:r['ordinal']) if r['side']==side]
def journals(store,side,scope):return [r for r in store.rows('cycles',scope) if r['cycle_id'].startswith(side+'-')]
def persist(store,key,scope,status,data):
    with store.transaction():store.cycle(key,scope,status,data)
@contextmanager
def lock(store,side,scope):
    if side not in ('sales','purchase','slow','seed') or scope not in SCOPES:raise ValueError('WORKFLOW_LOCK_SCOPE_INVALID')
    name='ds_profile_v1_'+side+'_'+scope
    if store._read('SELECT GET_LOCK(%s,0) AS acquired',[name])[0]['acquired']!=1:raise ValueError('WORKFLOW_BUSY')
    try:yield
    finally:
        try:store._read('SELECT RELEASE_LOCK(%s) AS released',[name])
        except Exception:pass
def notices(key,document):
    side='sales' if document['kind']=='sales_prices' else 'purchase'
    body='【Profile固定入口测试，非生产价格变化】\n'+document['scope_notice']+'\n'+wf.price_draft(side,bridge.changes(document))
    return [{'logical_id':key+'-'+channel,'channel':channel,'role':'Profile小时价格循环，独立测试数据','body':body,'attachments':[],'message_format':'markdown' if side=='purchase' else 'text'} for channel in (['private'] if side=='sales' else ['private','group'])]
def case_id(key):
    if not re.fullmatch(r'[a-z0-9-]{1,45}',key):raise ValueError('WORKFLOW_CASE_INVALID')
    return 'profile-v1-'+key
def recover_receipt(key,items):
    """Read durable component states before a recovery call; never retry unknown."""
    profile=storage.profile();case=case_id(key)
    progress=io.Progress(delivery.runtime_home(profile),'acceptance-'+case,case)
    parts=[part for notice in items for part in delivery.notification_parts(notice,case)]
    transport=delivery.AcceptanceTransport(profile,parts)
    parts=transport.normalize(parts,progress)
    if not parts or any(progress.status(p['key'])!='provider_accepted' for p in parts):raise ValueError('WORKFLOW_SEND_UNKNOWN_REVIEW_REQUIRED')
    # Full normal entry still checks unchanged fingerprints and destination.
    return delivery.deliver_batch(profile,case,items)
def dispatch(key,items,previous_status,send=None):
    if previous_status=='sending':
        if send is not None:raise ValueError('WORKFLOW_SEND_UNKNOWN_REVIEW_REQUIRED')
        return recover_receipt(key,items)
    if send is not None:return send(key,items)
    return delivery.deliver_batch(storage.profile(),case_id(key),items)
def select_cycle(rows,side,scope):
    pending=[r for r in rows if r['status']!='committed']
    if len(pending)>1:raise ValueError('MULTIPLE_PENDING_CYCLES')
    if pending:return pending[0]['cycle_id'],pending[0]
    tick=max((int(r['cycle_id'].rsplit('-',1)[1]) for r in rows),default=-1)+1
    return side+'-'+scope+'-'+str(tick),None
def project(side,current,at):
    return [{k:(i+1 if k=='id' else at.strftime('%Y-%m-%d %H:%M:%S') if k=='snapshot_at' else row.get('detail_id') if k=='matched_detail_id' else row.get(k)) for k in bridge.SPECS[side]['fields']} for i,row in enumerate(current)]
def run(side,scope='main',*,send=None,fault=None):
    if side not in ('sales','purchase') or scope not in SCOPES:raise ValueError('PRICE_SCOPE_REJECTED')
    if scope!='main' and send is None:raise ValueError('FAULT_SCOPE_REAL_SEND_FORBIDDEN')
    store=storage.Store()
    try:
        with lock(store,side,scope):return advance(store,side,scope,send=send,fault=fault)
    finally:store.close()
def advance(store,side,scope,*,send=None,fault=None):
    rows=journals(store,side,scope)
    key,existing=select_cycle(rows,side,scope)
    if existing:
        data=payload(existing);state=existing['status']
        if state=='unknown':raise ValueError('WORKFLOW_SEND_UNKNOWN_REVIEW_REQUIRED')
        if state not in ('planned','failed','sending','delivered'):raise ValueError('WORKFLOW_CYCLE_STATE_INVALID')
    else:
        seeds=[r for r in store.rows('cycles') if r['cycle_id']=='seed-'+side]
        if len(seeds)!=1 or seeds[0]['status']!='committed':raise ValueError('PRICE_SEED_REQUIRED')
        current=inputs(store,side,scope);before=clean(store.rows(side+'_snapshot',scope))
        if not before or not current:raise ValueError('EMPTY_INPUT_KEEP_TEST_BASELINE')
        if rows:
            latest=max(rows,key=lambda r:int(r['cycle_id'].rsplit('-',1)[1]))
            if snapshot_digest(side,before)!=payload(latest)['after_digest']:raise ValueError('COMMITTED_BASELINE_CHANGED')
        tick=int(key.rsplit('-',1)[1]);at=datetime.fromisoformat(payload(seeds[0])['observed_at'])+timedelta(hours=tick)
        doc=bridge.compare(side,before,current,at)
        if any(e['event'].startswith('unresolved') or e['event']=='recorded_basis_changed' for e in doc['events']):raise ValueError('AMBIGUOUS_PRICE_INPUT_KEEP_BASELINE')
        doc.update(physical_test_reference=storage.TABLES[side+'_snapshot'],test_scope=scope,observation_clock='explicit controlled acceptance clock: seed time plus hourly ticks')
        projected=project(side,current,at)
        data={'document':doc,'current':current,'before_digest':snapshot_digest(side,before),'after':projected,'after_digest':snapshot_digest(side,projected),'notices':notices(key,doc) if doc['deliverable_event_count'] else [],'real_transport':send is None}
        persist(store,key,scope,'planned',data);state='planned'
        if fault:fault('planned',key)
    return complete_plan(store,side,scope,key,data,state,send=send,fault=fault)

def complete_plan(store,side,scope,key,data,state,*,send=None,fault=None,hasher=snapshot_digest,dispatcher=dispatch,saver=storage.save):
    if hasher(side,store.rows(side+'_snapshot',scope))!=data['before_digest']:raise ValueError('TEST_BASELINE_MOVED')
    if state!='delivered':
        if data['notices']:
            if state!='sending':persist(store,key,scope,'sending',data)
            try:receipt=dispatcher(key,data['notices'],state,send)
            except DeliveryDeferred:
                persist(store,key,scope,'planned',data)
                return {'status':'delivery_pending','cycle':key,'test_snapshot_advanced':False}
            except Exception as exc:
                status='failed' if str(exc)=='DELIVERY_COMPONENT_FAILED' else 'unknown'
                persist(store,key,scope,status,{**data,'delivery_error':str(exc) if isinstance(exc,(ValueError,io.IOErrorBoundary)) else type(exc).__name__})
                raise
        else:receipt={'status':'silent_no_deliverable_change','components':0,'real_network':False}
        data['receipt']=receipt
        persist(store,key,scope,'delivered',data)
        if fault:fault('delivered',key)
    with store.transaction():
        store.replace_snapshot(side,scope,data['after'])
        if hasher(side,store.rows(side+'_snapshot',scope))!=data['after_digest']:raise ValueError('SNAPSHOT_READBACK_FAILED')
        store.cycle(key,scope,'committed',data)
        if fault:fault('before_commit',key)
    if fault:fault('after_commit',key)
    result={'cycle':key,'events':data['document']['event_counts'],'delivery':data['receipt'],'test_snapshot_advanced':True,'snapshot_digest':data['after_digest'],'real_transport':data['real_transport']}
    saver(key+'-result.json',result);return result
