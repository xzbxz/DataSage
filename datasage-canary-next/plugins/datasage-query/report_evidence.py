"""Fixed legacy report evidence on the existing governed read-only executor.

Not a model tool or general query entry. No paging, DML, delivery or credentials.
"""
from datetime import datetime
import json,time
from . import tools,wire,legacy_workflow as wf
from .workflow_io import IOErrorBoundary

def requests(region,period,phase):
    if region not in wf.policy()['regions'] or phase not in ('weekly','monthly'):
        raise IOErrorBoundary('REPORT_SCOPE_NOT_ALLOWED')
    period_args={'baseline_week':period} if phase=='weekly' else {'calendar_month':period}
    prefix='registered_slow_pool_baseline_' if phase=='weekly' else 'registered_slow_monthly_'
    return [{'request_id':name,'domain':'inventory','mode':'metric','metric':prefix+metric,
             'dimensions':dims,'metric_filters':{'warehouse_department':region},'limit':100,**period_args}
            for name,metric,dims in (
                ('pool','groups',['product','pool_sku','warehouse_department','unit']),
                ('flow','net_outbound',['product','pool_sku','warehouse_department','unit','salesperson']),
                ('summary','summary',['unit']),('flow_total','net_outbound',['unit']))]

def collect(region,period,phase,week,*,snapshots=None):
    from .local_report import _assert_local_context
    from . import runtime_health
    _assert_local_context()
    fixed=requests(region,period,phase)
    deadline=tools._call_deadline(None)
    observed=tools._business_today()
    tools._validate_query_dispatch({'requests':fixed},observed_on=observed)
    if not runtime_health.query_readiness_status().get('ready'):
        raise IOErrorBoundary('REPORT_DATABASE_NOT_READY')
    if not tools._try_acquire_query_slot():raise IOErrorBoundary('REPORT_QUERY_CAPACITY_UNAVAILABLE')
    try:
        prepared=[tools._prepare_one(r,deadline_at=deadline,resolution_cache={},max_unique_lookups=0,
                    preflight_stats={},period_observed_on=observed) for r in fixed]
        factory=snapshots or (lambda:tools._ConsistentSnapshotExecutor(deadline_at=deadline))
        with factory() as db:
            if not getattr(db,'marker',None):raise IOErrorBoundary('REPORT_SNAPSHOT_REQUIRED')
            packets={};members={}
            for request,plan in zip(fixed,prepared):
                result=tools._run_one(request,prepared=plan,execute_query=db.execute,
                    snapshot_group_marker=db.marker,deadline_at=deadline,period_observed_on=observed)
                if result.get('status')!='success' or result.get('truncated') is not False:
                    raise IOErrorBoundary('REPORT_QUERY_FAILED_OR_TRUNCATED')
                if result.get('_snapshot_group_marker')!=db.marker:raise IOErrorBoundary('REPORT_SNAPSHOT_MISMATCH')
                members[request['request_id']]=db.marker
                # Same bounded wire transport as other local operator reports.
                def payload(_args):return json.dumps({'status':result['status'],'results':[result]},ensure_ascii=False,default=str)
                packets[request['request_id']]=json.loads(wire.bounded_json_handler('datasage_query',payload)({}))
            # Only labels for this department. No employee/customer/price fields.
            labels=[]
            for sql,args in (
                ('SELECT goods_sku_id,goods_no,attr_val,whse_dept FROM vk_ods.slow_moving_goods_ods WHERE whse_dept=%s AND goods_num>10 AND is_whitelist=%s ORDER BY id LIMIT 10001',[region,'n']),
                ('SELECT goods_sku_id,goods_no,attr_val,whse_dept FROM vk_ai.slow_moving_baseline WHERE week_label=%s AND whse_dept=%s ORDER BY source_row_id LIMIT 10001',[week,region])):
                rows,cut,_=db.execute(sql,args,10000,deadline_at=deadline)
                if cut or len(rows)>10000:raise IOErrorBoundary('REPORT_LABEL_INPUT_TRUNCATED')
                labels.extend(rows)
            evidence={'region':region,'period':period,'phase':phase,'snapshot_marker':db.marker,
                      'evidence_origin':'governed_readonly_snapshot','snapshot_members':members,'partitioned':False,'row_limit':100,'packets':packets}
            return evidence,labels
    finally:tools._release_query_slot()

def dimension(row,code,metric):
    from . import contracts,capability_contract
    _,sem=contracts.execution_contracts('inventory')
    label=capability_contract.effective_dimension_definitions(sem,metric)[code]['label']
    values=[d.get('value') for d in row.get('dimensions',[]) if d.get('label')==label]
    if len(values)!=1 or values[0] in (None,''):raise IOErrorBoundary('REPORT_DIMENSION_MISSING')
    return str(values[0])

def num(value):
    number=wf.number(value)
    if number is None or not number.is_finite():raise IOErrorBoundary('REPORT_NUMERIC_EVIDENCE_INCOMPLETE')
    return number

def equal(left,right):
    if num(left)!=num(right):raise IOErrorBoundary('REPORT_SUMMARY_DETAIL_MISMATCH')

def shared(rows,field):
    values=[num(r.get('facts',{}).get(field)) for r in rows]
    if not values or len(set(values))!=1:raise IOErrorBoundary('REPORT_POPULATION_EVIDENCE_MISSING_OR_CONFLICT')
    return values[0]

def validate(evidence):
    region,period,phase=(evidence[k] for k in ('region','period','phase'))
    fixed=requests(region,period,phase)
    if not evidence.get('snapshot_marker') or evidence.get('partitioned') is not False:
        raise IOErrorBoundary('REPORT_SNAPSHOT_REQUIRED')
    expected={r['request_id']:r for r in fixed};packets=evidence.get('packets',{})
    if evidence.get('snapshot_members')!={name:evidence['snapshot_marker'] for name in expected}:
        raise IOErrorBoundary('REPORT_SNAPSHOT_MEMBERS_MISMATCH')
    if set(packets)!=set(expected):raise IOErrorBoundary('REPORT_PACKET_MISSING')
    groups={};times=[];freeze=[]
    for name,packet in packets.items():
        results=packet.get('results',[])
        if packet.get('status')!='success' or len(results)!=1:raise IOErrorBoundary('REPORT_PACKET_FAILED')
        result=results[0];rows=result.get('rows')
        if result.get('status')!='success' or result.get('truncated') is not False or not isinstance(rows,list) or result.get('row_count')!=len(rows) or len(rows)>100:
            raise IOErrorBoundary('REPORT_PACKET_INCOMPLETE_OR_TRUNCATED')
        if result.get('request_id')!=name:raise IOErrorBoundary('REPORT_PACKET_ID_MISMATCH')
        timing=result.get('applied_time_range',{})
        if timing.get('baseline_week' if phase=='weekly' else 'monthly_month')!=period:
            raise IOErrorBoundary('REPORT_PERIOD_MISMATCH')
        try:times.append(datetime.fromisoformat(timing['read_at' if phase=='weekly' else 'monthly_read_at']))
        except Exception:raise IOErrorBoundary('REPORT_OBSERVATION_TIME_MISSING') from None
        if phase=='weekly':freeze.append(timing.get('frozen_at'))
        groups[name]=rows
    if phase=='weekly' and (not all(freeze) or len(set(freeze))!=1):raise IOErrorBoundary('REPORT_BASELINE_MISMATCH')
    pool,flow,summary,totals=(groups[k] for k in ('pool','flow','summary','flow_total'))
    if not pool or not summary or not totals:raise IOErrorBoundary('REPORT_EMPTY_POPULATION_NOT_PROVEN')
    metrics={k:v['metric'] for k,v in expected.items()}
    def dim(row,code,name):return dimension(row,code,metrics[name])
    def key(row,name):
        if dim(row,'warehouse_department',name)!=region:raise IOErrorBoundary('REPORT_REGION_MISMATCH')
        return (dim(row,'pool_sku',name),dim(row,'unit',name))
    pool_keys=[key(r,'pool') for r in pool]
    flow_keys=[(*key(r,'flow'),r['facts'].get('sales_identity_ref')) for r in flow]
    if any(k[-1] in (None,'') for k in flow_keys):raise IOErrorBoundary('REPORT_SALES_IDENTITY_MISSING')
    if len(set(pool_keys))!=len(pool_keys) or len(set(flow_keys))!=len(flow_keys):raise IOErrorBoundary('REPORT_DUPLICATE_GRAIN')
    equal(shared(pool,'population_union_groups'),len(pool))
    equal(shared(summary,'population_union_groups'),len(pool))
    if flow:equal(shared(flow,'population_display_groups'),len(flow))
    equal(shared(totals,'population_display_groups'),len(totals))
    def by_unit(rows,name):
        result={}
        for row in rows:
            unit=dim(row,'unit',name)
            if unit in result:raise IOErrorBoundary('REPORT_DUPLICATE_UNIT_SUMMARY')
            result[unit]=row['facts']
        return result
    sums=by_unit(summary,'summary');nets=by_unit(totals,'flow_total')
    if set(sums)!={k[1] for k in pool_keys}:raise IOErrorBoundary('REPORT_UNIT_COVERAGE_MISMATCH')
    opening={key(r,'pool') for r in pool if r.get('states',{}).get('pool_movement_state')!='New'}
    if set(nets)!={k[1] for k in opening}:raise IOErrorBoundary('REPORT_BASELINE_UNIT_COVERAGE_MISMATCH')
    if any(k[:2] not in opening for k in flow_keys):raise IOErrorBoundary('REPORT_FLOW_OUTSIDE_BASELINE')
    states={'New':'new','Exited':'exited','Reduced':'reduced','No Change':'unchanged','Increased':'increased','Unassessable':'unassessable'}
    for unit,total in sums.items():
        rows=[r for r in pool if key(r,'pool')[1]==unit]
        for state,field in states.items():
            equal(total.get(field+'_group_count'),sum(r.get('states',{}).get('pool_movement_state')==state for r in rows))
        if any(r.get('states',{}).get('pool_movement_state') not in states or r['states']['pool_movement_state']=='Unassessable' for r in rows):
            raise IOErrorBoundary('REPORT_POOL_STATE_UNASSESSABLE')
        for side,excluded in [('opening','New'),('closing','Exited')]:
            active=[r for r in rows if r['states']['pool_movement_state']!=excluded]
            equal(total.get(side+'_group_count'),len(active))
            for measure in ('quantity','rolls'):
                values=[num(r['facts'].get(side+'_'+measure)) for r in active]
                # SQL absent-side SUM is NULL, not a missing value for present rows.
                actual=total.get(side+'_'+measure)
                if not active and actual is None:actual=0
                equal(actual,sum(values,num(0)))
    for unit,total in nets.items():
        rows=[r for r in flow if key(r,'flow')[1]==unit]
        equal(total.get('baseline_scope_groups'),sum(k[1]==unit for k in opening))
        for field in ('metric_value','net_rolls','high_net_rolls','gross_flow_rows','return_flow_rows'):
            equal(total.get(field),sum((num(r['facts'].get(field)) for r in rows),num(0)))
        for row in rows:
            equal(row['facts'].get('unit_display_groups'),len(rows))
            equal(row['facts'].get('unit_net_quantity'),total['metric_value'])
        if total.get('outbound_unknown_rows')!=0 or total.get('returns_unknown_rows')!=0:
            raise IOErrorBoundary('REPORT_FLOW_SCOPE_UNKNOWN')
    net=sum((num(t['net_rolls']) for t in nets.values()),num(0));high=sum((num(t['high_net_rolls']) for t in nets.values()),num(0))
    equal(shared(totals,'scope_net_rolls'),net);equal(shared(totals,'scope_high_net_rolls'),high)
    if flow:
        equal(shared(flow,'scope_net_rolls'),net);equal(shared(flow,'scope_high_net_rolls'),high)
        sales={}
        for row in flow:sales.setdefault(row['facts']['sales_identity_ref'],[]).append(row)
        equal(shared(flow,'population_sales_groups'),len(sales))
        for rows in sales.values():equal(shared(rows,'sales_net_rolls'),sum((num(r['facts']['net_rolls']) for r in rows),num(0)))
    return {'population_complete':True,'quantities_complete':True,'amounts_applicable':False,
        'snapshot_marker':evidence['snapshot_marker'],'observed_from':min(times).isoformat(),'observed_to':max(times).isoformat(),
        'as_of_label':'截至本次只读快照观察时点，非全周最终结果' if phase=='weekly' else '截至本次只读快照观察时点，非整月最终结果',
        'pool_groups':len(pool),'flow_groups':len(flow),'unit_totals':nets,
        'checked':['population_counts','stable_grain_uniqueness','unit_coverage','pool_states','opening_closing_quantity_and_rolls','flow_quantity_rolls_high_rolls','sales_totals','flow_event_counts'],
        'net_rolls':net,'high_net_rolls':high}
