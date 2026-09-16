"""Finite remaining-department checks; no production switch, freeze or schedule."""
from pathlib import Path
from datetime import datetime
import json,hashlib
from . import acceptance_delivery as delivery,reminder_acceptance as session
from . import report_evidence,workflow_inputs,legacy_workflow as wf,operations
from .workflow_io import IOErrorBoundary,run_lock

REGIONS=('HN','BKK','IDK','HCM-HT','HN-HT','IDK-HT','BKK-HT')
PHASES=('task','weekly','monthly')

def folder(profile,region):
    if region not in REGIONS:raise IOErrorBoundary('REGIONAL_ACCEPTANCE_SCOPE_NOT_ALLOWED')
    root=session._root(profile)/('regional-'+region);root.mkdir(exist_ok=True);return root

def seal(root,phase,notice,evidence):
    files={name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in notice['attachments']}
    result={'status':'prepared','phase':phase,'notice':notice,'notice_digest':wf.digest(notice),'files':files,'evidence':evidence}
    operations._atomic(root/phase/'manifest.json',result);return result

def prepare(profile,region):
    from .local_report import _assert_local_context,configure_runtime
    from . import tools
    from .legacy_xlsx import gen_workbook_xlsx
    if region not in REGIONS:raise IOErrorBoundary('REGIONAL_ACCEPTANCE_SCOPE_NOT_ALLOWED')
    _assert_local_context();delivery.load_settings(profile);configure_runtime(profile)
    if tools._business_today().isocalendar()[:2]!=(2026,38):raise IOErrorBoundary('ACCEPTANCE_CYCLE_EXPIRED')
    root=folder(profile,region);reference=session._reference(profile);results=[]
    with run_lock(delivery.runtime_home(profile),'regional-prepare-'+region):
        for phase in PHASES:
            out=root/phase;out.mkdir(exist_ok=True)
            if (out/'manifest.json').exists():results.append(session._read(out/'manifest.json'));continue
            try:
                if phase=='task':
                    with tools._ConsistentSnapshotExecutor(deadline_at=tools._call_deadline(None)) as db:
                        rows=workflow_inputs.complete(db,'SELECT '+','.join(wf.BASELINE_COLUMNS)+',frozen_at FROM vk_ai.slow_moving_baseline WHERE week_label=%s AND whse_dept=%s ORDER BY source_row_id LIMIT 10001',[session.WEEK,region])
                        observed=workflow_inputs.complete(db,'SELECT NOW(6) AS observed_at',limit=1)[0]['observed_at']
                        if not rows:
                            result={'phase':phase,'status':'no_frozen_task_rows','note':'No selected department rows, not a claim of zero physical stock.'}
                            operations._atomic(out/'manifest.json',result);results.append(result);continue
                        depts=reference['regions'][region]['dynamic_sales_departments']
                        employees=workflow_inputs.complete(db,"SELECT region,main_dept,person_name,wecom_account,position,is_delete,wecom_status FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_status='payroll' AND COALESCE(wecom_account,'')<>'' AND region=%s AND main_dept IN ("+','.join('%s' for _ in depts)+') ORDER BY wecom_account LIMIT 10001',[region,*depts])
                    operations._atomic(out/'source-baseline.json',rows)
                    if len({r['source_row_id'] for r in rows})!=len(rows) or len({str(r.get('frozen_at')) for r in rows})!=1 or any(r.get('frozen_at') is None or r.get('baseline_version')!=2 or r.get('source_table')!='vk_ods.slow_moving_goods_ods' for r in rows):
                        raise IOErrorBoundary('REGIONAL_BASELINE_IDENTITY_INVALID')
                    frozen=datetime.fromisoformat(str(rows[0]['frozen_at']))
                    if frozen.isocalendar()[:2]!=(2026,38) or frozen>datetime.fromisoformat(str(observed)):raise IOErrorBoundary('REGIONAL_BASELINE_TIME_INVALID')
                    checked=wf.freeze_plan([{**r,'id':r['source_row_id'],'goods_num':r['total_qty'],'piece_num':r['total_piece'],'is_whitelist':'n'} for r in rows],[],session.WEEK,session.WEEK)
                    if checked['status']!='ready':raise IOErrorBoundary('REGIONAL_BASELINE_FIELDS_INCOMPLETE')
                    targets=wf.task_recipients(reference['regions'],employees,[region])[region]
                    period=wf.legacy_periods(delivery.workflow.at_utc8(session.WEEK))
                    text,sheet=wf.task_draft(region,session.WEEK,period['planned_start'][:10],period['planned_end'][:10],rows)
                    path=out/(region+'_'+session.WEEK+'_Products.xlsx');gen_workbook_xlsx([sheet],path)
                    notice=session._notice('task-'+region,'原'+region+'任务角色同文合并'+str(len(targets))+'人，仅验收重定向，使用既有冻结，未重冻',text,[path])
                    result=seal(root,phase,notice,{'baseline_rows':len(rows),'original_targets':targets,'frozen_at':str(rows[0]['frozen_at']),'observed_at':str(observed),'frozen':False})
                else:
                    period=session.WEEK if phase=='weekly' else session.MONTH
                    evidence,labels=report_evidence.collect(region,period,phase,session.WEEK)
                    operations._atomic(out/'query-evidence.json',evidence);operations._atomic(out/'source-labels.json',labels)
                    adapted=workflow_inputs.legacy_report_packet(evidence['packets']['pool'],evidence['packets']['flow'],labels,region,period,evidence=evidence)
                    operations._atomic(out/'validation.json',adapted)
                    if not adapted['label_coverage']['complete']:raise IOErrorBoundary('REGIONAL_REPORT_LABEL_REVIEW_REQUIRED')
                    text=wf.report_draft(region,period,adapted['summary'],adapted['sales_rows'],monthly=phase=='monthly')
                    path=out/(region+'_'+period+'_Report.xlsx');gen_workbook_xlsx([('Detail',wf.REPORT_HEADERS,adapted['detail_rows'])],path,borders=True,landscape=True)
                    targets=wf.report_recipients(reference['regions'],region)
                    role=region+'原执行人/管理层同文合并'+str(len(targets))+'人，截至'+adapted['completeness']['observed_to']+'，非期末最终结果'
                    notice=session._notice(phase+'-'+region,role,text,[path],message_format='markdown')
                    result=seal(root,phase,notice,{'summary':adapted['summary'],'pool_groups':adapted['completeness']['pool_groups'],
                        'flow_groups':adapted['completeness']['flow_groups'],'label_coverage':adapted['label_coverage'],
                        'original_targets':targets,'observed_at':adapted['completeness']['observed_to'],'partitioned':evidence['partitioned']})
            except Exception as exc:
                result={'phase':phase,'status':'blocked','code':str(exc) if isinstance(exc,wf.WorkflowError) else getattr(exc,'code',type(exc).__name__),'detail':getattr(exc,'evidence',{})}
                operations._atomic(out/'manifest.json',result)
            results.append(result)
        summary={'region':region,'phases':[{'phase':r['phase'],'status':r['status'],'code':r.get('code'),'rows':r.get('evidence',{}).get('pool_groups',r.get('evidence',{}).get('baseline_rows'))} for r in results],'sent':False}
        operations._atomic(root/'preparation-summary.json',summary);return summary

def send(profile,region,phase):
    from .local_report import _assert_local_context
    if phase not in PHASES:raise IOErrorBoundary('REGIONAL_PHASE_NOT_ALLOWED')
    _assert_local_context();root=folder(profile,region)
    record=session._read(root/phase/'manifest.json')
    if record.get('status')!='prepared':raise IOErrorBoundary('REGIONAL_CASE_NOT_READY')
    if phase=='monthly':
        preceding=root/'weekly/send-result.json'
        if not preceding.exists() or session._read(preceding).get('status')!='provider_accepted_not_human_read':raise IOErrorBoundary('REGIONAL_WEEKLY_NOT_ACCEPTED')
    if wf.digest(record['notice'])!=record['notice_digest']:raise IOErrorBoundary('REGIONAL_NOTICE_CHANGED')
    for path,digest in record['files'].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest:raise IOErrorBoundary('REGIONAL_ATTACHMENT_CHANGED')
    result=delivery.deliver_batch(profile,'regional-'+region.lower()+'-'+phase+'-w38-v1',[record['notice']])
    operations._atomic(root/phase/'send-result.json',result);return result

def main(profile,argv):
    if len(argv)==2 and argv[0]=='--prepare' and argv[1] in REGIONS:result=prepare(profile,argv[1])
    elif len(argv)==3 and argv[0]=='--send' and argv[1] in REGIONS and argv[2] in PHASES:result=send(profile,argv[1],argv[2])
    else:print('REGIONAL_ACCEPTANCE_ACTION_REQUIRED');return 2
    print(json.dumps(result,ensure_ascii=False,default=str));return 0
