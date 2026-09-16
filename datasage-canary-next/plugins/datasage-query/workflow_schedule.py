"""Prepared official-cron entry; requires a separate bounded approval, default off."""
from datetime import datetime,timedelta,timezone
import json
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle

JOBS=('sales','purchase','slow-task','slow-report')
def plan():return {'status':'prepared_not_registered','runtime':'official Hermes script/no_agent local job','timezone':'Asia/Shanghai',
    'test_duration_hours':6,'max_window_hours':24,'test_departments':['HCM'],'targets':'existing pinned test private member and test webhook; no legacy production targets',
    'jobs':[{'job':'sales','script':'datasage_live_sales.py','cron':'7 * * * *','meaning':'hourly compare with own committed snapshot'},
            {'job':'purchase','script':'datasage_live_purchase.py','cron':'12 * * * *','meaning':'hourly compare with own committed snapshot'},
            {'job':'slow-task','script':'datasage_live_slow_task.py','cron':'0,5,10,15,20,25 9 * * 2','meaning':'Tuesday 09:00 initial task, bounded continuation of incomplete stages; never refreeze completed generation'},
            {'job':'slow-report','script':'datasage_live_slow_report.py','cron':'0,5 19 * * 6','meaning':'Saturday weekly before monthly; existing scope and receipt prerequisites'}],
    'activation_requirements':['user approval of exact start/end','workflow-live-runtime.json schedule_enabled=true','workflow-schedule-acceptance.json enabled=true, start/end <=24h, max_invocations<=60, fixed departments','official jobs created paused and reviewed before resume'],
    'stop':'disable local schedule approval or expire window; pause official jobs; preserve journals and snapshots',
    'resume':'review unresolved states, restore a separately approved bounded window, resume existing jobs; no reset/new generation',
    'limits':{'logical_notifications_per_invocation':2,'max_invocations':60},'minute_note':'07/12 are proposed test minutes, not a claim about original external scheduler minutes'}
def approval(value,at):
    if not isinstance(value,dict) or set(value)!={'enabled','starts_at','ends_at','max_invocations','departments'} or value['enabled'] is not True:raise ValueError('BOUNDED_SCHEDULE_APPROVAL_REQUIRED')
    start=datetime.fromisoformat(value['starts_at']);end=datetime.fromisoformat(value['ends_at'])
    if not start.tzinfo or not end.tzinfo or not timedelta(0)<end-start<=timedelta(hours=24) or not start<=at<end:raise ValueError('SCHEDULE_WINDOW_INACTIVE')
    if type(value['max_invocations']) is not int or not 1<=value['max_invocations']<=60:raise ValueError('SCHEDULE_BUDGET_INVALID')
    if not isinstance(value['departments'],list) or not 1<=len(value['departments'])<=2 or len(set(value['departments']))!=len(value['departments']) or any(d not in live.DEPARTMENTS for d in value['departments']):raise ValueError('SCHEDULE_DEPARTMENTS_INVALID')
    return value
def due(job,at):
    if job=='sales':return at.minute==7
    if job=='purchase':return at.minute==12
    if job=='slow-task':return at.weekday()==1 and at.hour==9 and at.minute in (0,5,10,15,20,25)
    if job=='slow-report':return at.weekday()==5 and at.hour==19 and at.minute in (0,5)
    raise ValueError('SCHEDULE_JOB_INVALID')
def tick(job):
    if job not in JOBS:raise ValueError('SCHEDULE_JOB_INVALID')
    config=json.loads(live.binding().read_text(encoding='utf-8'));live.validate_binding(config)
    if config['schedule_enabled'] is not True:return {'status':'schedule_disabled','registered_or_enabled':False}
    path=base.profile()/'workflow-schedule-acceptance.json'
    if path.is_symlink() or not path.is_file():raise ValueError('BOUNDED_SCHEDULE_APPROVAL_REQUIRED')
    requested=json.loads(path.read_text(encoding='utf-8'))
    store=live.Store()
    try:
        with live.lock(store,'bounded-schedule'):
            clock=store._read('SELECT NOW(6) AS at,UTC_TIMESTAMP(6) AS utc_at')[0]
            if clock['at']-clock['utc_at']!=timedelta(hours=8):raise ValueError('SCHEDULE_DATABASE_CLOCK_NOT_UTC8')
            at=clock['at'].replace(tzinfo=timezone(timedelta(hours=8)));approved=approval(requested,at)
            if not due(job,at):return {'status':'not_due','job':job,'observed_at':at.isoformat()}
            key='schedule-'+job+'-'+at.strftime('%Y%m%d%H%M')
            runs=[r for r in store.rows('cycles') if r['cycle_id'].startswith('schedule-') and cycle.payload(r).get('window')==approved['starts_at']]
            if any(r['cycle_id']==key for r in runs):return {'status':'same_schedule_slot_already_started','job':job}
            if len(runs)>=approved['max_invocations']:raise ValueError('SCHEDULE_INVOCATION_BUDGET_EXHAUSTED')
            data={'window':approved['starts_at'],'job':job,'observed_at':at.isoformat()}
            cycle.persist(store,key,'schedule','planned',data)
            if job in ('sales','purchase'):
                from . import workflow_live_prices
                result=workflow_live_prices.run(job)
                if result.get('status')=='prepared_not_sent':result=workflow_live_prices.run(job,allow_send=True)
            else:
                from . import workflow_live_slow as slow
                department=approved['departments'][len(runs)%len(approved['departments'])]
                preparation=slow.prepare(department,reports=job=='slow-report')
                # At most two logical stage notices per scheduler invocation.
                result={'preparation':preparation,'stages':[]}
                for _ in range(2):
                    state=store.rows('cycles')
                    current=next((r for r in state if r['cycle_id']==preparation.get('cycle')),None)
                    if not current:break
                    index=cycle.payload(current)['phase_index']
                    if job=='slow-task' and index>=3 or index>=5:break
                    item=slow.deliver(department,report_only=job=='slow-report');result['stages'].append(item)
                    if item.get('status')!='stage_completed':break
            cycle.persist(store,key,'schedule','committed',{**data,'result':result})
            return result
    finally:store.close()
