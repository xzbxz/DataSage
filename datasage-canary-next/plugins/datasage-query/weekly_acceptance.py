"""Operator-only HCM / 2026-W38 read-to-local acceptance; never a send path."""
from pathlib import Path
from datetime import datetime
import json,uuid
from . import report_evidence,workflow_inputs,legacy_workflow as wf,operations
from .workflow_io import IOErrorBoundary,private_root,run_lock

REGION='HCM'
WEEK='2026-W38'

def render_local(evidence,labels,out):
    if (evidence.get('region'),evidence.get('period'),evidence.get('phase'))!=(REGION,WEEK,'weekly'):
        raise IOErrorBoundary('ACCEPTANCE_SCOPE_NOT_ALLOWED')
    packets=evidence['packets']
    adapted=workflow_inputs.legacy_report_packet(packets['pool'],packets['flow'],labels,REGION,WEEK,evidence=evidence)
    observed=adapted['completeness']['observed_to']
    if datetime.fromisoformat(observed).isocalendar()[:2]!=(2026,38):
        raise IOErrorBoundary('ACCEPTANCE_CURRENT_OBSERVATION_OUTSIDE_WEEK')
    from .legacy_xlsx import gen_workbook_xlsx
    status='local_review_ready' if adapted['label_coverage']['complete'] else 'label_review_required'
    prefix='【本地业务验收，未发送】HCM / 2026-W38\n截至本次只读快照观察时点：'+observed+'；非全周最终结果。\n'
    if evidence.get('evidence_origin')=='synthetic':prefix='【合成数据，仅验证代码】\n'+prefix
    prefix+='规格数按登记SKU/部门/单位组计数，数量按来源单位分别核验。\n'
    if not adapted['label_coverage']['complete']:prefix+='标签存在缺失或歧义，保持Unknown；数值已对账，但不作为可发送完整报表。\n'
    text=prefix+wf.report_draft(REGION,WEEK,adapted['summary'],adapted['sales_rows'],weekly_start=adapted.get('completeness',{}).get('frozen_at'),detail_semantics=adapted.get('detail_semantics'))
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    operations._atomic(out/'evidence.json',evidence)
    operations._atomic(out/'validation.json',adapted)
    (out/'message.txt').write_text(text,encoding='utf-8')
    workbook=out/('HCM_2026-W38_'+('REVIEW_ONLY' if status!='local_review_ready' else 'local')+'.xlsx')
    gen_workbook_xlsx([('Detail',wf.REPORT_HEADERS,wf.legacy_detail_rows_for_xlsx(adapted['detail_rows'])),wf.REPORT_NOTES_SHEET],workbook,borders=True,landscape=True,legacy_layout=True)
    result={'status':status,'region':REGION,'baseline_week':WEEK,'observed_at':observed,
        'full_week_final':False,'sent':False,'frozen':False,'price_accepted':False,
        'monthly_executed':False,'files':[str(workbook),str(out/'message.txt'),str(out/'validation.json')],
        'label_coverage':adapted['label_coverage']}
    operations._atomic(out/'result.json',result)
    return result

def run_local(profile,*,read_enabled=False):
    if read_enabled is not True:raise IOErrorBoundary('ACCEPTANCE_READ_NOT_ENABLED')
    from .contract_store import profile_root
    from .local_report import _assert_local_context,configure_runtime
    profile=Path(profile)
    if profile.resolve()!=profile_root().resolve():raise IOErrorBoundary('ACCEPTANCE_PROFILE_MISMATCH')
    _assert_local_context();configure_runtime(profile)
    with run_lock(profile,'weekly-acceptance-hcm-2026-w38'):
        try:
            evidence,labels=report_evidence.collect(REGION,WEEK,'weekly',WEEK)
            return render_local(evidence,labels,private_root(profile)/('hcm-weekly-acceptance-'+uuid.uuid4().hex))
        except Exception as exc:
            # Failure evidence only, never a successful-looking report workbook.
            operations._atomic(private_root(profile)/('hcm-weekly-rejected-'+uuid.uuid4().hex+'.json'),
                {'status':'rejected','region':REGION,'week':WEEK,'code':str(exc) if isinstance(exc,wf.WorkflowError) else type(exc).__name__,'sent':False})
            raise

def main(profile,argv):
    if argv!=['--read-hcm-2026-w38']:
        print('ACCEPTANCE_READ_NOT_ENABLED: explicit --read-hcm-2026-w38 required; local output only')
        return 2
    try:
        print(json.dumps(run_local(profile,read_enabled=True),ensure_ascii=False));return 0
    except Exception as exc:
        print(str(exc) if isinstance(exc,wf.WorkflowError) else type(exc).__name__);return 2
