"""Source-label summaries, independently aggregated per ADS; no lifecycle inference."""
from datetime import date
from .analytical_queries import (AnalysisQueryError, _dataset, _approved, _quote_table,
    _quote_column, _time_window, _value_filter, _bound_value, _entity_bindings, _filter_clause)
from .capability_contract import effective_dimension_definitions

MEASURES = ('rolls','tagged_rolls','ddp_rmb','tagged_ddp_rmb','sales_rmb','tagged_sales_rmb','tagged_ddp_gap_rmb')
QUALITY = ('unknown_tags','missing_rolls','missing_ddp','missing_sales','negative_roll_rows',
           'scope_unknown','unknown_channels','multiple_channels','multiple_roots','formation_pending',
           'missing_formation','cross_year_roots','low_formation_confidence','in_transit_rows',
           'current_status_unknown','source_amount_exceptions','no_source_rows','unknown_candidates','unknown_cohort_labels')
FACT_FIELDS = ({'fabric_'+x for x in MEASURES} | {'fabric_known_'+x for x in MEASURES}
               | {'fabric_unresolved_'+x+'_rows' for x in MEASURES}
               | {'fabric_'+x for x in QUALITY}
               | {'fabric_scope_rows','fabric_group_rows','fabric_identity_errors','fabric_tag_rate',
                  'fabric_tag_contribution','fabric_scope_tagged_rolls','fabric_population_groups',
                  'fabric_read_at','fabric_read_utc_at','fabric_etl_min','fabric_etl_max',
                  'fabric_etl_time_count','fabric_missing_etl','fabric_business_time_min','fabric_business_time_max'})


def build_fabric_query(request, metric, datasets, semantics, limit, *, observed_on=None):
    side=metric['fabric_side'];delivery=side=='delivery';table=metric['table'];pk=metric['source_key']
    ds=_dataset(table,datasets);fields=metric['source_fields']
    for field in fields:_approved(field,ds)
    dims=effective_dimension_definitions(semantics,metric)
    chosen=request.get('dimensions') or [];filters=request.get('metric_filters') or {}
    if any(k not in metric['allowed_dimensions'] for k in [*chosen,*filters]):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','该源摘要不支持此部门归属或维度。')
    if request.get('comparison') is not None or request.get('order_by') is not None:
        raise AnalysisQueryError('INVALID_PLAN','源摘要按稳定分组键展示；不将多种数值混成排名或通用比较。')
    bucket=request.get('time_bucket')
    if not delivery and (request.get('time_range') is not None or bucket is not None):
        raise AnalysisQueryError('INVALID_PLAN','源库存仅提供本次读取快照，不提供历史期间或历史库存回填。')
    if bucket not in {None,'month'}:raise AnalysisQueryError('INVALID_PLAN','源出库仅支持按月时间分组。')
    params=[];ctes=['clock AS (SELECT NOW(6) AS read_at,UTC_TIMESTAMP(6) AS read_utc_at)',
        'raw AS (SELECT '+','.join(_quote_column(f) for f in fields)+' FROM '+_quote_table(table)+')']
    status_join='';status='NULL';status_ok='1=1'
    if not delivery:
        base=_dataset(metric['status_table'],datasets)
        for f in ['id','status']:_approved(f,base)
        conditions=[_filter_clause('b',f['column'],f,params) for f in base.get('required_filters',[])]
        conditions.append('EXISTS(SELECT 1 FROM raw a WHERE a.inventory_id=b.id)')
        ctes.append('stock_state AS (SELECT b.id,COUNT(*) AS n,MAX(b.status) AS status FROM '+_quote_table(metric['status_table'])+' b WHERE '+' AND '.join(conditions)+' GROUP BY b.id)')
        status_join=' LEFT JOIN stock_state b ON b.id=a.inventory_id';status='b.status';status_ok='b.n=1 AND b.status IN (1,2,3)'
    flag='is_low_price' if delivery else 'is_stagnant';reference='delivery_time' if delivery else 'etl_time'
    cross='(a.root_candidate_cnt>1 AND YEAR(a.first_in_whse_time)<>YEAR(a.first_in_whse_time_max))'
    formation=f"""CASE WHEN a.cohort_basis_time IS NULL THEN '待核验·缺形成时间'
      WHEN a.cohort_confidence='低' THEN '待核验·低可信'
      WHEN a.cohort_confidence IS NULL OR a.cohort_confidence NOT IN ('高','中') THEN '待核验·未知可信度'
      WHEN {cross} THEN '待核验·跨年多根'
      WHEN a.root_candidate_cnt>1 AND (a.first_in_whse_time IS NULL OR a.first_in_whse_time_max IS NULL) THEN '待核验·根时间缺失'
      WHEN a.{reference} IS NULL THEN '待核验·参照时间缺失'
      WHEN a.cohort_basis_time>a.{reference} THEN '待核验·形成晚于参照'
      WHEN YEAR(a.cohort_basis_time)=YEAR(a.{reference}) THEN '源依据本期形成' ELSE '源依据历史遗留' END"""
    normalized={'source_channel':"CASE WHEN a.source_channel IN ('中国订单','销售端提报','测算专员备货','无来源') THEN a.source_channel ELSE '未识别渠道' END"}
    for f in ['source_channel_set','source_confidence','trace_confidence','source_ambiguity_flag','inventory_cohort','cohort_basis_type']:
        normalized[f]=f"COALESCE(NULLIF(TRIM(a.{f}),''),'未记录')"
    select=[f'{normalized.get(f,"a."+f)} AS {_quote_column(f)}' for f in fields]
    for f in ['source_channel_cnt','root_candidate_cnt']:
        select.append(f"CASE WHEN a.{f} IS NULL THEN '未记录' WHEN a.{f}<0 THEN '无效候选数' ELSE CAST(a.{f} AS CHAR) END AS {f}_label")
    select += [f'{formation} AS formation_assessment',f"CASE WHEN {cross} THEN 1 ELSE 0 END AS cross_year_roots",
               f'CASE WHEN {status_ok} THEN 0 ELSE 1 END AS current_status_unknown',f'{status} AS current_status']
    if delivery:
        select.append("CASE WHEN NULLIF(TRIM(a.current_customer_dept),'') IS NULL THEN '未知部门' WHEN a.current_customer_dept LIKE %s THEN '含HT' ELSE '非HT' END AS fabric_ht_class")
        params.append('%HT%')
    ctes.append('prepared AS (SELECT '+','.join(select)+' FROM raw a'+status_join+')')
    where=[];unknown=[];applied={'source':'fabric_source_observation','basis':'recorded_delivery_history' if delivery else 'source_inventory_snapshot'}
    if delivery:
        source_history_start=metric.get('source_history_start')
        try:
            source_history_start=date.fromisoformat(str(source_history_start)).isoformat()
        except (TypeError,ValueError) as exc:
            raise AnalysisQueryError('CONTRACT_UNAVAILABLE','货源出库历史起点合同无效。') from exc
        unknown.append("p.is_inner_cus IS NULL OR p.is_inner_cus<>'n' OR p.is_ccbs_cus IS NULL OR p.is_ccbs_cus<>'n' OR p.delivery_time IS NULL OR p.delivery_time<%s OR (COALESCE(p.current_customer_dept,'') NOT LIKE %s AND (p.bill_type IS NULL OR p.bill_type<>'bulk'))")
        # This expression occurs in SELECT before the WHERE parameters below.
        params.extend([source_history_start,'%HT%'])
        applied['source_history_start']=source_history_start
    else:
        unknown.append("p.whse_org IS NULL OR p.whse_org IN ('五点中国公司','缅鑫国际贸易有限公司') OR p.whse_type IS NULL OR p.whse_type='CCBS仓'")
    if delivery and request.get('time_range'):
        start,end,_=_time_window(request,'source_recorded_history',observed_on)
        where.append('(p.delivery_time IS NULL OR (p.delivery_time>=%s AND p.delivery_time<%s))');params.extend([start,end])
        unknown.append('p.delivery_time IS NULL');applied.update(window_start=start,window_end=end)
    scope=request.get('inventory_scope','total')
    if not delivery:
        if scope not in {'total','on_hand'}:raise AnalysisQueryError('INVALID_PLAN','源库存仅支持完整源范围或可确认在仓范围。')
        applied['inventory_scope']=scope
        if scope=='on_hand':
            where.append('(p.current_status IN (1,2) OR p.current_status_unknown=1)');unknown.append('p.current_status_unknown=1')
    bindings=_entity_bindings(request)
    for code,value in filters.items():
        definition=dims[code];binding,value=_bound_value(bindings,code,value)
        column=definition['identity_filter']['column'] if binding is not None else definition['filter_column']
        if code=='fabric_sku':
            values=value if isinstance(value,list) else [value]
            if any(isinstance(v,bool) or not str(v).isascii() or not str(v).isdecimal() for v in values):raise AnalysisQueryError('INVALID_PLAN','源规格必须是明确整数标识。')
        if code in {'fabric_candidate_count','fabric_root_count'}:
            values=value if isinstance(value,list) else [value]
            if any(isinstance(v,bool) or not (str(v).isascii() and str(v).isdecimal() or v in ('未记录','无效候选数')) for v in values):raise AnalysisQueryError('INVALID_PLAN','候选数只接受明确整数或缺失类别。')
            value=[str(v) for v in values] if isinstance(value,list) else str(value)
        where.append(_value_filter('p',column,value,params))
    period=",DATE_FORMAT(p.delivery_time,'%%Y-%%m') AS period" if bucket else ''
    ctes.append('scoped AS (SELECT p.*,CASE WHEN '+(' OR '.join(unknown) or 'FALSE')+' THEN 1 ELSE 0 END AS scope_unknown'+period+' FROM prepared p'+(' WHERE '+' AND '.join(where) if where else '')+')')
    output=[];group_columns=[]
    for code in chosen:
        for item in dims[code]['columns']:
            col=item if isinstance(item,str) else item.get('alias',item['column'])
            if col in output:raise AnalysisQueryError('CONTRACT_UNAVAILABLE','源维度输出重复。')
            output.append(col);group_columns.append(_quote_column(col))
    if bucket:output.append('period');group_columns.append('period')
    sources={'rolls':'piece_num','ddp_rmb':'ddp_amount_rmb'}
    if delivery:sources.update(sales_rmb='sale_amount_rmb',tagged_ddp_gap_rmb='(ddp_amount_rmb-sale_amount_rmb)')
    expressions={};missing={}
    for name,expr in sources.items():
        expressions[name]=expr;missing[name]=f'{expr} IS NULL'
        if not name.startswith('tagged_'):
            expressions['tagged_'+name]=expr;missing['tagged_'+name]=f'({expr} IS NULL AND {flag}=\'y\') OR {flag} IS NULL OR {flag} NOT IN (\'y\',\'n\')'
    if delivery:missing['tagged_ddp_gap_rmb']=f'(ddp_amount_rmb IS NULL OR sale_amount_rmb IS NULL) AND {flag}=\'y\' OR {flag} IS NULL OR {flag} NOT IN (\'y\',\'n\')'
    quality={
      'unknown_tags':f"{flag} IS NULL OR {flag} NOT IN ('y','n')",'missing_rolls':'piece_num IS NULL','missing_ddp':'ddp_amount_rmb IS NULL',
      'missing_sales':'sale_amount_rmb IS NULL' if delivery else 'FALSE','negative_roll_rows':'piece_num<0','scope_unknown':'scope_unknown=1',
      'unknown_channels':"source_channel='未识别渠道'",'no_source_rows':"source_channel='无来源'",'multiple_channels':'source_channel_cnt>1','multiple_roots':'root_candidate_cnt>1',
      'unknown_candidates':'source_channel_cnt IS NULL OR source_channel_cnt<0 OR root_candidate_cnt IS NULL OR root_candidate_cnt<0',
      'unknown_cohort_labels':"inventory_cohort NOT IN ('本期形成','历史遗留')",
      'formation_pending':"formation_assessment LIKE '待核验%%'",'missing_formation':'cohort_basis_time IS NULL','cross_year_roots':'cross_year_roots=1',
      'low_formation_confidence':"cohort_confidence='低'",'in_transit_rows':'current_status=3','current_status_unknown':'current_status_unknown=1',
      'source_amount_exceptions':"amount_exception_flag='y'" if delivery else 'FALSE'}
    aggregates=['COUNT(*) AS n']
    for name,expr in expressions.items():
        condition=f"{flag}='y' AND scope_unknown=0" if name.startswith('tagged_') else 'scope_unknown=0'
        known_condition=(f"scope_unknown=0 AND ({flag}='n' OR ({flag}='y' AND {expr} IS NOT NULL))" if name.startswith('tagged_') else f'scope_unknown=0 AND {expr} IS NOT NULL')
        aggregates += [f'SUM(CASE WHEN {condition} THEN {expr} ELSE 0 END) AS v_{name}',
                       f'SUM(CASE WHEN {missing[name]} OR scope_unknown=1 THEN 1 ELSE 0 END) AS m_{name}',
                       f'SUM(CASE WHEN {known_condition} THEN 1 ELSE 0 END) AS k_{name}']
    aggregates += [f'SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS q_{name}' for name,expr in quality.items()]
    a_sql=','.join(aggregates)
    ctes.append('grouped AS (SELECT '+(','.join(group_columns)+',' if group_columns else '')+a_sql+' FROM scoped'+(' GROUP BY '+','.join(group_columns) if group_columns else '')+')')
    # The complete grouping precedes LIMIT. Reuse it for denominators instead
    # of scanning and recomputing formation/source expressions a second time.
    sum_fields=['n']+[prefix+name for name in expressions for prefix in ('v_','m_','k_')]+['q_'+name for name in quality]
    total_sql=','.join(f'SUM({name}) AS {name}' for name in sum_fields)
    ctes.append('totals AS (SELECT '+('period,' if bucket else '')+total_sql+' FROM grouped'+(' GROUP BY period' if bucket else '')+')')
    business=f'MIN(delivery_time) AS business_min,MAX(delivery_time) AS business_max,' if delivery else 'NULL AS business_min,NULL AS business_max,'
    ctes.append(f'meta AS (SELECT COUNT(*) AS scope_rows,COUNT(*)-COUNT(DISTINCT {_quote_column(pk)}) AS identity_errors,{business} MIN(etl_time) AS etl_min,MAX(etl_time) AS etl_max,COUNT(DISTINCT etl_time) AS etl_times,SUM(etl_time IS NULL) AS missing_etl FROM scoped)')
    ctes.append('population AS (SELECT COUNT(*) AS groups_n FROM grouped)')
    fields_out=[f'g.{_quote_column(c)}' for c in output]
    fields_out+=['CASE WHEN m.identity_errors>0 OR g.q_scope_unknown>0 THEN NULL ELSE g.n END AS metric_value',
                 'g.n AS __matched_row_count','g.n AS fabric_group_rows','m.scope_rows AS fabric_scope_rows','m.identity_errors AS fabric_identity_errors',
                 'clock.read_at AS fabric_read_at','clock.read_utc_at AS fabric_read_utc_at','m.etl_min AS fabric_etl_min','m.etl_max AS fabric_etl_max',
                 'm.etl_times AS fabric_etl_time_count','m.missing_etl AS fabric_missing_etl','m.business_min AS fabric_business_time_min','m.business_max AS fabric_business_time_max','population.groups_n AS fabric_population_groups']
    for name in expressions:
        full=f'CASE WHEN m.identity_errors>0 OR g.m_{name}>0 THEN NULL ELSE COALESCE(g.v_{name},0) END'
        known=f'CASE WHEN m.identity_errors>0 OR g.q_scope_unknown>=g.n OR g.k_{name}=0 THEN NULL ELSE g.v_{name} END'
        fields_out.extend([full+' AS fabric_'+name,known+' AS fabric_known_'+name,f'g.m_{name} AS fabric_unresolved_{name}_rows'])
    fields_out += [f'g.q_{name} AS fabric_{name}' for name in quality]
    rate_valid='m.identity_errors=0 AND g.m_rolls=0 AND g.m_tagged_rolls=0 AND g.q_negative_roll_rows=0'
    total_valid='m.identity_errors=0 AND t.m_tagged_rolls=0 AND t.q_negative_roll_rows=0'
    fields_out += [f'CASE WHEN {rate_valid} AND g.v_rolls>0 THEN 1.0*g.v_tagged_rolls/g.v_rolls ELSE NULL END AS fabric_tag_rate',
        f'CASE WHEN {rate_valid} AND {total_valid} AND t.v_tagged_rolls>0 THEN 1.0*g.v_tagged_rolls/t.v_tagged_rolls ELSE NULL END AS fabric_tag_contribution',
        f'CASE WHEN {total_valid} THEN COALESCE(t.v_tagged_rolls,0) ELSE NULL END AS fabric_scope_tagged_rolls',
        "CASE WHEN m.identity_errors>0 OR g.q_scope_unknown>0 OR g.q_missing_rolls>0 OR g.q_missing_ddp>0 OR g.q_unknown_tags>0 OR g.q_missing_sales>0 THEN 'incomplete' ELSE 'complete' END AS metric_data_state"]
    join='g.period <=> t.period' if bucket else 'TRUE'
    sql='WITH '+',\n'.join(ctes)+' SELECT '+','.join(fields_out)+' FROM meta m CROSS JOIN clock CROSS JOIN population LEFT JOIN grouped g ON TRUE LEFT JOIN totals t ON '+join
    if output:sql+=' ORDER BY '+','.join('g.'+_quote_column(c) for c in output)
    sql+=' LIMIT %s';params.append(limit+1)
    return sql,params,{'metric':request['metric'],'dataset':table,'source_datasets':[table]+([] if delivery else [metric['status_table']]),'dimension_outputs':output,'effective_dimensions':chosen,'time_range':applied,'filters':filters,'warnings':[],'_fabric_observation':True}


def fabric_observation(rows,scope):
    if not rows:raise AnalysisQueryError('SOURCE_OBSERVATION_MISSING','源摘要没有读取时点证据。')
    first=rows[0];keys=['fabric_read_at','fabric_read_utc_at','fabric_etl_min','fabric_etl_max','fabric_etl_time_count','fabric_missing_etl','fabric_scope_rows']
    if any(any(r.get(k)!=first.get(k) for k in keys) for r in rows):raise AnalysisQueryError('SOURCE_OBSERVATION_INVALID','源读取元信息不一致。')
    return {**scope,**{k:first.get(k) for k in keys}}
