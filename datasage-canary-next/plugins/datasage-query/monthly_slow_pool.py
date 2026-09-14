"""Independent monthly pool: physical snapshots and one current whitelist.
No freezing, source writes, lifecycle inference or independent flow formula.
"""
from datetime import date,datetime
from .capability_contract import MONTHLY_SLOW_FORBIDDEN_PARAMETERS
from .analytical_queries import (AnalysisQueryError,_dataset,_approved,_quote_table,
    _add_months,_value_filter,_entity_bindings,_bound_value,
    _pool_comparison_query,_frozen_pool_net_outbound_query)

KEYS=('goods_id','goods_sku_id','whse_dept','unit')
def unit(column):
    return f"CONVERT((CASE WHEN LOWER(TRIM({column}))='m' THEN 'm' ELSE NULLIF(TRIM({column}),'') END) USING utf8mb4) COLLATE utf8mb4_bin"

def key_valid(alias=''):
    p=alias+'.' if alias else ''
    return f"{p}goods_id IS NOT NULL AND {p}goods_sku_id IS NOT NULL AND {p}whse_dept IS NOT NULL AND {p}unit IS NOT NULL AND {p}unit IN ('m','y','kg','Pcs')"

def scope_filter(request,params,alias):
    clauses=[];bindings=_entity_bindings(request)
    for code,value in (request.get('metric_filters') or {}).items():
        if code=='salesperson':continue
        field={'product':'goods_id','pool_sku':'goods_sku_id','warehouse_department':'whse_dept','unit':'unit'}.get(code)
        if field is None:raise AnalysisQueryError('UNSUPPORTED_DIMENSION','月报仅接受已登记业务键和战果销售筛选。')
        _,value=_bound_value(bindings,code,value)
        clause=_value_filter(alias,field,value,params)
        unknown=f'{alias}.{field} IS NULL' if field!='unit' else f"{alias}.unit IS NULL OR {alias}.unit NOT IN ('m','y','kg','Pcs')"
        clauses.append('('+clause+' OR '+unknown+')')
    return ' AND '.join(clauses) or '1=1'

def monthly_cohort(request,metric,datasets,observed_on):
    invalid = next((k for k in MONTHLY_SLOW_FORBIDDEN_PARAMETERS if request.get(k) is not None), None)
    if invalid is not None:
        raise AnalysisQueryError('INVALID_PLAN','独立月报使用完整日历月和月初池，不接受周基线、通用比较或排序。',
            path=invalid, hint='该月报不接受 '+invalid+'；删除该参数后仍须通过其他独立校验。')
    window=request.get('time_range')
    try:
        start=date.fromisoformat(window['start']) if window else date(observed_on.year,observed_on.month,1)
        end=date.fromisoformat(window['end']) if window else _add_months(start,1)
        if start.day!=1 or end!=_add_months(start,1):raise ValueError()
    except (ValueError,KeyError,TypeError):raise AnalysisQueryError('INVALID_PLAN','月报需要一个完整日历月；当月实际截止由读取时点确定。')
    month=start.strftime('%Y-%m');previous=_add_months(start,-1).strftime('%Y-%m')
    physical=metric['monthly_snapshot_table'];ods=metric['monthly_ods_table']
    for table,fields in [(physical,['id','month_date','goods_id','goods_sku_id','goods_name','unit','whse_dept','goods_num','piece_num','whse_org','whse_type','is_handing_sales','is_discountable']),
                         (ods,['id','goods_id','goods_sku_id','goods_name','source_unit','whse_dept','goods_num','piece_num','is_whitelist','slow_label'])]:
        ds=_dataset(table,datasets)
        for field in fields:_approved(field,ds)
    departments=metric.get('monthly_departments')
    if not isinstance(departments,list) or not departments or any(not isinstance(x,str) for x in departments):
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE','月报旧部门范围未登记。')
    requested=(request.get('metric_filters') or {}).get('warehouse_department')
    if requested is not None and any(x not in departments for x in (requested if isinstance(requested,list) else [requested])):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','所选部门不在已确认月报范围。')
    policy=metric.get('monthly_scope_policy') or {}
    if set(policy)!={'excluded_warehouse_type','excluded_organizations','minimum_quantity_exclusive'}:
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE','月报来源筛选定义不完整。')
    threshold=policy['minimum_quantity_exclusive'];organizations=policy['excluded_organizations']
    if type(threshold) not in (int,float) or not isinstance(organizations,list) or not organizations:
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE','月报来源筛选定义无效。')
    params=[month,previous,start.isoformat(),end.isoformat()]
    ctes=['clock_seed AS (SELECT %s AS monthly_month,%s AS monthly_opening_month,%s AS monthly_window_start,%s AS monthly_requested_end,NOW(6) AS monthly_read_at,UTC_TIMESTAMP(6) AS monthly_read_utc_at)',
      "clock AS (SELECT c.*,CASE WHEN monthly_requested_end<monthly_read_at THEN monthly_requested_end ELSE monthly_read_at END AS monthly_window_end,CASE WHEN DATE_FORMAT(monthly_read_at,'%%Y-%%m')=monthly_month THEN 1 ELSE 0 END AS monthly_is_current FROM clock_seed c)"]
    marks=','.join('%s' for _ in departments)
    # source_unit is the inventory unit, never the promotion pricing unit.
    ctes.append(f"ods_base AS (SELECT id AS source_id,goods_id,goods_sku_id,goods_name,CONVERT(NULLIF(whse_dept,'') USING utf8mb4) COLLATE utf8mb4_bin AS whse_dept,{unit('source_unit')} AS unit,goods_num AS qty,piece_num AS rolls,is_whitelist,slow_label FROM {_quote_table(ods)} WHERE whse_dept IN ({marks}) OR whse_dept IS NULL)")
    params.extend(departments)
    wf=scope_filter(request,params,'w')
    ctes.append('white_source AS (SELECT w.* FROM ods_base w WHERE '+wf+')')
    ctes.append("white_keys AS (SELECT DISTINCT goods_id,goods_sku_id,whse_dept,unit FROM white_source WHERE is_whitelist='y' AND "+key_valid()+')')
    ctes.append("white_meta AS (SELECT COUNT(*) AS monthly_whitelist_rows FROM white_keys)")
    ctes.append("white_quality AS (SELECT COALESCE(SUM(CASE WHEN is_whitelist IS NULL OR is_whitelist NOT IN ('y','n') OR (is_whitelist='y' AND NOT("+key_valid()+")) THEN 1 ELSE 0 END),0) AS monthly_whitelist_unknown_rows FROM white_source)")
    # No status, sale_bill deletion, or other new filter is added to the legacy monthly source.
    eligible="whse_type<>%s AND whse_org NOT IN ("+','.join('%s' for _ in organizations)+") AND (is_handing_sales='y' OR is_discountable='y') AND whse_dept IN ("+marks+")"
    ctes.append(f"physical_base AS (SELECT id AS source_id,month_date,goods_id,goods_sku_id,goods_name,CONVERT(NULLIF(whse_dept,'') USING utf8mb4) COLLATE utf8mb4_bin AS whse_dept,{unit('unit')} AS unit,goods_num AS qty,piece_num AS rolls,CASE WHEN {eligible} THEN 1 WHEN NOT({eligible}) THEN 0 ELSE -1 END AS source_eligible FROM {_quote_table(physical)} WHERE month_date IN ((SELECT monthly_opening_month FROM clock),(SELECT monthly_month FROM clock)))")
    params.extend(([policy['excluded_warehouse_type']]+organizations+departments)*2)
    ctes.append("physical_groups AS (SELECT month_date,MIN(source_id) AS source_id,goods_id,goods_sku_id,whse_dept,unit,MAX(goods_name) AS goods_name,SUM(qty) AS qty,SUM(rolls) AS rolls,COUNT(*) AS source_rows,SUM(CASE WHEN qty IS NULL THEN 1 ELSE 0 END) AS source_missing_qty,SUM(CASE WHEN rolls IS NULL THEN 1 ELSE 0 END) AS source_missing_rolls,SUM(CASE WHEN source_eligible=-1 THEN 1 ELSE 0 END) AS uncertain_source FROM physical_base WHERE source_eligible<>0 GROUP BY month_date,goods_id,goods_sku_id,whse_dept,unit)")
    match=' AND '.join('w.'+k+'=r.'+k for k in KEYS)
    ctes.append("physical_pool AS (SELECT r.*,CASE WHEN source_missing_qty>0 OR uncertain_source>0 THEN -1 WHEN qty>%s THEN 1 ELSE 0 END AS membership,'monthly_candidate' AS slow_label,0 AS unknown_whitelist FROM physical_groups r WHERE NOT EXISTS(SELECT 1 FROM white_keys w WHERE "+match+'))')
    params.append(threshold)
    ctes.append("opening_stage AS (SELECT * FROM physical_pool WHERE month_date=(SELECT monthly_opening_month FROM clock) AND membership<>0)")
    ctes.append("closing_stage AS (SELECT source_id,goods_id,goods_sku_id,goods_name,whse_dept,unit,qty,rolls,source_rows,source_missing_qty,source_missing_rolls,membership,slow_label,unknown_whitelist FROM physical_pool WHERE month_date=(SELECT monthly_month FROM clock) AND (SELECT monthly_is_current FROM clock)=0 AND membership<>0 UNION ALL SELECT r.source_id,r.goods_id,r.goods_sku_id,r.goods_name,r.whse_dept,r.unit,r.qty,r.rolls,1,CASE WHEN r.qty IS NULL THEN 1 ELSE 0 END,CASE WHEN r.rolls IS NULL THEN 1 ELSE 0 END,CASE WHEN r.qty>%s AND r.is_whitelist='n' THEN 1 ELSE -1 END,r.slow_label,CASE WHEN r.is_whitelist IS NULL OR r.is_whitelist NOT IN ('n','y') THEN 1 ELSE 0 END FROM ods_base r WHERE (SELECT monthly_is_current FROM clock)=1 AND (r.qty>%s OR r.qty IS NULL) AND (r.is_whitelist='n' OR r.is_whitelist IS NULL OR r.is_whitelist NOT IN ('n','y')) AND NOT EXISTS(SELECT 1 FROM white_keys w WHERE "+match+'))')
    params.extend([threshold,threshold])
    for name,source in [('b_raw','opening_stage'),('c_raw','closing_stage')]:
        clauses=scope_filter(request,params,'r')
        ctes.append(name+' AS (SELECT r.* FROM '+source+' r WHERE '+clauses+')')
    ctes.append("meta AS (SELECT (SELECT COUNT(*) FROM physical_base WHERE month_date=(SELECT monthly_opening_month FROM clock)) AS monthly_opening_snapshot_rows,CASE WHEN (SELECT monthly_is_current FROM clock)=1 THEN 1 ELSE CASE WHEN EXISTS(SELECT 1 FROM physical_base WHERE month_date=(SELECT monthly_month FROM clock)) THEN 1 ELSE 0 END END AS monthly_closing_available,(SELECT COUNT(*) FROM b_raw WHERE membership=-1 OR NOT("+key_valid()+")) AS monthly_opening_uncertain_groups,w.*,q.* FROM white_meta w CROSS JOIN white_quality q)")
    ctes.append('base_keys AS (SELECT DISTINCT goods_id,goods_sku_id,whse_dept,unit FROM b_raw WHERE membership=1 AND '+key_valid()+')')
    return {'ctes':ctes,'params':params,'source_datasets':[physical,ods],
        'flow_start':'(SELECT monthly_window_start FROM clock)','flow_end':'(SELECT monthly_window_end FROM clock)',
        'complete_condition':'monthly_opening_uncertain_groups=0 AND monthly_whitelist_unknown_rows=0'}

def build_monthly_query(request,metric,datasets,semantics,limit,*,observed_on):
    cohort=monthly_cohort(request,metric,datasets,observed_on)
    if metric.get('monthly_view')=='flow':
        return _frozen_pool_net_outbound_query(request,metric,datasets,semantics,limit,observed_on=observed_on,cohort=cohort)
    chosen=request.get('dimensions') or []
    mode=metric['baseline_view']
    expected={'product','pool_sku','warehouse_department','unit'} if mode=='groups' else {'unit','warehouse_department'}
    if chosen and (mode=='groups' and set(chosen)!=expected or mode=='summary' and (not set(chosen)<=expected or 'unit' not in chosen)):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','月池明细保持四键；汇总按单位，可附加仓库部门。')
    if request.get('movement_state') is not None:
        raise AnalysisQueryError('INVALID_PLAN','月池首批不接受变化状态筛选。')
    return _pool_comparison_query(request,metric,cohort['ctes'],cohort['params'],cohort['source_datasets'],limit,monthly=True)

def validate_monthly_rows(rows,*,flow=False):
    if not rows:raise AnalysisQueryError('MONTHLY_EVIDENCE_MISSING','月报缺少读取与来源元信息。')
    keys=('monthly_month','monthly_opening_month','monthly_window_start','monthly_requested_end','monthly_read_at','monthly_read_utc_at','monthly_window_end','monthly_is_current','monthly_opening_snapshot_rows','monthly_closing_available','monthly_whitelist_rows','monthly_whitelist_unknown_rows','monthly_opening_uncertain_groups')
    first=rows[0]
    if any(any(r.get(k)!=first.get(k) for k in keys) for r in rows):raise AnalysisQueryError('MONTHLY_EVIDENCE_INVALID','月报来源元信息不一致。')
    try:
        start=datetime.fromisoformat(str(first['monthly_window_start']));read=datetime.fromisoformat(str(first['monthly_read_at']))
        end=datetime.fromisoformat(str(first['monthly_requested_end']));actual_end=datetime.fromisoformat(str(first['monthly_window_end']))
        utc=datetime.fromisoformat(str(first['monthly_read_utc_at']))
        if any(x.tzinfo is not None for x in (start,read,end,actual_end,utc)):raise ValueError()
        if (start.day!=1 or start.time()!=datetime.min.time() or end.date()!=_add_months(start.date(),1)
                or end.time()!=datetime.min.time() or actual_end!=min(end,read)
                or first['monthly_month']!=start.strftime('%Y-%m')
                or first['monthly_opening_month']!=_add_months(start.date(),-1).strftime('%Y-%m')
                or int(first['monthly_is_current'])!=int(start<=read<end)):raise ValueError()
        for key in keys[8:]:
            if int(first[key])<0:raise ValueError()
        if start>read:raise AnalysisQueryError('MONTHLY_PERIOD_NOT_OBSERVED','未来月份尚未开始。')
        if flow and int(first['monthly_opening_snapshot_rows'])==0:raise AnalysisQueryError('MONTHLY_OPENING_SNAPSHOT_MISSING','缺少上月物理快照，无法定义月初池战果。')
    except AnalysisQueryError:raise
    except (ValueError,TypeError,KeyError):raise AnalysisQueryError('MONTHLY_EVIDENCE_INVALID','月报时点或覆盖证据无效。')
    for row in rows:
        row.pop('opening_unknown_class_rows',None);row.pop('closing_unknown_class_rows',None)
    return {'source':'monthly_slow_pool_observation',**{k:first.get(k) for k in keys},
        'closing_basis':'current_ods' if int(first['monthly_is_current']) else 'target_month_physical_snapshot',
        'whitelist_basis':'same_current_inventory_unit_key_set'}
