"""Current recorded finding-task evidence at task, execution and linked-detail grains."""
from datetime import datetime


def build_pattern_query(request, metric, datasets_contract, semantics, limit, *, observed_on=None):
    from .analytical_queries import (AnalysisQueryError, _dataset, _approved, _quote_table,
        _value_filter, _entity_bindings, _bound_value, _dimension_filter, _time_window)
    view = metric.get('pattern_view')
    if view not in {'summary','linked_amount','person_attributed_amount'}:
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE','找版查询视图未登记。')
    rmb_metric = metric.get('measure') == 'delivery_amount_rmb'
    if any(request.get(k) is not None for k in ('comparison','baseline_week','movement_state','order_by','attribution_mode')):
        raise AnalysisQueryError('INVALID_PLAN','找版仅提供当前观察和明确时间口径的队列观察，按稳定键排序。')
    basis=request.get('pattern_time_basis','current_observation')
    if basis not in metric.get('allowed_time_bases',[]):
        raise AnalysisQueryError('INVALID_PLAN','该找版指标不支持所选时间口径。')
    if basis=='current_observation' and (request.get('time_range') is not None or request.get('calendar_month') is not None or request.get('time_bucket') is not None):
        raise AnalysisQueryError('INVALID_PLAN','指定期间时必须明确任务创建、执行完成或关联出库发生口径。')
    table=metric['table'];linked_table=metric['linked_table']
    ds=_dataset(table,datasets_contract);sd=_dataset(linked_table,datasets_contract)
    fields=['task_id','task_no','task_type','customer_id','customer_name','sales_id','sales_name','task_region','task_status','task_create_time','task_modified_time','execute_id','executor_id','executor_erp_id','executor_name','execute_status','execute_modified_time','complete_time','is_find','is_suitable','is_receive','final_goods_no','sale_bill_no','sale_goods_detail_id','delivery_amount','currency_no']
    for field in fields:_approved(field,ds)
    for field in ['goods_detail_id','sale_bill_id','goods_no','sales_id','delivery_time','bill_status','delivery_amount','delivery_amount_rmb','currency_no']:_approved(field,sd)
    qt,qs=_quote_table(table),_quote_table(linked_table)
    # IDs own grain; names are labels only and never a deduplication key.
    dimension_keys={'task':'task_id','customer':'customer_id','salesperson':'sales_id','executor':'executor_id','candidate_product':'candidate_product_label','task_region':'task_region','task_type':'task_type','task_status':'task_status_label','execute_status':'execute_status_label','currency':'currency_no'}
    labels={'task':('task_number','task_no'),'customer':('customer_name','customer_name'),'salesperson':('sales_name','sales_name'),'executor':('executor_name','executor_name')}
    chosen=request.get('dimensions')
    if not chosen:
        chosen=[] if view=='summary' or rmb_metric else ['currency']
    if not set(chosen)<=set(metric.get('allowed_dimensions',[])):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','找版分组维度不支持。')
    if view!='summary' and not rmb_metric and 'currency' not in chosen:
        raise AnalysisQueryError('CURRENCY_SCOPE_REQUIRED','关联金额必须按已核验交易币种分组。')
    filters=request.get('metric_filters') or {}
    if not isinstance(filters,dict) or not set(filters)<=set(metric.get('allowed_dimensions',[])):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','找版筛选维度不支持。')
    params=[];clauses=[];unknown=[]
    bindings=_entity_bindings(request)
    for key,value in filters.items():
        binding,value=_bound_value(bindings,key,value)
        if key in {'task_status','execute_status'}:
            values=value if isinstance(value,list) else [value]
            if any(isinstance(v,bool) or not (isinstance(v,int) and v>=0 or isinstance(v,str) and v.isascii() and v.isdecimal()) for v in values):
                raise AnalysisQueryError('INVALID_PLAN','状态筛选必须使用明确原始整数代码，不用状态名称代替。')
        definition=semantics['dimensions'][key]
        column=_dimension_filter(definition,binding)
        expression=_value_filter('r',column,value,params)
        clauses.append(f'({expression} OR r.{column} IS NULL)')
        unknown.append(f'r.{column} IS NULL')
    window={}
    time_column={'task_created':'task_create_time','execution_completed':'complete_time','linked_delivery':'linked_delivery_time'}.get(basis)
    if time_column:
        start,end,window=_time_window(request,'current_month',observed_on)
        params.extend([start,end]);clauses.append(f'(r.{time_column} IS NULL OR (r.{time_column}>=%s AND r.{time_column}<%s))')
        unknown.append(f'r.{time_column} IS NULL')
        if basis=='task_created':unknown.append('r.task_time_variants<>1')
    bucket=request.get('time_bucket')
    if bucket not in {None,'month'}:raise AnalysisQueryError('INVALID_PLAN','找版仅支持明确业务时间的月分组。')
    grouping=[dimension_keys[k] for k in chosen]
    if bucket=='month':grouping.append('period')
    # Global quality checks run before scope filtering, so conflicts cannot be hidden by a filter.
    ctes=[f"clock AS (SELECT NOW(6) AS pattern_read_at,UTC_TIMESTAMP(6) AS pattern_read_utc_at)",
      f"p AS (SELECT {','.join(fields)} FROM {qt})",
      "task_numbers AS (SELECT task_no,COUNT(DISTINCT task_id) AS task_ids FROM p GROUP BY task_no)",
      "task_quality AS (SELECT task_id,COUNT(*) AS n,COUNT(task_no) AS numbered,COUNT(DISTINCT task_no) AS numbers,COUNT(DISTINCT task_create_time) AS task_time_variants,COUNT(DISTINCT task_status) AS task_status_variants FROM p GROUP BY task_id)",
      "execute_quality AS (SELECT execute_id,COUNT(DISTINCT task_id) AS task_ids,COUNT(DISTINCT executor_id) AS executor_ids,COUNT(DISTINCT execute_status) AS execute_status_variants FROM p GROUP BY execute_id)",
      "detail_quality AS (SELECT sale_goods_detail_id,COUNT(*) AS n,COUNT(delivery_amount) AS amounts,COUNT(DISTINCT delivery_amount) AS amount_variants,COUNT(DISTINCT currency_no) AS requirement_currency_variants FROM p WHERE sale_goods_detail_id IS NOT NULL GROUP BY sale_goods_detail_id)",
      f"sales_details AS (SELECT s.goods_detail_id,COUNT(*) AS n,MAX(s.sale_bill_id) AS sale_bill_id,MAX(s.goods_no) AS goods_no,MAX(s.sales_id) AS sales_id,MAX(s.delivery_time) AS delivery_time,MAX(s.bill_status) AS bill_status,MAX(s.delivery_amount) AS delivery_amount,MAX(s.delivery_amount_rmb) AS delivery_amount_rmb,COUNT(DISTINCT s.delivery_amount_rmb) AS rmb_amount_variants,MAX(s.currency_no) AS currency_no FROM {qs} s WHERE EXISTS(SELECT 1 FROM p WHERE p.sale_goods_detail_id=s.goods_detail_id) GROUP BY s.goods_detail_id)"]
    task_ok="p.task_id IS NOT NULL AND t.n=t.numbered AND t.numbers=1 AND tn.task_ids=1"
    exec_ok=f"({task_ok}) AND p.execute_id IS NOT NULL AND e.task_ids=1 AND e.executor_ids<=1"
    product_ok="NULLIF(TRIM(p.final_goods_no),'') IS NOT NULL AND p.final_goods_no NOT LIKE '%%,%%'"
    source_ok="s.n=1 AND s.sale_bill_id IS NOT NULL AND s.bill_status=6 AND s.delivery_amount IS NOT NULL AND s.currency_no REGEXP '^[A-Z]{3}$'"
    link_ok=f"({source_ok}) AND d.n=d.amounts AND d.amount_variants=1 AND p.delivery_amount=s.delivery_amount AND p.sales_id=s.sales_id AND ({product_ok}) AND p.final_goods_no=s.goods_no AND p.complete_time IS NOT NULL AND p.complete_time<s.delivery_time"
    ctes.append(f"raw AS (SELECT p.*,CASE WHEN {task_ok} THEN 1 ELSE 0 END AS task_key_ok,CASE WHEN {exec_ok} THEN 1 ELSE 0 END AS execute_key_ok,CASE WHEN {product_ok} THEN 1 ELSE 0 END AS product_key_ok,t.task_time_variants,t.task_status_variants,e.execute_status_variants,CASE WHEN {link_ok} THEN 1 ELSE 0 END AS link_ok,CASE WHEN {link_ok} AND s.delivery_amount_rmb IS NOT NULL AND s.rmb_amount_variants=1 THEN 1 ELSE 0 END AS rmb_ready,CASE WHEN p.sale_goods_detail_id IS NOT NULL OR NULLIF(p.sale_bill_no,'') IS NOT NULL OR p.delivery_amount IS NOT NULL THEN 1 ELSE 0 END AS link_recorded,CASE WHEN s.n=1 THEN s.currency_no ELSE NULL END AS transaction_currency,CASE WHEN s.n=1 THEN s.delivery_time ELSE NULL END AS linked_delivery_time,CASE WHEN s.n=1 THEN s.delivery_amount ELSE NULL END AS linked_amount,CASE WHEN s.n=1 THEN s.delivery_amount_rmb ELSE NULL END AS linked_amount_rmb,CASE WHEN s.n=1 THEN s.sale_bill_id ELSE NULL END AS linked_bill_id,d.requirement_currency_variants FROM p LEFT JOIN task_quality t ON p.task_id=t.task_id LEFT JOIN task_numbers tn ON p.task_no=tn.task_no LEFT JOIN execute_quality e ON p.execute_id=e.execute_id LEFT JOIN detail_quality d ON p.sale_goods_detail_id=d.sale_goods_detail_id LEFT JOIN sales_details s ON p.sale_goods_detail_id=s.goods_detail_id)")
    ctes.append("prepared AS (SELECT raw.*,final_goods_no AS candidate_product_label,CASE task_status WHEN 1 THEN '待审核' WHEN 2 THEN '进行中' WHEN 3 THEN '完结' WHEN 4 THEN '待执行' WHEN 5 THEN '待回复' ELSE CASE WHEN task_status IS NULL THEN '未填' ELSE CONCAT('未定义(',task_status,')') END END AS task_status_label,CASE execute_status WHEN 1 THEN '进行中' WHEN 3 THEN '已完成' ELSE CASE WHEN execute_status IS NULL THEN '未填' ELSE CONCAT('未定义(',execute_status,')') END END AS execute_status_label FROM raw)")
    # Rename requirement currency before filtering; only the linked transaction supplies amount currency.
    columns=[f for f in fields if f!='currency_no']
    extras=['task_key_ok','execute_key_ok','product_key_ok','task_time_variants','task_status_variants','execute_status_variants','link_ok','rmb_ready','link_recorded','linked_delivery_time','linked_amount','linked_amount_rmb','linked_bill_id','requirement_currency_variants','candidate_product_label','task_status_label','execute_status_label']
    # A task without a recorded/suspected link is outside an amount population,
    # not a linked fact with missing transaction time or currency. Keep suspected
    # links (including missing detail IDs) so genuine coverage gaps still propagate.
    amount_population = ' WHERE link_recorded=1' if view != 'summary' else ''
    ctes.append('r AS (SELECT '+','.join(columns+extras)+',currency_no AS requirement_currency,transaction_currency AS currency_no FROM prepared'+amount_population+')')
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    scope_unknown=' OR '.join(unknown) if unknown else 'FALSE'
    period=f",DATE_FORMAT(r.{time_column},'%%Y-%%m') AS period" if bucket=='month' else ''
    ctes.append(f"scoped AS (SELECT r.*,CASE WHEN {scope_unknown} THEN 1 ELSE 0 END AS scope_unknown{period} FROM r{where})")
    ctes.append("observation AS (SELECT MAX(task_modified_time) AS pattern_task_modified_max,MAX(execute_modified_time) AS pattern_execute_modified_max FROM p)")
    ctes.append("coverage AS (SELECT COUNT(*) AS pattern_scope_rows,COALESCE(SUM(scope_unknown),0) AS pattern_scope_unknown_rows FROM scoped)")
    if request.get('_currency_scope_probe'):
        # The automatic basis probe is an aggregate over the complete scoped
        # population. It deliberately has no outer LIMIT or pagination
        # parameter; the generic currency resolver wraps it in a one-row count.
        probe_currency = "CASE WHEN NULLIF(TRIM(currency_no),'') IS NULL OR currency_no NOT REGEXP '^[A-Z]{3}$' THEN NULL ELSE currency_no END"
        ctes.append("scope_probe AS (SELECT " + probe_currency + " AS currency_no,COUNT(*) AS __matched_row_count FROM scoped GROUP BY " + probe_currency + ")")
        probe_sql = (
            'WITH ' + ',\n'.join(ctes)
            + ' SELECT scope_probe.*,clock.*,observation.*,coverage.* FROM clock CROSS JOIN observation CROSS JOIN coverage CROSS JOIN scope_probe'
        )
        return probe_sql, params, {
            'metric': request.get('metric'), 'dataset': None,
            'source_datasets': [table, linked_table],
            'dimension_outputs': ['currency_no'],
            'filters': filters,
            'time_range': {'source': 'pattern_current_observation', 'basis': basis,
                           'window_start': window.get('start'), 'window_end': window.get('end')},
            '_validate_pattern_observation': True,
            'effective_dimensions': ['currency'],
            'identity_outputs': {},
            'warnings': ['币种范围探针覆盖完整受控范围；缺失币种、关联和人民币缺口沿用现有 scope/link 状态。'],
        }
    groupcols=','.join(grouping)
    labelsql=[]
    for key in chosen:
        if key in labels:
            alias,column=labels[key]
            labelsql.append(f"CASE WHEN COUNT(DISTINCT {column})=1 THEN MAX({column}) ELSE NULL END AS {alias}")
    projection=','.join([*grouping,*labelsql])
    projection=projection+',' if projection else ''
    groupby=' GROUP BY '+groupcols if grouping else ''
    identity_outputs = {}
    if 'executor' in chosen:
        # The pattern contract uses executor_erp_id only as the governed
        # salesperson filter identity.  It must not become the source group
        # key, which remains executor_id above.
        identity_outputs['executor'] = 'executor_filter_identity'
    if view=='summary':
        known="task_key_ok=1 AND scope_unknown=0"
        count=lambda condition: f'COUNT(DISTINCT CASE WHEN {known} AND ({condition}) THEN task_id ELSE NULL END)'
        stats={
          'known_task_count':count('TRUE'),
          'known_execution_count':'COUNT(DISTINCT CASE WHEN execute_key_ok=1 AND scope_unknown=0 THEN execute_id ELSE NULL END)',
          'known_executor_count':'COUNT(DISTINCT CASE WHEN scope_unknown=0 THEN executor_id ELSE NULL END)',
          'known_candidate_product_count':'COUNT(DISTINCT CASE WHEN product_key_ok=1 AND scope_unknown=0 THEN final_goods_no ELSE NULL END)',
          'recorded_linked_detail_count':'COUNT(DISTINCT CASE WHEN scope_unknown=0 THEN sale_goods_detail_id ELSE NULL END)',
          'verified_linked_detail_count':'COUNT(DISTINCT CASE WHEN scope_unknown=0 AND link_ok=1 THEN sale_goods_detail_id ELSE NULL END)',
          'verified_linked_bill_count':'COUNT(DISTINCT CASE WHEN scope_unknown=0 AND link_ok=1 THEN linked_bill_id ELSE NULL END)',
          'requirement_currency_missing_rows':"SUM(CASE WHEN requirement_currency IS NULL OR requirement_currency='' THEN 1 ELSE 0 END)",
          'requirement_currency_conflict_details':'COUNT(DISTINCT CASE WHEN requirement_currency_variants>1 THEN sale_goods_detail_id ELSE NULL END)',
          'rows_without_execution_id':'SUM(CASE WHEN execute_id IS NULL THEN 1 ELSE 0 END)',
          'recorded_linked_bill_count':"COUNT(DISTINCT CASE WHEN scope_unknown=0 THEN NULLIF(sale_bill_no,'') ELSE NULL END)",
          'tasks_with_recorded_link':count('link_recorded=1'),
          'tasks_with_verified_link':count('link_ok=1'),
          'tasks_with_found_record':count("is_find='y'"),'tasks_with_not_found_record':count("is_find='n'"),
          'tasks_with_suitable_record':count("is_suitable='y'"),'tasks_with_unsuitable_record':count("is_suitable='n'"),
          'tasks_with_received_record':count("is_receive='y'"),'tasks_with_not_received_record':count("is_receive='n'"),
          'tasks_with_unfilled_found':count("is_find IS NULL OR is_find=''"),
          'tasks_with_unfilled_feedback':count("is_suitable IS NULL OR is_suitable=''"),
          'tasks_with_unfilled_receive':count("is_receive IS NULL OR is_receive=''"),
          'tasks_with_completed_status_record':count('task_status=3'),
          'task_status_conflict_count':count('task_status_variants>1'),
          'execute_status_conflict_count':'COUNT(DISTINCT CASE WHEN execute_status_variants>1 THEN execute_id ELSE NULL END)',
          'task_status_unfilled_rows':'SUM(CASE WHEN task_status IS NULL THEN 1 ELSE 0 END)',
          'task_status_unknown_rows':'SUM(CASE WHEN task_status IS NOT NULL AND task_status NOT IN (1,2,3,4,5) THEN 1 ELSE 0 END)',
          'execute_status_unfilled_rows':'SUM(CASE WHEN execute_status IS NULL THEN 1 ELSE 0 END)',
          'execute_status_unknown_rows':'SUM(CASE WHEN execute_status IS NOT NULL AND execute_status NOT IN (1,3) THEN 1 ELSE 0 END)',
          'unrecognized_flag_rows':"SUM(CASE WHEN (is_find IS NOT NULL AND is_find NOT IN ('','y','n')) OR (is_suitable IS NOT NULL AND is_suitable NOT IN ('','y','n')) OR (is_receive IS NOT NULL AND is_receive NOT IN ('','y','n')) THEN 1 ELSE 0 END)",
          'task_identity_unknown_rows':'SUM(CASE WHEN task_key_ok=0 THEN 1 ELSE 0 END)',
          'execution_identity_unknown_rows':'SUM(CASE WHEN execute_key_ok=0 THEN 1 ELSE 0 END)',
          'executor_identity_unknown_rows':'SUM(CASE WHEN executor_id IS NULL THEN 1 ELSE 0 END)',
          'candidate_product_unfilled_rows':"SUM(CASE WHEN final_goods_no IS NULL OR final_goods_no='' THEN 1 ELSE 0 END)",
          'candidate_product_nonatomic_rows':"SUM(CASE WHEN final_goods_no LIKE '%%,%%' THEN 1 ELSE 0 END)",
          'recorded_unverified_link_rows':'SUM(CASE WHEN link_recorded=1 AND link_ok=0 THEN 1 ELSE 0 END)',
        }
        identity_projection = (
            "CASE WHEN executor_id IS NOT NULL "
            "AND COUNT(DISTINCT executor_erp_id)=1 "
            "THEN MAX(executor_erp_id) ELSE NULL END AS executor_filter_identity,"
            if 'executor' in chosen else ''
        )
        ctes.append('grouped AS (SELECT '+projection+identity_projection+'COUNT(*) AS result_source_rows,'+','.join(f'{expr} AS {name}' for name,expr in stats.items())+' FROM scoped'+groupby+')')
        value='CASE WHEN COALESCE(g.task_identity_unknown_rows,0)=0 AND pattern_scope_unknown_rows=0 THEN g.known_task_count ELSE NULL END'
        known_value='g.known_task_count'
        missing='COALESCE(g.task_identity_unknown_rows,0)+pattern_scope_unknown_rows'
        extra=''
    else:
        person = view=='person_attributed_amount'
        identity=['task_id','executor_id','final_goods_no'] if person else []
        dedup=list(dict.fromkeys([*grouping,*identity,'sale_goods_detail_id']))
        bad='link_ok=0 OR scope_unknown=1'+(' OR task_key_ok=0 OR executor_id IS NULL OR product_key_ok=0' if person else '')+(' OR rmb_ready=0' if rmb_metric else '')
        amount_field='linked_amount_rmb' if rmb_metric else 'linked_amount'
        include_currency_totals='currency' in chosen
        # One amount per stable detail or owner-confirmed person attribution key, never SUM(DISTINCT amount).
        amount_identity_projection = (
            ",COUNT(DISTINCT executor_erp_id) AS executor_filter_identity_count,"
            "MAX(executor_erp_id) AS executor_filter_identity"
            if 'executor' in chosen else ''
        )
        ctes.append('amount_keys AS (SELECT '+','.join(dedup)+f",COUNT(*) AS attribution_source_rows,SUM(CASE WHEN {bad} THEN 1 ELSE 0 END) AS bad_rows,MAX({amount_field}) AS linked_amount"+amount_identity_projection+" FROM scoped WHERE link_recorded=1 GROUP BY "+','.join(dedup)+')')
        # Labels derive from the scoped identity, not from arbitrarily selected duplicate amounts.
        amount_projection=groupcols+',' if groupcols else ''
        identity_projection = (
            "CASE WHEN MAX(executor_id) IS NOT NULL "
            "AND COUNT(DISTINCT executor_filter_identity)=1 "
            "THEN MAX(executor_filter_identity) ELSE NULL END AS executor_filter_identity,"
            if 'executor' in chosen else ''
        )
        ctes.append('grouped AS (SELECT '+amount_projection+identity_projection+'COUNT(*) AS amount_key_count,SUM(attribution_source_rows) AS result_source_rows,SUM(CASE WHEN bad_rows=0 THEN linked_amount ELSE 0 END) AS known_amount,SUM(CASE WHEN bad_rows=0 THEN 1 ELSE 0 END) AS known_amount_keys,SUM(bad_rows) AS unresolved_amount_rows FROM amount_keys'+groupby+')')
        if include_currency_totals:
            ctes.append('currency_keys AS (SELECT '+','.join(['currency_no',*identity,'sale_goods_detail_id'])+f",SUM(CASE WHEN {bad} THEN 1 ELSE 0 END) AS bad_rows,MAX({amount_field}) AS linked_amount FROM scoped WHERE link_recorded=1 GROUP BY "+','.join(['currency_no',*identity,'sale_goods_detail_id'])+')')
            ctes.append('currency_totals AS (SELECT currency_no,SUM(CASE WHEN bad_rows=0 THEN linked_amount ELSE 0 END) AS currency_known_amount,COUNT(*) AS currency_amount_keys,SUM(bad_rows) AS currency_unresolved_rows FROM currency_keys GROUP BY currency_no)')
        value='CASE WHEN g.unresolved_amount_rows=0 AND pattern_scope_unknown_rows=0 THEN g.known_amount ELSE NULL END'
        known_value='g.known_amount';missing='COALESCE(g.unresolved_amount_rows,0)+pattern_scope_unknown_rows'
        extra=',ct.currency_known_amount,ct.currency_amount_keys,ct.currency_unresolved_rows' if include_currency_totals else ''
    ctes.append('population AS (SELECT COUNT(*) AS pattern_display_groups FROM grouped)')
    label_join=''
    # Amount grouping label enrichment is separate and one row per group.
    if view!='summary' and labelsql:
        ctes.append('group_labels AS (SELECT '+groupcols+','+','.join(labelsql)+' FROM scoped'+groupby+')')
        label_join=' LEFT JOIN group_labels gl ON '+' AND '.join('g.'+k+' <=> gl.'+k for k in grouping)
        extra+=','+','.join('gl.'+labels[k][0] for k in chosen if k in labels)
    amount_join=' LEFT JOIN currency_totals ct ON g.currency_no <=> ct.currency_no' if view!='summary' and 'currency' in chosen else ''
    order=' ORDER BY '+','.join('g.'+k for k in grouping) if grouping else ''
    sql='WITH '+',\n'.join(ctes)+f' SELECT g.*,clock.*,observation.*,coverage.*,population.*{extra},{value} AS metric_value,{known_value} AS known_subset_value,{missing} AS missing_value_count,COALESCE(g.result_source_rows,0) AS known_value_count,COALESCE(g.result_source_rows,0) AS __matched_row_count FROM clock CROSS JOIN observation CROSS JOIN coverage CROSS JOIN population LEFT JOIN grouped g ON TRUE'+amount_join+label_join+order+' LIMIT %s'
    params.append(limit+1)
    outputs=[*grouping,*[labels[k][0] for k in chosen if k in labels]]
    return sql,params,{'metric':request.get('metric'),'dataset':None,'source_datasets':[table,linked_table],'dimension_outputs':outputs,'identity_outputs':identity_outputs,'filters':filters,'time_range':{'source':'pattern_current_observation','basis':basis,'window_start':window.get('start'),'window_end':window.get('end')},'_validate_pattern_observation':True,'effective_dimensions':chosen,'warnings':[metric.get('answer_note','')]}


def pattern_observation(rows, scope):
    from .analytical_queries import AnalysisQueryError
    if not rows:raise AnalysisQueryError('OBSERVATION_EVIDENCE_MISSING','找版查询未返回观察时点。')
    keys=('pattern_read_at','pattern_read_utc_at','pattern_task_modified_max','pattern_execute_modified_max')
    first=rows[0]
    if any(any(row.get(key)!=first.get(key) for key in keys) for row in rows):
        raise AnalysisQueryError('OBSERVATION_EVIDENCE_MISSING','找版观察元信息不一致。')
    try:datetime.fromisoformat(str(first['pattern_read_at']));datetime.fromisoformat(str(first['pattern_read_utc_at']))
    except (KeyError,ValueError):raise AnalysisQueryError('OBSERVATION_EVIDENCE_MISSING','找版读取时点缺失。')
    return {**scope,**{key:first.get(key) for key in keys}}
