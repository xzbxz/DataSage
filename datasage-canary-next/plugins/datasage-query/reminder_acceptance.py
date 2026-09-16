"""Local reminder acceptance cases. Prepare and send are separate fixed actions."""
from pathlib import Path
from datetime import datetime
import json,hashlib,re,shutil,uuid
from . import acceptance_delivery as delivery,report_evidence,workflow_inputs,legacy_workflow as wf,operations
from .workflow_io import IOErrorBoundary,private_root,run_lock

WEEK='2026-W38';MONTH='2026-09'
CASES=('idk-current','hcm-task','hcm-weekly','hcm-monthly','sales-observation','purchase-observation',
       'sales-personalized-synthetic','sales-manager-no-buyers-synthetic','purchase-private-group-synthetic',
       'failure-synthetic','hcm-customer-packages','hcm-dispatch-audit','customer-package-audit-synthetic')

def _root(profile):return private_root(delivery.runtime_home(profile))
def _case(profile,case):
    if case not in CASES:raise IOErrorBoundary('ACCEPTANCE_CASE_NOT_REGISTERED')
    path=_root(profile)/('case-'+case);path.mkdir(exist_ok=True);return path
def _read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def _reference(profile):
    path=delivery.runtime_home(profile)/'legacy-recipient-reference.json'
    if path.is_symlink() or not path.is_file():raise IOErrorBoundary('LEGACY_RECIPIENT_REFERENCE_REQUIRED')
    return _read(path)
def _notice(logical,role,body,files=(),*,channel='private',**extra):
    return {'logical_id':logical,'role':role,'body':body,'attachments':[str(f) for f in files],'channel':channel,**extra}

def stage(profile,case,notices,evidence):
    folder=_case(profile,case)
    if not 1<=len(notices)<=2:raise IOErrorBoundary('ACCEPTANCE_CASE_BATCH_LIMIT')
    if evidence.get('origin') not in ('current_readonly','synthetic','cached_observation'):
        raise IOErrorBoundary('ACCEPTANCE_CASE_ORIGIN_REQUIRED')
    files={}
    for notice in notices:
        delivery.notification_parts(notice,case) # Shape/role safety before persisting.
        for filename in notice['attachments']:
            name,data,mime,digest=delivery.file_snapshot(filename,delivery.runtime_home(profile))
            files[filename]={'sha256':hashlib.sha256(data).hexdigest(),'semantic_digest':digest}
    manifest={'case_id':case,'status':'prepared','evidence':evidence,'notices':notices,'files':files,
              'source_content_digest':wf.digest(notices),'production_enabled':False}
    operations._atomic(folder/'manifest.json',manifest);return manifest

def prepare(profile,case):
    from .local_report import _assert_local_context,configure_runtime
    from . import tools
    from .legacy_xlsx import gen_workbook_xlsx
    _assert_local_context();delivery.load_settings(profile);configure_runtime(profile)
    if tools._business_today().isocalendar()[:2]!=(2026,38):raise IOErrorBoundary('ACCEPTANCE_CYCLE_EXPIRED')
    folder=_case(profile,case)
    with run_lock(delivery.runtime_home(profile),'prepare-'+case):
        if (folder/'manifest.json').exists():
            previous=_read(folder/'manifest.json')
            if case in ('sales-observation','purchase-observation') and previous.get('status')=='no_accepted_price_reference_no_alert':
                if (folder/'send-result.json').exists():raise IOErrorBoundary('OLD_PRICE_CASE_HAS_DELIVERY_STATE')
                operations._atomic(folder/'manifest.before-legacy-database-reference.json',previous)
            else:return previous
        reference=_reference(profile)
        if case in ('idk-current','sales-observation','purchase-observation'):
            old_name={'idk-current':'idk','sales-observation':'sales_price','purchase-observation':'purchase_price'}[case]
            cached=_root(profile)/('observation-'+old_name)/'observation.json'
            if (folder/'observation.json').exists():cached=folder/'observation.json'
            kind={'idk-current':'idk_unpriced','sales-observation':'sales_prices','purchase-observation':'purchase_prices'}[case]
            legacy_path=_root(profile)/'legacy-database-price-reference'/('sales-observation.json' if case=='sales-observation' else 'purchase-observation.json')
            if case!='idk-current' and legacy_path.exists():doc=_read(legacy_path)
            elif case=='idk-current' and cached.exists():doc=_read(cached)
            else:
                binding={'kind':kind,'limit':10000,**({} if case=='idk-current' else {'regions':['HCM','HN','BKK','IDK'],'reference_source':'legacy_database'})}
                doc=operations.execute(delivery.runtime_home(profile),'acceptance-'+old_name,binding)
            if case!='idk-current' and (folder/'observation.json').exists():
                old_doc=_read(folder/'observation.json')
                if old_doc.get('baseline_source')!='legacy_database':operations._atomic(folder/'observation.before-legacy-database-reference.json',old_doc)
            operations._atomic(folder/'observation.json',doc)
            data=wf.operation_preview_input(old_name,doc)
            if case!='idk-current':
                if data['changes']:return prepare_price_events(profile,case,doc,data,reference,folder)
                manifest={'case_id':case,'status':'no_deliverable_price_change','evidence':{'origin':'current_readonly','baseline_source':doc.get('baseline_source'),
                    'observed_at':doc['observed_at'],'source_rows':doc['source_rows'],'event_counts':doc['event_counts'],'reference':doc.get('reference')},
                    'price_baseline_accepted':False,'sent':False,'production_enabled':False}
                operations._atomic(folder/'manifest.json',manifest);return manifest
            bundle=wf.build_preview('idk',data,folder)
            recipients=[r['account'] for r in reference['regions']['IDK']['executors']]
            return stage(profile,case,[_notice('idk-executors-same-content','IDK原执行人（同文合并'+str(len(recipients))+'人）',bundle['message_bodies'][0],message_format='markdown')],
                {'origin':'current_readonly','scope':'IDK all current unpriced source rows','observed_at':doc['observed_at'],'source_rows':doc['source_rows'],'original_recipients':recipients})
        if case in ('hcm-weekly','hcm-monthly'):
            phase='weekly' if case=='hcm-weekly' else 'monthly';period=WEEK if phase=='weekly' else MONTH
            cached=_root(profile)/('observation-'+case.replace('-','_'))
            if (cached/'evidence.json').exists():evidence=_read(cached/'evidence.json');labels=_read(cached/'labels.json')
            else:evidence,labels=report_evidence.collect('HCM',period,phase,WEEK)
            operations._atomic(folder/'query-evidence.json',evidence)
            adapted=workflow_inputs.legacy_report_packet(evidence['packets']['pool'],evidence['packets']['flow'],labels,'HCM',period,evidence=evidence)
            operations._atomic(folder/'validation.json',adapted)
            text=wf.report_draft('HCM',period,adapted['summary'],adapted['sales_rows'],monthly=phase=='monthly')
            file=folder/('HCM_'+period+'_Report.xlsx');gen_workbook_xlsx([('Detail',wf.REPORT_HEADERS,adapted['detail_rows'])],file,borders=True,landscape=True)
            recipients=wf.report_recipients(reference['regions'],'HCM')
            role='HCM原执行人及管理层（同文合并'+str(len(recipients))+'人）；截至'+adapted['completeness']['observed_to']+'，非期末最终结果'
            if not adapted['label_coverage']['complete']:role+='；标签Unknown待核验，不称完整可上线报表'
            return stage(profile,case,[_notice(case+'-roles',role,text,[file],message_format='markdown')],{'origin':'current_readonly','period':period,
                'observed_at':adapted['completeness']['observed_to'],'numeric_complete':True,'label_coverage':adapted['label_coverage'],
                'original_recipients':recipients,'read_only_existing_baseline':True})
        if case=='hcm-task':
            with tools._ConsistentSnapshotExecutor(deadline_at=tools._call_deadline(None)) as db:
                sql='SELECT '+','.join(wf.BASELINE_COLUMNS)+',frozen_at FROM vk_ai.slow_moving_baseline WHERE week_label=%s AND whse_dept=%s ORDER BY source_row_id LIMIT 10001'
                baseline=workflow_inputs.complete(db,sql,[WEEK,'HCM'])
                clock=workflow_inputs.complete(db,'SELECT NOW(6) AS at',limit=1)[0]['at']
                depts=reference['regions']['HCM']['dynamic_sales_departments']
                employees=workflow_inputs.complete(db,"SELECT region,main_dept,person_name,wecom_account,position,is_delete,wecom_status FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_status='payroll' AND COALESCE(wecom_account,'')<>'' AND region=%s AND main_dept IN ("+','.join('%s' for _ in depts)+') ORDER BY wecom_account LIMIT 10001',['HCM',*depts])
            if not baseline or len({r['source_row_id'] for r in baseline})!=len(baseline) or len({str(r['frozen_at']) for r in baseline})!=1:
                raise IOErrorBoundary('ACCEPTANCE_EXISTING_BASELINE_INVALID')
            if any(r['baseline_version']!=2 or r['source_table']!='vk_ods.slow_moving_goods_ods' for r in baseline):raise IOErrorBoundary('ACCEPTANCE_BASELINE_IDENTITY_INVALID')
            checked=wf.freeze_plan([{**r,'id':r['source_row_id'],'goods_num':r['total_qty'],'piece_num':r['total_piece'],'is_whitelist':'n'} for r in baseline],[],WEEK,WEEK)
            if checked['status']!='ready':raise IOErrorBoundary('ACCEPTANCE_BASELINE_FIELDS_INCOMPLETE')
            operations._atomic(folder/'baseline.json',baseline)
            targets=wf.task_recipients(reference['regions'],employees,['HCM'])['HCM']
            periods=wf.legacy_periods(delivery.workflow.at_utc8(WEEK))
            text,sheet=wf.task_draft('HCM',WEEK,periods['planned_start'][:10],periods['planned_end'][:10],baseline)
            file=folder/('HCM_'+WEEK+'_Products.xlsx');gen_workbook_xlsx([sheet],file)
            return stage(profile,case,[_notice('hcm-task-all-roles','HCM原执行人/在职销售客服/管理层（同文合并'+str(len(targets))+'人），使用已有冻结，未重冻',text,[file])],
                {'origin':'current_readonly','baseline_week':WEEK,'baseline_rows':len(baseline),'frozen_at':str(baseline[0]['frozen_at']),'observed_at':str(clock),'original_recipients':targets,'frozen':False})
        if case=='hcm-customer-packages':
            task=_case(profile,'hcm-task')
            if not (task/'send-result.json').exists() or _read(task/'send-result.json').get('status')!='provider_accepted_not_human_read':
                raise IOErrorBoundary('ACCEPTANCE_TASK_STAGE_NOT_ACCEPTED')
            baseline=_read(task/'baseline.json')
            with tools._ConsistentSnapshotExecutor(deadline_at=tools._call_deadline(None)) as db:mapping=workflow_inputs.customer_mapping(db,[(r['goods_no'],r['whse_dept']) for r in baseline])
            plan=wf.contact_plan(baseline,mapping);operations._atomic(folder/'full-hcm-plan.json',plan)
            packages=plan['sales_packages'][:2]
            if not packages:
                result={'case_id':case,'status':'no_eligible_customer_packages','sent':False};operations._atomic(folder/'manifest.json',result);return result
            notices=[]
            build=folder/('build-'+uuid.uuid4().hex);build.mkdir()
            for package in packages:
                archive=wf.customer_zip(package,build,WEEK)
                notices.append(_notice('sales-'+wf.account_token(package['account'])[:12],'原销售客户包，HCM范围；仅抽验前'+str(len(packages))+'个完整逻辑包，共'+str(len(plan['sales_packages']))+'个待验',wf.customer_package_message(package,WEEK),[archive]))
            return stage(profile,case,notices,{'origin':'current_readonly','scope':'HCM baseline products, existing 12-calendar-month buyer rules','total_logical_packages':len(plan['sales_packages']),'selected_original_accounts':[p['account'] for p in packages]})
        if case=='hcm-dispatch-audit':
            source=_case(profile,'hcm-customer-packages')
            if not (source/'send-result.json').exists() or _read(source/'send-result.json').get('status')!='provider_accepted_not_human_read':
                raise IOErrorBoundary('ACCEPTANCE_CUSTOMER_STAGE_NOT_ACCEPTED')
            manifest=_read(source/'manifest.json');plan=_read(source/'full-hcm-plan.json')
            accounts=manifest['evidence']['selected_original_accounts']
            packages=[p for p in plan['sales_packages'] if p['account'] in accounts]
            if not 1<=len(packages)<=2 or len(packages)!=len(set(accounts)):raise IOErrorBoundary('ACCEPTANCE_AUDIT_SAMPLE_MISMATCH')
            build=folder/('build-'+uuid.uuid4().hex);build.mkdir()
            audits=delivery.workflow.dispatch_audits(packages,{p['account']:'provider_accepted' for p in packages},reference['regions'],WEEK,build)
            notices=[_notice('sample-dispatch-'+str(i),'所选客户包派发核对；回执仅代表测试成员接收，非原销售/客户接收；其余'+str(len(plan['sales_packages'])-len(packages))+'包未抽验',
                audit['text'],[audit['path']]) for i,audit in enumerate(audits)]
            return stage(profile,case,notices,{'origin':'current_readonly','source_customer_case':'hcm-customer-packages',
                'selected_packages':len(packages),'unselected_packages_not_attempted':len(plan['sales_packages'])-len(packages),
                'original_audit_roles':[audit['targets'] for audit in audits],'actual_recipient':'zhangzhengwei','human_receipt':'unknown'})
        if case=='sales-personalized-synthetic':
            change={'goods_no':'SYN-ONLY-001','dept':'HCM','customer_grade':'A','color_label':'Red','old_ddp_price':12,'new_ddp_price':10,'currency_no':'CNY'}
            notices=[]
            for code in ('A','B'):
                sub=folder/code;sub.mkdir(exist_ok=True)
                data={'evidence_origin':'synthetic','region':'HCM','sales_name':'Synthetic Sales '+code,'changes':[change],
                    'customer_mapping_complete':True,'customers_by_goods':{'SYN-ONLY-001':[['SYN-C-'+code,'合成客户'+code]]}}
                bundle=wf.build_preview('sales_price',data,sub)
                notices.append(_notice('synthetic-sales-'+code,'销售个性化客户清单合成演练，非真实变价',bundle['message_bodies'][0],[sub/f for f in bundle['files'] if f.endswith('.xlsx')]))
            return stage(profile,case,notices,{'origin':'synthetic','purpose':'same actual recipient, distinct logical users and personalized attachments'})
        if case=='sales-manager-no-buyers-synthetic':
            from .legacy_message_templates import sales
            change={'goods_no':'SYN-ONLY-002','dept':'HCM','customer_grade':'A','color_label':'Blue','old_ddp_price':10,'new_ddp_price':11,'currency_no':'CNY'}
            file=folder/'Synthetic_HCM_Manager_Customers.xlsx'
            gen_workbook_xlsx([('SYN-ONLY-002',['Sales','Customer No','Customer'],[['Synthetic Sales A','SYN-C-A','合成客户A'],['Synthetic Sales B','SYN-C-B','合成客户B']])],file)
            manager=sales([change],manager=True,region='HCM')+'\n\nSee attachment for all sales\' customers across this region (one Sheet per product).'
            no_buyers=sales([change])+'\n\nYou have no customers who purchased these products, so no attachment is included.'
            return stage(profile,case,[_notice('synthetic-region-manager','区域管理层跨销售清单合成演练，非真实变价',manager,[file]),
                _notice('synthetic-sales-no-buyers','区域内销售无关联买客合成演练，仍发正文且无附件',no_buyers)],{'origin':'synthetic','purpose':'regional manager columns versus no-buyer sales message'})
        if case=='customer-package-audit-synthetic':
            package={'account':'synthetic-sales','sales_name':'Synthetic Sales','sales_names':['Synthetic Sales'],'region':'HCM',
                'customers':[{'customer_id':'SYN-1','customer_no':'SYN-C1','customer_name':'合成客户甲','products':[['SYN-ONLY-001','Red',3]]}]}
            build=folder/('build-'+uuid.uuid4().hex);build.mkdir()
            archive=wf.customer_zip(package,build,WEEK)
            audits=delivery.workflow.dispatch_audits([package],{'synthetic-sales':'provider_accepted'},
                {'HCM':{'executors':[{'account':'synthetic-exec','name':'Executor'}],'managers':['synthetic-manager']}},WEEK,build)
            return stage(profile,case,[_notice('synthetic-customer-package','销售客户图片ZIP合成演练，一客一图，不含真实客户',wf.customer_package_message(package,WEEK),[archive]),
                _notice('synthetic-dispatch-audit','区域派发核对合成演练，回执为模拟，不表示真实客户触达',audits[0]['text'],[audits[0]['path']])],
                {'origin':'synthetic','purpose':'one PNG per customer / ZIP per sales / regional audit schema','simulated_receipts':True})
        if case=='purchase-private-group-synthetic':
            changes=[{'goods_no':'SYN-ONLY-'+str(n),'goods_name':'合成面料','supplier_no':'SYN-S','supplier_name':'合成供应商','color_label':'Red','old_inc':old,'new_inc':new,'old_exc':old-2,'new_exc':new-2,'currency_no':'CNY','unit_cuur':'m','adjust_date':'2026-09-16'} for n,old,new in [(1,12,10),(2,10,13)]]
            body=wf.price_draft('purchase',changes)
            return stage(profile,case,[_notice('synthetic-purchase-private','采购报价私信格式合成演练，非真实变价',body,message_format='markdown'),
                _notice('synthetic-purchase-group','采购报价群Markdown及单独@all合成演练，非真实变价',body,channel='group',message_format='markdown',mention_all=True)],
                {'origin':'synthetic','old_external_schedule_and_target':'not_in_git_not_invented','purpose':'old dual transport format and decrease-before-increase'})
        if case=='failure-synthetic':
            return stage(profile,case,[_notice('synthetic-task-failure','旧故障通知收件角色（合成故障，未发生真实业务失败）',
                'DataSage slow-moving task failed. Please check the local cron log. Error: RuntimeError')],
                {'origin':'synthetic','original_recipients':['zhangzhengwei'],'purpose':'legacy failure message, no intentional production failure'})
        raise IOErrorBoundary('ACCEPTANCE_CASE_NOT_IMPLEMENTED')

def prepare_price_events(profile,case,document,data,reference,folder):
    """Old role and buyer logic first; at most two whole logical notices sampled."""
    from . import tools
    changes=data['changes'];notices=[]
    if case=='purchase-observation':
        # The external old user/webhook selection is not in Git. This is a review
        # notice to the approved member, not an invented production group route.
        body=wf.price_draft('purchase',changes)
        notices=[_notice('purchase-legacy-database-review','旧采购目标外部配置待核实；本次仅测试成员核验真实记录报价变化；有效期未证实',body,message_format='markdown')]
        original_count=1
    else:
        affected=sorted({c['dept'] for c in changes})
        if not set(affected)<=set(reference['regions']):raise IOErrorBoundary('PRICE_REGION_NOT_IN_LEGACY_RULES')
        departments=sorted({d for region in affected for d in reference['regions'][region]['dynamic_sales_departments']})
        with tools._ConsistentSnapshotExecutor(deadline_at=tools._call_deadline(None)) as db:
            employees=workflow_inputs.complete(db,"SELECT region,main_dept,person_name,wecom_account,position,is_delete,wecom_status FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_status='payroll' AND COALESCE(wecom_account,'')<>'' AND region IN ("+','.join('%s' for _ in affected)+') AND main_dept IN ('+','.join('%s' for _ in departments)+') ORDER BY wecom_account LIMIT 10001',[*affected,*departments])
            executors=sorted({e['account'] for region in affected for e in reference['regions'][region]['executors']})
            if executors:employees+=workflow_inputs.complete(db,"SELECT wecom_account,position,is_delete FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_account IN ("+','.join('%s' for _ in executors)+') LIMIT 10001',executors)
            mapping=workflow_inputs.customer_mapping(db,[(c['goods_no'],c['dept']) for c in changes])
        plan=wf.price_recipients(reference['regions'],employees,affected,reference.get('price_manager_fixed',[]))
        specs=[]
        for region in affected:
            regional=[c for c in changes if c['dept']==region];buyers={}
            for cid,products in mapping['productsByCustomer'].items():
                customer=mapping['customerInfo'].get(cid,{})
                for c in regional:
                    if customer.get('customer_no') and any(p['goods_no']==c['goods_no'] and p['whse_dept']==region for p in products):
                        row=(customer.get('sales'),customer['customer_no'],customer.get('name'));items=buyers.setdefault(c['goods_no'],[])
                        if any(v[1]==row[1] and v!=row for v in items):raise IOErrorBoundary('BUYER_CUSTOMER_NUMBER_AMBIGUOUS')
                        if row not in items:items.append(row)
            for target in plan[region]['sales']:
                own={g:[[no,name] for owner,no,name in rows if mapping['wecomBySales'].get(owner)==target['account']] for g,rows in buyers.items()}
                specs.append({'region':region,'account':target['account'],'role':'sales','changes':regional,'customers_by_goods':own,'sales_name':target['name']})
            if buyers:
                for account in plan[region]['managers']:specs.append({'region':region,'account':account,'role':'manager','changes':regional,'manager_rows_by_goods':buyers})
        if not specs or len(specs)>200:raise IOErrorBoundary('LEGACY_PRICE_LOGICAL_PLAN_UNAVAILABLE_OR_TOO_LARGE')
        operations._atomic(folder/'original-logical-plan.json',specs)
        # Prefer one full sales notice and one full manager notice if present.
        selected=specs[:1]
        other=next((s for s in specs[1:] if s['role']!=selected[0]['role']),None)
        if other is not None:selected.append(other)
        elif len(specs)>1:selected.append(specs[1])
        for index,spec in enumerate(selected):
            build=folder/('event-'+str(index));build.mkdir(exist_ok=True)
            bundle=wf.build_preview('sales_price',{'evidence_origin':'existing_local_observation','customer_mapping_complete':True,**spec},build)
            body=bundle['message_bodies'][-1] if spec['role']=='manager' else bundle['message_bodies'][0]
            notices.append(_notice('sales-'+spec['region']+'-'+spec['role']+'-'+wf.account_token(spec['account'])[:12],
                spec['region']+'原'+spec['role']+'角色；仅证实名义DDP字段变化，历史单位/税标记未存储，不将当前字段补为历史',body,[build/f for f in bundle['files'] if f.endswith('.xlsx')]))
        original_count=len(specs)
    return stage(profile,case,notices,{'origin':'current_readonly','baseline_source':'legacy_database','reference':document['reference'],
        'observed_at':document['observed_at'],'deliverable_events':document['deliverable_event_count'],
        'original_logical_notification_count':original_count,'selected_logical_notifications':len(notices),'comparison_disclosure':document['scope_notice']})

def send(profile,case):
    from .local_report import _assert_local_context
    _assert_local_context();folder=_case(profile,case)
    if case=='hcm-monthly':
        previous=_case(profile,'hcm-weekly')/'send-result.json'
        if not previous.exists() or _read(previous).get('status')!='provider_accepted_not_human_read':raise IOErrorBoundary('ACCEPTANCE_WEEKLY_STAGE_NOT_ACCEPTED')
    manifest=_read(folder/'manifest.json')
    if manifest.get('status')!='prepared':return {'case_id':case,'status':manifest.get('status'),'sent':False}
    if manifest.get('case_id')!=case or wf.digest(manifest['notices'])!=manifest['source_content_digest']:
        raise IOErrorBoundary('ACCEPTANCE_STAGED_CONTENT_CHANGED')
    for filename,expected in manifest['files'].items():
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest()!=expected['sha256']:raise IOErrorBoundary('ACCEPTANCE_STAGED_FILE_CHANGED')
    result=delivery.deliver_batch(profile,case+'-w38-v1',manifest['notices'])
    operations._atomic(folder/'send-result.json',result);return result

def main(profile,argv):
    if argv==['--list']:print(json.dumps({'cases':CASES,'mode':'acceptance_only'}));return 0
    if len(argv)!=2 or argv[0] not in ('--prepare','--send') or argv[1] not in CASES:
        print('ACCEPTANCE_ACTION_REQUIRED');return 2
    try:
        result=(prepare if argv[0]=='--prepare' else send)(profile,argv[1])
        print(json.dumps({k:v for k,v in result.items() if k not in ('notices','files')},ensure_ascii=False,default=str));return 0
    except Exception as exc:
        folder=_case(profile,argv[1]);result={'case_id':argv[1],'status':'blocked','code':str(exc) if isinstance(exc,(wf.WorkflowError,operations.OperationError)) else type(exc).__name__,'detail':getattr(exc,'evidence',{})}
        operations._atomic(folder/'last-error.json',result);print(json.dumps(result,ensure_ascii=False));return 2
