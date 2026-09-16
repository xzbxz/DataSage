"""Finite real-source acceptance actions; fixture paths are inaccessible here."""
from pathlib import Path
import json
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle
from . import workflow_live_prices as prices,workflow_live_slow as slow,workflow_schedule as schedule

ACTIONS=('init','prices','observe-prices','deliver-prices','slow-prepare','slow-preview','slow-deliver','slow-new-generation','status','schedule-plan','scheduled-tick','lock-check')
def status():
    from . import reminder_acceptance
    reference=reminder_acceptance._reference(base.profile())
    store=live.Store()
    try:
        records=store.rows('cycles');summary=[];snapshots=[];departments=[]
        for row in records:
            data=cycle.payload(row);item={'cycle':row['cycle_id'],'scope':row['test_scope'],'status':row['status']}
            if row['cycle_id'].startswith('lp-'):item.update(observation=data.get('observation'),blockers=data.get('blockers'),event_counts=data['document']['event_counts'])
            if row['cycle_id'].startswith('ls-'):
                current=cycle.clean(store.rows('slow_baseline',data['scope']))
                if base.digest(base.normalized('slow_baseline',current))!=data['freeze_digest']:raise ValueError('LIVE_FREEZE_STATUS_MISMATCH')
                role_count=len(base.wf.report_recipients(reference['regions'],data['department']))
                if not role_count:raise ValueError('REPORT_ROLE_PLAN_EMPTY')
                info={'department':data['department'],'cycle':row['cycle_id'],'status':row['status'],'frozen_rows':len(current),'observation':data['observation'],'phase_index':data['phase_index'],'original_report_role_count':role_count,'phases':{k:{'prepared':True,'receipt':v.get('receipt'),'evidence':v['evidence']} for k,v in data['phases'].items()}}
                departments.append(info)
            summary.append(item)
        for side in ('sales','purchase'):
            rows=store.rows(side+'_snapshot',side)
            input_verified=None;origin_verified=None
            candidates=[r for r in records if r['cycle_id'].startswith('lp-'+side+'-')]
            if candidates:
                latest=max(candidates,key=lambda r:cycle.payload(r)['observation']['observed_at']);data=cycle.payload(latest)
                expected=data['after_digest'] if latest['status']=='committed' else data['before_digest']
                if prices.snapshot_digest(side,rows)!=expected:raise ValueError('LIVE_PRICE_STATUS_MISMATCH')
                if data.get('current') is not None:
                    written=[cycle.payload(r) for r in sorted(store.rows('price_input',side),key=lambda r:r['ordinal'])]
                    if written!=data['current']:raise ValueError('LIVE_PRICE_INPUT_STATUS_MISMATCH')
                    input_verified=True
            if rows and all(r['reference_kind']=='legacy' for r in rows):
                origin=next(r for r in records if r['cycle_id']=='origin-'+side)
                reconstructed=[{k:base.tools._json_value(r[k]) for k in prices.bridge.SPECS[side]['fields']} for r in sorted(rows,key=lambda r:r['id'])]
                if base.digest(reconstructed)!=cycle.payload(origin)['digest']:raise ValueError('LEGACY_STARTING_COPY_DIGEST_MISMATCH')
                origin_verified=True
            snapshots.append({'side':side,'rows':len(rows),'digest':prices.snapshot_digest(side,rows),'origin_kinds':sorted({r['reference_kind'] for r in rows}),'latest_input_readback_verified':input_verified,'legacy_starting_copy_verified':origin_verified})
        result={'mode':'real_source_acceptance','production_enabled':False,'clock':'database_observation','cycles':summary,'snapshots':snapshots,'departments':departments,'runtime':{'entry':str(base.profile()/'scripts/datasage_workflow.py'),'working_directory':str(Path.cwd())},'schedule_enabled':json.loads(live.binding().read_text())['schedule_enabled'],'scope_note':'Each prepared department includes all source stock/month rows within fixed budgets; customer delivery is one complete package sample.'}
        receipts=[p['receipt'] for d in departments for p in d['phases'].values() if p.get('receipt')]
        for row in records:
            data=cycle.payload(row)
            if row['cycle_id'].startswith('lp-') and data.get('receipt',{}).get('batch_receipts'):receipts.extend(data['receipt']['batch_receipts'].values())
        for receipt in receipts:
            path=Path(receipt['progress_file'])
            if not path.resolve().is_relative_to(base.profile().resolve()):raise ValueError('LIVE_RECEIPT_PATH_INVALID')
            progress=json.loads(path.read_text(encoding='utf-8'))
            if sum(len(k)==64 and v['status']=='provider_accepted' for k,v in progress['components'].items())!=receipt['components']:raise ValueError('LIVE_RECEIPT_COUNT_MISMATCH')
        result['delivery_evidence']={'logical_notifications':sum(r['logical_notifications'] for r in receipts),'components':sum(r['components'] for r in receipts),'actual_component_states_verified':True,'human_read':'unknown'}
        live.save('status.json',result)
        compact={**{k:result[k] for k in ('mode','production_enabled','clock','snapshots','runtime','schedule_enabled','delivery_evidence')},
                 'price_cycles':[r for r in summary if r['cycle'].startswith('lp-')],
                 'departments':[{'department':d['department'],'frozen_rows':d['frozen_rows'],'stock_rows':d['observation']['stock_rows'],'monthly_rows':d['observation']['monthly_rows'],'status':d['status'],'phase_index':d['phase_index'],'weekly_groups':d['phases'].get('weekly',{}).get('evidence',{}).get('completeness',{}).get('pool_groups'),'monthly_groups':d['phases'].get('monthly',{}).get('evidence',{}).get('completeness',{}).get('pool_groups'),'labels_complete':all(v.get('evidence',{}).get('label_coverage',{}).get('complete',True) for v in d['phases'].values())} for d in departments]}
        live.save('summary.json',compact);return compact
    finally:store.close()
def lock_check():
    first=live.Store();second=live.Store();denied=False
    try:
        with live.lock(first,'prices-sales'):
            try:
                with live.lock(second,'prices-sales'):pass
            except ValueError as exc:
                if str(exc)!='LIVE_WORKFLOW_BUSY':raise
                denied=True
        with live.lock(second,'prices-sales'):released=True
        if not denied:raise ValueError('DUPLICATE_START_NOT_BLOCKED')
        result={'concurrent_start_blocked':denied,'lock_released_for_restart':released,'real_connections':2,'data_modified':False};live.save('lock-check.json',result);return result
    finally:first.close();second.close()
def run(action,*,department=None,job=None,reason=None):
    if action not in ACTIONS:raise ValueError('LIVE_ACTION_REJECTED')
    if action.startswith('slow-') and department not in live.DEPARTMENTS:raise ValueError('LIVE_DEPARTMENT_REQUIRED')
    if action=='init':return live.bootstrap()
    if action in ('prices','observe-prices','deliver-prices'):
        results={}
        for side in ('sales','purchase'):
            try:results[side]=prices.run(side,manual=action=='observe-prices',allow_send=action=='deliver-prices')
            except Exception as exc:
                result={'status':'blocked','sent':'not_claimed','snapshot_advanced':'not_claimed','code':str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else type(exc).__name__};live.save(side+'-last-error.json',result);results[side]=result
        return results
    if action=='slow-prepare':return slow.prepare(department)
    if action=='slow-preview':return slow.preview(department)
    if action=='slow-new-generation':return slow.prepare(department,new_generation=True,reason=reason)
    if action=='slow-deliver':return slow.deliver(department)
    if action=='status':return status()
    if action=='lock-check':return lock_check()
    if action=='schedule-plan':return schedule.plan()
    return schedule.tick(job)
