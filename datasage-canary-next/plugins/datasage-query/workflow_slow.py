"""Fixed freeze -> task -> customer -> audit -> weekly -> monthly orchestration."""
from datetime import datetime
from pathlib import Path
import hashlib,uuid
from . import workflow_storage as s,workflow_cycle as cycle,workflow_fixture as fixture
from . import workflow_inputs as inputs,legacy_workflow as wf,report_evidence as reports
from . import acceptance_delivery as delivery,reminder_acceptance as session,workflow_io as io
from .legacy_xlsx import gen_workbook_xlsx

PHASES=('task','customer','audit','weekly','monthly')
def artifact_root():
    root=io.private_root(delivery.runtime_home(s.profile()))/'wv1'
    if root.is_symlink():raise ValueError('WORKFLOW_ARTIFACT_PATH_INVALID')
    root.mkdir(exist_ok=True);return root
def notice(key,body,files=(),fmt='text'):
    return {'logical_id':key,'role':'Profile固定入口 HCM 3个SKU测试子集；非生产全量；仅批准测试成员','channel':'private','body':body,'attachments':[str(f) for f in files],'message_format':fmt}
def freeze(store,seed):
    week=seed['week'];key='slow-'+week.lower();existing=next((r for r in store.rows('cycles','main') if r['cycle_id']==key),None)
    if existing:
        data=cycle.payload(existing)
        if s.digest(s.normalized('slow_baseline',store.rows('slow_baseline')))!=data['freeze_digest']:raise ValueError('FROZEN_TEST_BASELINE_CHANGED')
        return key,existing['status'],data
    now=store._read('SELECT NOW(6) AS at')[0]['at']
    if week!=f'{now.isocalendar().year}-W{now.isocalendar().week:02d}':raise ValueError('TEST_SEED_WEEK_EXPIRED')
    if store.rows('slow_baseline'):raise ValueError('UNREGISTERED_FREEZE_NO_OVERWRITE')
    with s.Snapshot() as db:
        spec=wf.source_query_specs(week)[0];source=inputs.complete(db,spec['sql'],spec['params'])
    plan=wf.freeze_plan(source,[],week,week)
    if plan['status']!='ready':raise ValueError('FREEZE_PLAN_BLOCKED')
    with store.transaction():
        store.insert('slow_baseline',plan['insert_rows']);saved=store.rows('slow_baseline')
        projected=[{k:r[k] for k in wf.BASELINE_COLUMNS} for r in saved]
        if s.normalized('slow_baseline',projected)!=s.normalized('slow_baseline',plan['insert_rows']):raise ValueError('FREEZE_FIELD_READBACK_MISMATCH')
        if len({str(r['frozen_at']) for r in saved})!=1 or not saved[0]['frozen_at']:raise ValueError('FREEZE_TIME_READBACK_MISMATCH')
        data={'week':week,'month':seed['month'],'baseline':saved,'freeze_digest':s.digest(s.normalized('slow_baseline',saved)),'frozen_at':str(saved[0]['frozen_at']),'phase_index':0,'phases':{},'scope':'HCM bounded test subset','seed':seed}
        store.cycle(key,'main','planned',data)
    return key,'planned',data
def build(phase,key,data,*,snapshots=None,saver=s.save):
    rows=data['baseline'];week=data['week'];folder=artifact_root()/uuid.uuid4().hex[:8];folder.mkdir();region=data.get('department','HCM')
    ref=session._reference(s.profile())
    if phase=='task':
        with s.tools._ConsistentSnapshotExecutor(deadline_at=s.tools._call_deadline(None)) as db:
            depts=ref['regions'][region]['dynamic_sales_departments']
            employees=inputs.complete(db,"SELECT region,main_dept,person_name,wecom_account,position,is_delete,wecom_status FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_status='payroll' AND COALESCE(wecom_account,'')<>'' AND region=%s AND main_dept IN ("+','.join('%s' for _ in depts)+') ORDER BY wecom_account LIMIT 10001',[region,*depts])
        targets=wf.task_recipients(ref['regions'],employees,[region])[region]
        periods=wf.legacy_periods(io.at_utc8(week));body,sheet=wf.task_draft(region,week,periods['planned_start'][:10],periods['planned_end'][:10],rows)
        path=folder/'Test_Products.xlsx';gen_workbook_xlsx([sheet],path)
        notices=[notice(key+'-task',body,[path])];evidence={'frozen_rows':len(rows),'original_role_count':len(targets),'actual_delivery':'approved_test_member_only'}
    elif phase=='customer':
        with s.tools._ConsistentSnapshotExecutor(deadline_at=s.tools._call_deadline(None)) as db:mapping=inputs.customer_mapping(db,[(r['goods_no'],r['whse_dept']) for r in rows])
        plan=wf.contact_plan(rows,mapping);packages=plan['sales_packages'][:1]
        if not packages:raise ValueError('TEST_CUSTOMER_PACKAGE_EMPTY')
        data['selected_packages']=packages;data['total_packages']=len(plan['sales_packages'])
        notices=[notice(key+'-customer',wf.customer_package_message(p,week),[wf.customer_zip(p,folder,week)]) for p in packages]
        evidence={'selected_packages':len(packages),'total_packages':len(plan['sales_packages']),'unselected_not_sent':len(plan['sales_packages'])-len(packages)}
    elif phase=='audit':
        if data['phases']['customer']['receipt']['status']!='provider_accepted_not_human_read':raise ValueError('CUSTOMER_RECEIPT_REQUIRED')
        packages=data['selected_packages']
        audits=io.dispatch_audits(packages,{p['account']:'provider_accepted' for p in packages},ref['regions'],week,folder)
        notices=[notice(key+'-audit-'+str(i),a['text'],[a['path']]) for i,a in enumerate(audits)]
        evidence={'receipt_meaning':'test target provider acceptance, not original salesperson or customer receipt','selected_packages':len(packages)}
    else:
        original_report_roles=wf.report_recipients(ref['regions'],region)
        if not original_report_roles:raise ValueError('REPORT_ROLE_PLAN_EMPTY')
        period=week if phase=='weekly' else data['month']
        ev,labels=reports.collect(region,period,phase,week,snapshots=snapshots or s.Snapshot)
        ev['physical_test_mapping']=data.get('physical_test_mapping') or {k:s.TABLES[v] for k,v in s.MAP.items()};ev['storage_mode']='isolated_acceptance'
        saver(key+'-'+phase+'-query.json',ev)
        adapted=inputs.legacy_report_packet(ev['packets']['pool'],ev['packets']['flow'],labels,region,period,evidence=ev)
        if not adapted['label_coverage']['complete']:raise ValueError('REPORT_LABEL_COVERAGE_INCOMPLETE')
        saver(key+'-'+phase+'-validation.json',adapted)
        body=wf.report_draft(region,period,adapted['summary'],adapted['sales_rows'],monthly=phase=='monthly')
        path=folder/('Test_'+phase+'.xlsx');gen_workbook_xlsx([('Detail',wf.REPORT_HEADERS,adapted['detail_rows'])],path,borders=True,landscape=True)
        notices=[notice(key+'-'+phase,body,[path],'markdown')];evidence={'summary':adapted['summary'],'completeness':adapted['completeness'],'label_coverage':adapted['label_coverage'],'original_report_role_count':len(original_report_roles),'delivery_route':'approved test member only; same report merged for role acceptance'}
    return {'notices':notices,'notice_digest':s.digest(notices),'files':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for n in notices for p in n['attachments']},'evidence':evidence}
def verify_artifacts(manifest):
    if s.digest(manifest['notices'])!=manifest['notice_digest']:raise ValueError('STAGED_NOTICE_CHANGED')
    for name,digest in manifest['files'].items():
        path=Path(name)
        if path.is_symlink() or not path.resolve().is_relative_to(artifact_root().resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('STAGED_ATTACHMENT_CHANGED')
def run():
    delivery.load_settings(s.profile());session._reference(s.profile())
    store=s.Store()
    try:
        with cycle.lock(store,'slow','main'):
            seed=fixture.seeded(store,'seed-slow')
            if not seed:raise ValueError('SLOW_SEED_REQUIRED')
            key,status,data=freeze(store,seed)
            if status=='committed':return {'status':'already_completed_no_resend','cycle':key,'phases':list(PHASES)}
            if status=='unknown':raise ValueError('SLOW_DELIVERY_UNKNOWN_REVIEW_REQUIRED')
            while data['phase_index']<len(PHASES):
                phase=PHASES[data['phase_index']];stage_key=key+'-'+phase
                if phase not in data['phases']:
                    data['phases'][phase]=build(phase,key,data);cycle.persist(store,key,'main','planned',data);status='planned'
                manifest=data['phases'][phase];verify_artifacts(manifest)
                if status!='sending':cycle.persist(store,key,'main','sending',data)
                try:receipt=cycle.dispatch(stage_key,manifest['notices'],status)
                except Exception as exc:
                    cycle.persist(store,key,'main','failed' if str(exc)=='DELIVERY_COMPONENT_FAILED' else 'unknown',data)
                    raise
                manifest['receipt']=receipt;data['phase_index']+=1
                status='committed' if data['phase_index']==len(PHASES) else 'planned'
                cycle.persist(store,key,'main',status,data)
                s.save(stage_key+'-result.json',{'phase':phase,'receipt':receipt,'evidence':manifest['evidence']})
            result={'status':'completed','cycle':key,'frozen_rows':len(data['baseline']),'phases':{k:v['receipt'] for k,v in data['phases'].items()},'scope':data['scope']}
            s.save(key+'-result.json',result);return result
    finally:store.close()
