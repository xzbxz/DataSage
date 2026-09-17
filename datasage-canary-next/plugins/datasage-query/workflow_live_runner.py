"""Finite real-source acceptance actions; fixture paths are inaccessible here."""
from pathlib import Path
import json
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle
from . import workflow_live_prices as prices,workflow_live_slow as slow,workflow_schedule as schedule

ACTIONS=('customer-audit','customer-coverage','customer-repair','customer-samples','customer-sample-send','init','prices','observe-prices','deliver-prices','anomaly-evidence','continuity-replay','slow-prepare','slow-preview','slow-deliver','slow-new-generation','status','schedule-plan','scheduled-tick','lock-check')
def status():
    from . import reminder_acceptance
    reference=reminder_acceptance._reference(base.profile())
    store=live.Store()
    try:
        records=store.rows('cycles');summary=[];snapshots=[];departments=[]
        for row in records:
            data=cycle.payload(row);item={'cycle':row['cycle_id'],'scope':row['test_scope'],'status':row['status']}
            if row['cycle_id'].startswith('lp-'):item.update(observation=data.get('observation'),blockers=data.get('blockers'),event_counts=data['document']['event_counts'],continuation=data['document'].get('continuation'),key_anomalies=data.get('key_anomalies',[]))
            if row['cycle_id'].startswith('ls-'):
                current=cycle.clean(store.rows('slow_baseline',data['scope']))
                if base.digest(base.normalized('slow_baseline',current))!=data['freeze_digest']:raise ValueError('LIVE_FREEZE_STATUS_MISMATCH')
                role_count=len(base.wf.report_recipients(reference['regions'],data['department']))
                if not role_count:raise ValueError('REPORT_ROLE_PLAN_EMPTY')
                info={'department':data['department'],'cycle':row['cycle_id'],'status':row['status'],'frozen_rows':len(current),'frozen_at':data['frozen_at'],'observation':data['observation'],'report_observation':data.get('report_observation'),'phase_index':data['phase_index'],'total_customer_packages':data.get('total_packages'),'selected_customer_packages':len(data.get('selected_packages',[])),'original_report_role_count':role_count,'phases':{k:{'prepared':True,'receipt':v.get('receipt'),'dedup_review':v.get('dedup_review'),'evidence':v['evidence']} for k,v in data['phases'].items()}}
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
        receipt_entries=[]
        for row in records:
            data=cycle.payload(row)
            if row['cycle_id'].startswith('ls-'):
                receipt_entries.extend((phase.get('receipt'),phase) for phase in data.get('phases',{}).values() if phase.get('receipt'))
            if row['cycle_id'].startswith('lp-') and data.get('receipt',{}).get('batch_receipts'):
                notices=data.get('notices') or []
                for raw_index,receipt in data['receipt']['batch_receipts'].items():
                    try:index=int(raw_index)
                    except (TypeError,ValueError):index=-1
                    manifest={'notices':notices[index*2:index*2+2],'files':{}}
                    receipt_entries.append((receipt,manifest))
        from . import workflow_delivery_review as review
        receipt_summary=review.summarize_receipts(receipt_entries)
        result['delivery_evidence']={'schema':'delivery-evidence/v2','logical_notifications':receipt_summary['fresh_provider_logical_notifications'],'components':receipt_summary['fresh_provider_components'],'recorded_logical_notifications':receipt_summary['recorded_logical_notifications'],'recorded_components':receipt_summary['recorded_components'],'verified_logical_notifications':receipt_summary['verified_logical_notifications'],'verified_components':receipt_summary['verified_components'],'verified_manifest_scope':'recorded_cycle_manifests','historical_logical_notifications':receipt_summary['historical_logical_notifications'],'historical_components':receipt_summary['historical_components'],'unverified_logical_notifications':receipt_summary['unverified_logical_notifications'],'unverified_components':receipt_summary['unverified_components'],'reused_prior_receipts':receipt_summary['reused_prior_receipts'],'receipt_count':receipt_summary['receipt_count'],'verified_receipt_count':receipt_summary['verified_receipt_count'],'historical_provider_accepted_count':receipt_summary['historical_provider_accepted_count'],'unverified_receipt_count':receipt_summary['unverified_receipt_count'],'actual_component_states_verified':receipt_summary['all_component_receipts_verified'],'actual_component_states_verified_basis':'recorded_manifest_binding','human_read':'unknown'}
        live.save('status.json',result)
        compact={**{k:result[k] for k in ('mode','production_enabled','clock','snapshots','runtime','schedule_enabled','delivery_evidence')},
                 'price_cycles':[r for r in summary if r['cycle'].startswith('lp-')],
                 'departments':[{'department':d['department'],'frozen_at':d['frozen_at'],'initial_source_observed_at':d['observation']['observed_at'],'report_source_observed_at':(d.get('report_observation') or d['observation'])['observed_at'],'report_source_stock_rows':(d.get('report_observation') or d['observation'])['stock_rows'],'report_source_monthly_rows':(d.get('report_observation') or d['observation'])['monthly_rows'],'frozen_rows':d['frozen_rows'],'stock_rows':d['observation']['stock_rows'],'monthly_rows':d['observation']['monthly_rows'],'status':d['status'],'phase_index':d['phase_index'],'weekly_groups':d['phases'].get('weekly',{}).get('evidence',{}).get('completeness',{}).get('pool_groups'),'monthly_groups':d['phases'].get('monthly',{}).get('evidence',{}).get('completeness',{}).get('pool_groups'),'labels_complete':all(v.get('evidence',{}).get('label_coverage',{}).get('complete',True) for v in d['phases'].values())} for d in departments]}
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
    if action in ('customer-audit','customer-coverage','customer-repair','customer-samples','customer-sample-send'):
        from . import workflow_customer_audit as audit
        return {'customer-audit':audit.run,'customer-coverage':audit.compact_and_summarize,'customer-repair':audit.repair_artifacts,'customer-samples':audit.prepare_samples,'customer-sample-send':audit.send_samples}[action]()
    if action=='init':return live.bootstrap()
    if action in ('anomaly-evidence','continuity-replay'):
        from . import workflow_price_diagnostics
        return workflow_price_diagnostics.run() if action=='anomaly-evidence' else workflow_price_diagnostics.replay()
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
