"""Installed local workflow entry: synthetic or real-source acceptance, never production mode."""
import argparse,json,sys,hashlib
from pathlib import Path
from . import local_report,workflow_storage as storage,workflow_fixture as fixture,workflow_cycle as cycle

def status():
    store=storage.Store()
    try:
        rows=store.rows('cycles');proof=[]
        for side in ('sales','purchase'):
            for scope in cycle.SCOPES:
                group=cycle.journals(store,side,scope)
                if not group:continue
                latest=max(group,key=lambda r:int(r['cycle_id'].rsplit('-',1)[1]));data=cycle.payload(latest)
                expected=data['after_digest'] if latest['status']=='committed' else data['before_digest']
                if cycle.snapshot_digest(side,store.rows(side+'_snapshot',scope))!=expected:raise ValueError('STATUS_SNAPSHOT_MISMATCH')
                proof.append({'cycle':latest['cycle_id'],'status':latest['status'],'snapshot_verified':True})
        receipts=[]
        for row in rows:
            data=cycle.payload(row)
            if row['cycle_id'].startswith('slow-'):
                receipts.extend((phase['receipt'],phase,row['cycle_id']) for phase in data.get('phases',{}).values() if 'receipt' in phase)
            elif data.get('real_transport') and data.get('receipt',{}).get('status')=='provider_accepted_not_human_read':receipts.append((data['receipt'],data,row['cycle_id']))
        from . import workflow_delivery_review as review
        receipt_summary=review.summarize_receipts(receipts)
        modules={}
        for name,module in list(sys.modules.items()):
            if name.startswith(__package__+'.workflow_'):
                path=Path(module.__file__).resolve()
                if not path.is_relative_to(storage.profile().resolve()):raise ValueError('WORKFLOW_MODULE_OUTSIDE_PROFILE')
                modules[name.rsplit('.',1)[-1]]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        result={'mode':'isolated_acceptance','production_enabled':False,'tables':[{'table':storage.TABLES[r],'rows':len(store.rows(r))} for r in storage.ROLES],
                'cycles':[{'cycle':r['cycle_id'],'scope':r['test_scope'],'status':r['status']} for r in rows],'latest_snapshot_proof':proof,
                'delivery_evidence':{'schema':'delivery-evidence/v2','real_logical_notifications':receipt_summary['provider_logical_notifications'],'real_components':receipt_summary['provider_components'],'recorded_logical_notifications':receipt_summary['recorded_logical_notifications'],'recorded_components':receipt_summary['recorded_components'],'verified_logical_notifications':receipt_summary['verified_logical_notifications'],'verified_components':receipt_summary['verified_components'],'verified_manifest_scope':'recorded_cycle_manifests','historical_logical_notifications':receipt_summary['historical_logical_notifications'],'historical_components':receipt_summary['historical_components'],'unverified_logical_notifications':receipt_summary['unverified_logical_notifications'],'unverified_components':receipt_summary['unverified_components'],'receipt_count':receipt_summary['receipt_count'],'verified_receipt_count':receipt_summary['verified_receipt_count'],'historical_provider_accepted_count':receipt_summary['historical_provider_accepted_count'],'unverified_receipt_count':receipt_summary['unverified_receipt_count'],'all_component_receipts_verified':receipt_summary['all_component_receipts_verified'],'human_read':'unknown'},
                'runtime':{'profile':str(storage.profile()),'entry':str(storage.profile()/'scripts/datasage_workflow.py'),'python':sys.executable,'working_directory':str(Path.cwd()),'loaded_workflow_modules':modules},'limitations':['application allowlist, not least-privilege DB account','existing canary connection has no TLS','bounded HCM sample and synthetic price events']}
        storage.save('status.json',result);return result
    finally:store.close()
def main(profile,argv):
    parser=argparse.ArgumentParser(description='Profile fixed workflow entry; production mode unavailable.')
    parser.add_argument('--mode',required=True,choices=['test','live'])
    parser.add_argument('action',choices=['diagnose','roles-check','roles-import','customer-audit','customer-coverage','customer-repair','customer-samples','customer-sample-send','manual-boundaries','init','seed','slow','prices','change-fixture','status','recovery-check','fault-exit','observe-prices','deliver-prices','anomaly-evidence','continuity-replay','slow-prepare','slow-preview','slow-deliver','slow-new-generation','schedule-plan','scheduled-tick','lock-check'])
    parser.add_argument('--department',choices=tuple(storage.wf.policy()['regions']))
    parser.add_argument('--job',choices=['sales','purchase','slow-task','slow-report'])
    parser.add_argument('--reason')
    parser.add_argument('--source',help='Explicit private role source; read only unless roles-import is selected.')
    parser.add_argument('--output',help='Explicit roles-import destination inside the Profile private local directory.')
    args=parser.parse_args(argv)
    local_report._assert_local_context()
    if profile.resolve()!=storage.profile().resolve():raise ValueError('WORKFLOW_PROFILE_MISMATCH')
    if args.action in ('roles-check','roles-import'):
        if any((args.department,args.job,args.reason)):raise ValueError('ROLE_ARGUMENT_CONFLICT')
        from . import workflow_roles
        try:
            if args.action=='roles-import':
                if not args.source or not args.output:raise ValueError('ROLE_IMPORT_EXPLICIT_PATHS_REQUIRED')
                result=workflow_roles.import_legacy(Path(args.source).resolve(),Path(args.output).resolve(),profile)
            else:
                if args.output:raise ValueError('ROLE_ARGUMENT_CONFLICT')
                result=workflow_roles.prepare(Path(args.source).resolve(),profile) if args.source else workflow_roles.diagnose(profile)
        except workflow_roles.RoleConfigurationError as exc:
            print(json.dumps({'status':'blocked','code':exc.code},ensure_ascii=False));return 2
        print(json.dumps(result,ensure_ascii=False));return 2 if result.get('status')=='blocked' else 0
    if args.source or args.output:raise ValueError('ROLE_ARGUMENTS_REQUIRE_ROLE_ACTION')
    if args.action=='diagnose':
        if any((args.department,args.job,args.reason)):raise ValueError('DIAGNOSTIC_ARGUMENT_CONFLICT')
        from . import runtime_health
        print(json.dumps(runtime_health.source_diagnostics(),ensure_ascii=False));return 0
    if args.mode=='live':
        from . import workflow_live_store as live,workflow_live_runner
        path=live.binding()
        if path.is_symlink() or not path.is_file():raise ValueError('LIVE_BINDING_REQUIRED')
        live.validate_binding(json.loads(path.read_text(encoding='utf-8')))
        local_report.configure_runtime(profile)
        result=workflow_live_runner.run(args.action,department=args.department,job=args.job,reason=args.reason)
        print(json.dumps(result,ensure_ascii=False,default=str));return 0
    if args.action not in ('manual-boundaries','init','seed','slow','prices','change-fixture','status','recovery-check','fault-exit') or any((args.department,args.job,args.reason)):raise ValueError('FIXTURE_ACTION_REJECTED')
    # Fail closed before credentials/DB; init uses the same explicit binding.
    path=storage.binding_path()
    if path.is_symlink() or not path.is_file():raise ValueError('TEST_BINDING_REQUIRED')
    storage.assert_binding(json.loads(path.read_text(encoding='utf-8')))
    local_report.configure_runtime(profile)
    if args.action=='manual-boundaries':
        from . import workflow_manual_boundaries
        result=workflow_manual_boundaries.run()
    elif args.action=='init':result=storage.bootstrap()
    elif args.action=='seed':result=fixture.seed()
    elif args.action=='prices':result={side:cycle.run(side) for side in ('sales','purchase')}
    elif args.action=='change-fixture':result={side:fixture.change(side) for side in ('sales','purchase')}
    elif args.action=='slow':
        from . import workflow_slow
        result=workflow_slow.run()
    elif args.action=='status':result=status()
    else:
        from . import workflow_recovery_check as checks
        result=cycle.run('sales','fault-interrupt',send=checks.sender(),fault=checks.terminate_after_plan) if args.action=='fault-exit' else checks.run()
    print(json.dumps(result,ensure_ascii=False,default=str));return 0
