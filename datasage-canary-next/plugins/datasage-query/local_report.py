"""Operator-only reports. Not a registered model tool or a cron identity proof.

Trust belongs to the local OS operator and reviewed bindings. A process with the
same filesystem privileges can impersonate a report ID; no claim to prevent it.
No SQL, recipients, sending, scheduler or production fixture is exposed here.
"""
from __future__ import annotations
from datetime import date, datetime
from pathlib import Path
import json
import re
import sys

from . import contract_store, settings, tools, wire

VIEWS = {
    'monthly_pool_summary': ('registered_slow_monthly_summary', ['unit']),
    'monthly_flow_summary': ('registered_slow_monthly_net_outbound', ['unit']),
    'monthly_flow_sales': ('registered_slow_monthly_net_outbound', ['salesperson','unit']),
    'pool_summary': ('registered_slow_pool_baseline_summary', ['unit']),
    'flow_summary': ('registered_slow_pool_baseline_net_outbound', ['unit']),
    'flow_sales': ('registered_slow_pool_baseline_net_outbound', ['salesperson', 'unit']),
}
BINDINGS_FILE = 'local-report-bindings.json'


class ReportError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def load_bindings(profile: Path):
    path = profile / BINDINGS_FILE
    if not path.is_file():
        raise ReportError('REPORT_NOT_CONFIGURED')
    if path.is_symlink() or path.resolve().parent != profile.resolve():
        raise ReportError('REPORT_BINDINGS_PATH_INVALID')
    if path.stat().st_size > 65536:
        raise ReportError('REPORT_BINDINGS_INVALID')
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ReportError('REPORT_BINDINGS_INVALID')
                result[key] = value
            return result
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise ReportError('REPORT_BINDINGS_INVALID') from exc


def _select_binding(bindings, report_id=None):
    if (not isinstance(bindings, dict) or set(bindings) != {'version','default_report','reports'}
            or type(bindings['version']) is not int or bindings['version'] != 1
            or not isinstance(bindings['reports'], dict)):
        raise ReportError('REPORT_BINDINGS_INVALID')
    report_id = report_id if report_id is not None else bindings['default_report']
    if not isinstance(report_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', report_id):
        raise ReportError('REPORT_ID_INVALID')
    binding = bindings['reports'].get(report_id)
    if binding is None: raise ReportError('REPORT_ID_NOT_CONFIGURED')
    return report_id,binding


def resolve_binding(bindings, report_id=None):
    report_id,binding=_select_binding(bindings,report_id)
    required = {'department','views','limit'}
    optional = {'baseline_week','max_baseline_age_days','time_range','calendar_month'}
    if not isinstance(binding, dict) or not required <= set(binding) or set(binding)-required-optional:
        raise ReportError('REPORT_BINDING_INVALID')
    department = binding['department']
    if not isinstance(department, str) or not department.strip() or len(department)>100 or any(ord(c)<32 for c in department):
        raise ReportError('REPORT_DEPARTMENT_INVALID')
    views=binding['views']
    if not isinstance(views,list) or not views or any(not isinstance(v,str) or v not in VIEWS for v in views) or len(set(views))!=len(views):
        raise ReportError('REPORT_VIEWS_INVALID')
    weekly=any(not v.startswith('monthly_') for v in views)
    monthly=any(v.startswith('monthly_') for v in views)
    if weekly:
        week=binding.get('baseline_week')
        try:
            if not isinstance(week,str) or not re.fullmatch(r'\d{4}-W\d{2}',week):raise ValueError()
            date.fromisocalendar(int(week[:4]),int(week[6:]),1)
        except ValueError:raise ReportError('REPORT_BASELINE_INVALID')
        age=binding.get('max_baseline_age_days')
        if type(age) is not int or not 1<=age<=366:raise ReportError('REPORT_BINDING_INVALID')
    elif any(k in binding for k in ('baseline_week','max_baseline_age_days','time_range')):
        raise ReportError('REPORT_BINDING_INVALID')
    if monthly:
        month=binding.get('calendar_month')
        try:
            if not isinstance(month,str) or not re.fullmatch(r'\d{4}-\d{2}',month):raise ValueError()
            date.fromisoformat(month+'-01')
        except ValueError:raise ReportError('REPORT_WINDOW_INVALID')
    elif 'calendar_month' in binding:raise ReportError('REPORT_BINDING_INVALID')
    if type(binding['limit']) is not int or not 1<=binding['limit']<=100:raise ReportError('REPORT_BINDING_INVALID')
    window=binding.get('time_range')
    if window is not None:
        try:
            if not isinstance(window,dict) or set(window)!={'start','end'}:raise ValueError()
            for v in window.values():
                if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v):raise ValueError()
                date.fromisoformat(v)
            if window['start']>=window['end'] or not any(v.startswith('flow_') for v in views):raise ValueError()
        except ValueError:raise ReportError('REPORT_WINDOW_INVALID')
    return report_id, json.loads(json.dumps(binding))


def _assert_local_context():
    from gateway.session_context import get_session_env
    if any(get_session_env('HERMES_SESSION_'+key) for key in ('PLATFORM','SOURCE','USER_ID')):
        raise ReportError('REPORT_INTERACTIVE_CONTEXT_REJECTED')


def execute_report(bindings, report_id=None):
    _assert_local_context()
    report_id,binding=resolve_binding(bindings,report_id)
    requests=[]
    for view in binding['views']:
        metric, dimensions=VIEWS[view]
        request={'request_id':view,'domain':'inventory','mode':'metric','metric':metric,
            'dimensions':list(dimensions),
            'metric_filters':{'warehouse_department':binding['department']},'limit':binding['limit']}
        if view.startswith('monthly_'):
            request['calendar_month']=binding['calendar_month']
        else:
            request['baseline_week']=binding['baseline_week']
        if view.startswith('flow_') and binding.get('time_range') is not None:
            request['time_range']=dict(binding['time_range'])
        requests.append(request)
    # This is the existing internal readiness/execution composition, AFTER the
    # separate local report allowlist. Public registration still uses entitlements.
    handler=wire.bounded_json_handler('datasage_query',tools.runtime_guarded_datasage_query)
    payload=json.loads(handler({'requests':requests}))
    if payload.get('status') not in {'success','partial'}:
        return {'report_id':report_id,'status':'failed','trust':'local_os_operator','query':payload}
    results=payload.get('results')
    if (not isinstance(results,list) or len(results)!=len(requests)
            or {r.get('request_id') for r in results if isinstance(r,dict)}!={r['request_id'] for r in requests}):
        raise ReportError('REPORT_RESULT_EVIDENCE_INVALID')
    # A binding names an explicit cohort and an operator-approved age bound.
    # Never silently switch to a newer/older cohort to make a scheduled report work.
    for result in results:
        if result.get('status') != 'success':continue
        scope=result.get('applied_time_range') or {}
        if result['request_id'].startswith('monthly_'):
            if scope.get('monthly_month')!=binding['calendar_month']:
                raise ReportError('REPORT_BASELINE_EVIDENCE_INVALID')
            continue
        try:
            if scope['baseline_week']!=binding['baseline_week']:raise ValueError()
            frozen=datetime.fromisoformat(scope['frozen_at']);read=datetime.fromisoformat(scope['read_at'])
            if frozen.tzinfo is not None or read.tzinfo is not None or read<frozen:raise ValueError()
            if (read-frozen).total_seconds()>binding['max_baseline_age_days']*86400:
                raise ReportError('REPORT_BASELINE_STALE')
        except (KeyError,TypeError,ValueError) as exc:
            if isinstance(exc,ReportError):raise
            raise ReportError('REPORT_BASELINE_EVIDENCE_INVALID') from exc
    return {'report_id':report_id,'status':payload['status'],'trust':'local_os_operator','query':payload}


def _display(value):
    if value is None:return '未知'
    # Untrusted source labels must not become official MEDIA delivery directives.
    value=str(value).replace('\r','\\r').replace('\n','\\n')
    value=re.sub('MEDIA', 'ＭＥＤＩＡ', value, flags=re.I)
    return value.replace('[SILENT]','［SILENT］')


def render_text(report):
    payload=report['query']
    lines=['滞销范围观察（本机报表）',_display(payload.get('answer_scope_line','未取得有效查询范围'))]
    labels={'metric_value':'已记录净数量','known_subset_value':'净数量已知部分','net_rolls':'普通净出库卷数','high_net_rolls':'高折净出库卷数','high_known_net_rolls':'高折净卷已知部分','high_missing_price_rows':'高折价格缺失记录','high_missing_roll_rows':'高折卷数缺失记录','high_price_anomaly_rows':'比价字段异常记录',
        'opening_group_count':'期初业务组数','closing_group_count':'期末业务组数','new_group_count':'新增业务组','exited_group_count':'退出业务组','unassessable_group_count':'不可判定业务组',
        'known_net_rolls':'净卷数已知部分','gross_quantity':'已记录出库数量','return_quantity':'已记录退货数量',
        'opening_quantity':'期初池数量','closing_quantity':'期末池数量','opening_rolls':'期初池卷数',
        'closing_rolls':'期末池卷数','missing_value_count':'缺失及未知计数',
        'outbound_unknown_rows':'出库范围未知记录','returns_unknown_rows':'退货范围未知记录'}
    for result in payload.get('results',[]):
        lines.append(_display(result.get('business_metric_label','查询分项'))+'；状态：'+_display(result.get('data_state',result.get('status'))))
        if result.get('error'):
            lines.append('失败：'+_display(result['error'].get('code')))
        if result.get('truncated'):lines.append('仅展示部分分组，不能视为全体。')
        total_row=next((r.get('facts',{}) for r in result.get('rows',[]) if 'scope_net_rolls' in r.get('facts',{})),None)
        if total_row is not None:
            lines.append('当前筛选范围普通净出库总卷数：'+_display(total_row['scope_net_rolls'])+' 卷')
            lines.append('当前筛选范围高折净出库总卷数：'+_display(total_row['scope_high_net_rolls'])+' 卷')
            for field,label in [('scope_missing_roll_rows','总体卷数缺失记录'),('scope_high_missing_price_rows','总体高折价格缺失记录'),('scope_high_missing_roll_rows','总体高折卷数缺失记录')]:
                lines.append(label+'：'+_display(total_row.get(field))+' 条')
        shown_sales=set()
        shown_units=set()
        for row in result.get('rows',[]):
            dims=row.get('dimensions',[])
            unit=next((d['value'] for d in dims if '单位' in d['label']),row.get('unit','来源单位'))
            fact=row.get('facts',{})
            if str(result.get('request_id')).endswith('flow_sales') and unit not in shown_units and 'unit_net_quantity' in fact:
                lines.append('截断前该单位范围净数量：'+_display(fact['unit_net_quantity'])+' '+_display(unit))
                lines.append('截断前该单位普通净卷数：'+_display(fact.get('unit_net_rolls'))+' 卷')
                lines.append('截断前该单位高折净卷数：'+_display(fact.get('unit_high_net_rolls'))+' 卷')
                shown_units.add(unit)
            if str(result.get('request_id')).endswith('flow_sales') and 'sales_net_rolls' in fact:
                identity=fact.get('sales_identity_ref')
                if identity not in shown_sales:
                    shown_sales.add(identity)
                    lines.append('；'.join(_display(d['label'])+'='+_display(d['value']) for d in dims if '单位' not in d['label']))
                    lines.append('销售身份引用：'+_display(identity))
                    lines.append('该销售在当前筛选范围的普通净卷：'+_display(fact['sales_net_rolls'])+' 卷')
                    lines.append('该销售在当前筛选范围的高折净卷：'+_display(fact['sales_high_net_rolls'])+' 卷')
                    if fact['sales_high_net_rolls'] is None:lines.append('该销售高折净卷已知部分：'+_display(fact.get('sales_high_known_net_rolls'))+' 卷')
                continue
            lines.append('；'.join(_display(d['label'])+'='+_display(d['value']) for d in dims))
            for field,label in labels.items():
                if field not in fact:continue
                complete_field={'known_subset_value':'metric_value','known_net_rolls':'net_rolls','high_known_net_rolls':'high_net_rolls'}.get(field)
                if complete_field and fact.get(complete_field) is not None:continue
                if field=='metric_value' and str(result.get('request_id')).endswith('pool_summary'):label='比较业务组数';suffix='组'
                else:suffix='卷' if 'rolls' in field else '组' if field.endswith('_group_count') else '条' if field.endswith(('_count','_rows')) else _display(unit)
                lines.append(label+'：'+_display(row['facts'][field])+' '+suffix)
            if row.get('states'):lines.append('证据状态：'+_display(json.dumps(row['states'],ensure_ascii=False)))
    for item in payload.get('disclosures',[]):lines.append(_display(item['text']))
    if payload.get('error'):lines.append('失败：'+_display(payload['error'].get('code')))
    lines.append('两个净卷数是已确认战果，与库存变化分别解释，不表示原冻结批次消化率；期初和期末来源及时点以各分项为准。')
    return '\n'.join(lines)+'\n'


def configure_runtime(profile):
    from hermes_cli.env_loader import load_hermes_dotenv
    import yaml
    load_hermes_dotenv(hermes_home=profile)
    config=yaml.safe_load((profile/'config.yaml').read_text(encoding='utf-8'))
    reader=config['plugins']['entries']['datasage-query'].get('settings',{})
    if not isinstance(reader,dict):raise ReportError('REPORT_RUNTIME_CONFIG_INVALID')
    settings.bind_config_reader(lambda key,default=None:reader.get(key,default))
    contract_store.pin_contract_snapshot()


def save_artifacts(profile, report, text):
    """Private run files; no recipients, queues or cron store. Never in Git."""
    import os
    import uuid
    root=profile/'report_runs'/'slow'
    for candidate in (profile/'report_runs',root):
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(profile.resolve()):
            raise ReportError('REPORT_OUTPUT_PATH_INVALID')
    root.mkdir(parents=True,exist_ok=True)
    run=root/uuid.uuid4().hex
    run.mkdir()
    document={**report,'runtime':{'profile_home':str(profile.resolve()),'python_executable':sys.executable,
        'python_prefix':sys.prefix,'pid':os.getpid(),'identity_basis':'local OS operator; not WeCom authorization'}}
    for name,content in [('report.json',json.dumps(document,ensure_ascii=False,indent=2)),('report.txt',text)]:
        temporary=run/(name+'.tmp')
        temporary.write_text(content,encoding='utf-8')
        temporary.replace(run/name)
    return run


def main(profile, argv=None):
    import argparse
    from hermes_constants import get_hermes_home
    parser=argparse.ArgumentParser(description='Local trusted slow report; no scheduling or sending.')
    parser.add_argument('--report-id')
    parser.add_argument('--legacy-preview', choices=['slow_task','slow_report','idk','sales_price','purchase_price','fabric'])
    parser.add_argument('--accept-snapshot', help='Explicitly accept a reviewed local observation digest; never writes the business database.')
    args=parser.parse_args(argv)
    try:
        if Path(get_hermes_home()).resolve()!=profile.resolve():raise ReportError('REPORT_PROFILE_MISMATCH')
        _assert_local_context()
        if args.legacy_preview:
            if args.report_id or args.accept_snapshot:raise ReportError('WORKFLOW_PREVIEW_ARGUMENT_CONFLICT')
            from .legacy_workflow import preview_from_file,WorkflowError
            try:
                path=preview_from_file(profile,args.legacy_preview)
                print('LOCAL_PREVIEW_ONLY '+str(path));return 0
            except WorkflowError as exc:
                print(str(exc),file=sys.stderr);return 2
        bindings=load_bindings(profile)
        selected,operation=_select_binding(bindings,args.report_id)
        if isinstance(operation,dict) and 'kind' in operation:
            from . import operations
            try:
                operations.validate_binding(operation)
                if args.accept_snapshot:
                    expected=operations.scope_fingerprint(operation)
                    operations.accept_snapshot(profile,selected,args.accept_snapshot,expected_scope=expected)
                    print('SNAPSHOT_ACCEPTED_LOCALLY; delivery remains not_requested')
                    return 0
                configure_runtime(profile)
                document=operations.execute(profile,selected,operation)
                artifact=operations.save_observation(profile,selected,document)
                html_path=artifact.with_suffix('.html')
                if html_path.is_symlink():raise operations.OperationError('REPORT_PATH_INVALID')
                html_path.write_text(operations.render(document),encoding='utf-8')
                if document['status']!='success':
                    print('REPORT_QUERY_INCOMPLETE',file=sys.stderr);return 3
                print('业务产物已生成；尚未投递。')
                print('本地观察标识：'+artifact.stem)
                print('状态计数：'+json.dumps(document.get('event_counts',{}),ensure_ascii=False))
                return 0
            except operations.OperationError as exc:
                print(str(exc),file=sys.stderr);return 2
        if args.accept_snapshot:raise ReportError('REPORT_SNAPSHOT_UNSUPPORTED')
        resolve_binding(bindings,args.report_id) # Fail before loading credentials/runtime.
        configure_runtime(profile)
        report=execute_report(bindings,args.report_id)
        text=render_text(report)
        save_artifacts(profile,report,text)
        if report['status']!='success':
            # Partial observations remain inspectable in API results; cron must not
            # publish a partial computation as an ordinary successful report.
            print('REPORT_QUERY_INCOMPLETE',file=sys.stderr);return 3
        print(text,end='')
        return 0
    except ReportError as exc:
        print(exc.code,file=sys.stderr);return 2
    except Exception:
        print('REPORT_RUNTIME_FAILED',file=sys.stderr);return 3
