"""Department-complete real observations, weekly freeze, immutable staged generations."""
from datetime import datetime,timedelta
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle
from . import workflow_inputs as inputs,workflow_slow as slow,legacy_workflow as wf

def page_rows(db,sql,args,fields,*,budget=100000):
    collected=[]
    for offset in range(0,budget,2000):
        # All selected fields define a total value order; identical rows are interchangeable.
        rows,cut,_=db.execute(sql+' ORDER BY '+','.join(fields)+' LIMIT %s OFFSET %s',[*args,2001,offset],2000)
        collected.extend(rows)
        if not cut:return collected
    raise ValueError('LIVE_SOURCE_ROW_BUDGET_EXCEEDED_KEEP_OLD_INPUT')
def observe_department(department):
    if department not in live.DEPARTMENTS:raise ValueError('LIVE_DEPARTMENT_REJECTED')
    with base.tools._ConsistentSnapshotExecutor(deadline_at=base.tools._call_deadline(None)) as db:
        clock=inputs.complete(db,'SELECT NOW(6) AS at,UTC_TIMESTAMP(6) AS utc_at',limit=1)[0];at=datetime.fromisoformat(str(clock['at']))
        month=at.strftime('%Y-%m');previous=(at.replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
        fields=[c['COLUMN_NAME'] for c in base.schema()['stock_input']]
        stock=page_rows(db,'SELECT '+','.join(fields)+' FROM vk_ods.slow_moving_goods_ods WHERE whse_dept=%s',[department],fields,budget=20000)
        monthly=page_rows(db,'SELECT '+','.join(base.MONTH_FIELDS)+' FROM vk_dw.inventory_barcode_detail_bymonth_dw WHERE whse_dept=%s AND month_date IN (%s,%s)',[department,previous,month],base.MONTH_FIELDS)
        if not stock or not monthly:raise ValueError('LIVE_DEPARTMENT_SOURCE_EMPTY_KEEP_BASELINE')
        evidence={'department':department,'week':f'{at.isocalendar().year}-W{at.isocalendar().week:02d}','month':month,'observed_at':at.isoformat(),'observed_utc':str(clock['utc_at']),'source_snapshot':db.marker,'stock_rows':len(stock),'monthly_rows':len(monthly),'source_scope':'all rows in this department, current stock and previous/current month inventory','bounded_complete':True,'stock_digest':base.digest(base.normalized('stock_input',stock)),'monthly_digest':base.digest(base.normalized('monthly_stock_input',monthly))}
        return stock,monthly,evidence
def latest(store,department,week):
    prefix=department.lower()+'-'+week.lower()+'-g'
    rows=[r for r in store.rows('cycles') if r['cycle_id'].startswith('ls-'+prefix)]
    return max(rows,key=lambda r:cycle.payload(r)['generation']) if rows else None
def persist(store,key,data,status='planned'):cycle.persist(store,key,data['scope'],status,data)
def install_input(store,scope,stock,monthly):
    for role,rows in [('stock_input',stock),('monthly_stock_input',monthly)]:
        store.replace_scope(role,scope,rows)
        actual=cycle.clean(store.rows(role,scope))
        if base.normalized(role,actual)!=base.normalized(role,rows):raise ValueError('LIVE_INPUT_READBACK_MISMATCH')
def prepare(department,*,new_generation=False,reason=None,reports=True):
    if department not in live.DEPARTMENTS:raise ValueError('LIVE_DEPARTMENT_REJECTED')
    if new_generation and (not isinstance(reason,str) or not 5<=len(reason.strip())<=120):raise ValueError('EXPLICIT_GENERATION_REASON_REQUIRED')
    store=live.Store()
    try:
        with live.lock(store,'slow-'+department.lower()):
            clock=store._read('SELECT NOW(6) AS at')[0]['at'];week=f'{clock.isocalendar().year}-W{clock.isocalendar().week:02d}'
            old=latest(store,department,week)
            if old and not new_generation:
                data=cycle.payload(old);key=old['cycle_id']
                if old['status'] in ('unknown','sending'):return {'status':'blocked','reason':'EXISTING_DELIVERY_REQUIRES_RECOVERY','cycle':key}
                stale=[]
                for phase in ('weekly','monthly'):
                    manifest=data['phases'].get(phase)
                    if manifest and 'receipt' not in manifest and (data['phase_index']<slow.PHASES.index(phase) or old['status']=='planned'):
                        observed=datetime.fromisoformat(manifest['evidence']['completeness']['observed_to'])
                        if clock-observed>timedelta(hours=1):stale.append(phase)
                if stale:
                    stock,monthly,evidence=observe_department(department)
                    with store.transaction():install_input(store,data['scope'],stock,monthly)
                    for phase in stale:data.setdefault('superseded_reports',[]).append({'phase':phase,'manifest':data['phases'].pop(phase)})
                    data['report_observation']=evidence;persist(store,key,data)
                if all(p in data['phases'] for p in ('weekly','monthly')) or not reports:return summary(old,data,unchanged=True)
            else:
                if old and old['status']!='committed':raise ValueError('INCOMPLETE_GENERATION_CANNOT_BE_BYPASSED')
                generation=cycle.payload(old)['generation']+1 if old else 0
                if generation>999:raise ValueError('GENERATION_BUDGET_EXCEEDED')
                scope=department.lower()+'-'+week.lower()+'-g'+str(generation);key='ls-'+scope
                stock,monthly,evidence=observe_department(department)
                if evidence['week']!=week:raise ValueError('WEEK_CHANGED_DURING_OBSERVATION')
                plan=wf.freeze_plan(stock,[],week,week)
                if plan['status']!='ready':raise ValueError('LIVE_FREEZE_PLAN_BLOCKED')
                with store.transaction():
                    install_input(store,scope,stock,monthly)
                    store.insert('slow_baseline',[{**r,'test_scope':scope} for r in plan['insert_rows']])
                    saved=cycle.clean(store.rows('slow_baseline',scope))
                    projected=[{k:r[k] for k in wf.BASELINE_COLUMNS} for r in saved]
                    if base.normalized('slow_baseline',projected)!=base.normalized('slow_baseline',plan['insert_rows']):raise ValueError('LIVE_FREEZE_READBACK_MISMATCH')
                    if len({str(r['frozen_at']) for r in saved})!=1:raise ValueError('LIVE_FREEZE_TIMESTAMP_MISMATCH')
                    data={'department':department,'week':week,'month':evidence['month'],'scope':scope,'generation':generation,'generation_reason':reason if new_generation else 'ordinary_weekly_generation','baseline':saved,'frozen_at':str(saved[0]['frozen_at']),'freeze_digest':base.digest(base.normalized('slow_baseline',saved)),'observation':evidence,'phase_index':0,'phases':{},'physical_test_mapping':{k:live.TABLES[v] for k,v in base.MAP.items()}}
                    store.cycle(key,scope,'planned',data)
            # Full department reports can be prepared without flooding test targets.
            if reports:
                if data['phase_index']>0 and 'weekly' not in data['phases']:
                    stock,monthly,evidence=observe_department(department)
                    with store.transaction():install_input(store,data['scope'],stock,monthly)
                    data['report_observation']=evidence
                for phase in ('weekly','monthly'):
                    if phase not in data['phases']:
                        data['phases'][phase]=build(phase,key,data);persist(store,key,data)
            result={'status':'prepared','cycle':key,'department':department,'generation':data['generation'],'frozen_rows':len(data['baseline']),'observation':data['observation'],'reports_prepared':reports,'sent':False}
            live.save(key+'-preparation.json',result);return result
    finally:store.close()
def build(phase,key,data):
    import uuid
    version=uuid.uuid4().hex[:8];files=[]
    def save_evidence(name,value):
        name=name.removesuffix('.json')+'-'+version+'.json';live.save(name,value);files.append(name)
    manifest=slow.build(phase,key,data,snapshots=lambda:live.Snapshot(data['scope']),saver=save_evidence)
    manifest['evidence']['query_files']=files
    for notice in manifest['notices']:
        notice['role']='真实来源 '+data['department']+' 部门完整库存观察；客户包仅有界抽验；测试目标'
        notice['body']='【真实来源验收，非生产派发】\n'+notice['body']
    manifest['notice_digest']=base.digest(manifest['notices']);return manifest
def summary(row,data,*,unchanged=False):return {'status':row['status'],'cycle':row['cycle_id'],'department':data['department'],'frozen_rows':len(data['baseline']),'generation':data['generation'],'phase_index':data['phase_index'],'prepared_phases':list(data['phases']),'existing_generation_preserved':unchanged,'sent':False}
def preview(department):
    if department not in live.DEPARTMENTS:raise ValueError('LIVE_DEPARTMENT_REJECTED')
    store=live.Store()
    try:
        with live.lock(store,'slow-'+department.lower()):
            clock=store._read('SELECT NOW(6) AS at')[0]['at'];week=f'{clock.isocalendar().year}-W{clock.isocalendar().week:02d}'
            row=latest(store,department,week)
            if not row:raise ValueError('LIVE_WEEK_PREPARATION_REQUIRED')
            if row['status'] in ('sending','unknown','failed'):raise ValueError('DELIVERY_RECOVERY_REQUIRED_BEFORE_PREVIEW')
            key=row['cycle_id'];data=cycle.payload(row)
            for phase in ('task','customer'):
                if phase not in data['phases']:data['phases'][phase]=build(phase,key,data);persist(store,key,data)
            lines=['# '+department+' 真实来源验收待发内容','', '目的地：仅 zhangzhengwei 测试私信。不是旧销售或客户。', '客户包为一个完整逻辑包抽验；其余包不发送。','']
            for phase in ('task','customer','weekly','monthly'):
                if phase not in data['phases']:continue
                lines+=['## '+phase,'']
                for notice in data['phases'][phase]['notices']:
                    lines += [notice['body'],'']+[ '['+__import__('pathlib').Path(p).name+']('+p.replace('\\','/')+')' for p in notice['attachments']]+['']
            lines+=['## 派发核对','', '待客户包平台回执成功后，按同一个已选客户包生成核对表；不会声称实际销售或客户已收到。']
            path=live.root()/(key+'-preview.md');path.write_text('\n'.join(lines),encoding='utf-8')
            return {'status':'prepared_not_sent','preview':str(path),'cycle':key,'frozen_rows':len(data['baseline']),'total_customer_packages':data.get('total_packages'),'selected_customer_packages':len(data.get('selected_packages',[]))}
    finally:store.close()
def deliver(department,*,report_only=False):
    if department not in live.DEPARTMENTS:raise ValueError('LIVE_DEPARTMENT_REJECTED')
    store=live.Store()
    try:
        with live.lock(store,'slow-'+department.lower()):
            clock=store._read('SELECT NOW(6) AS at')[0]['at'];week=f'{clock.isocalendar().year}-W{clock.isocalendar().week:02d}'
            row=latest(store,department,week)
            if not row:raise ValueError('LIVE_WEEK_PREPARATION_REQUIRED')
            key=row['cycle_id'];state=row['status'];data=cycle.payload(row)
            if state=='committed':return {'status':'already_completed_no_resend','cycle':key}
            if state=='unknown':return {'status':'blocked','cycle':key,'reason':'UNKNOWN_DELIVERY_REVIEW_REQUIRED'}
            actual=cycle.clean(store.rows('slow_baseline',data['scope']))
            if base.digest(base.normalized('slow_baseline',actual))!=data['freeze_digest']:raise ValueError('LIVE_FROZEN_BASELINE_CHANGED')
            phase=slow.PHASES[data['phase_index']]
            if report_only and phase not in ('weekly','monthly'):return {'status':'blocked','reason':'TASK_CHAIN_NOT_COMPLETE','cycle':key}
            if phase not in data['phases']:
                if phase in ('weekly','monthly'):raise ValueError('FRESH_REPORT_PREPARATION_REQUIRED')
                data['phases'][phase]=build(phase,key,data);persist(store,key,data);state='planned'
            manifest=data['phases'][phase];slow.verify_artifacts(manifest)
            if phase in ('weekly','monthly') and state=='planned' and clock-datetime.fromisoformat(manifest['evidence']['completeness']['observed_to'])>timedelta(hours=1):raise ValueError('REPORT_REOBSERVATION_REQUIRED')
            if state!='sending':persist(store,key,data,'sending')
            try:receipt=cycle.dispatch(key+'-'+phase,manifest['notices'],state)
            except Exception as exc:
                persist(store,key,data,'failed' if str(exc)=='DELIVERY_COMPONENT_FAILED' else 'unknown');raise
            manifest['receipt']=receipt;data['phase_index']+=1
            persist(store,key,data,'committed' if data['phase_index']==len(slow.PHASES) else 'planned')
            result={'status':'stage_completed','cycle':key,'phase':phase,'receipt':receipt,'next_phase':slow.PHASES[data['phase_index']] if data['phase_index']<len(slow.PHASES) else None,'department':department,'scope':'full department inventory; one complete customer package sampled'}
            live.save(key+'-'+phase+'-result.json',result);return result
    finally:store.close()
