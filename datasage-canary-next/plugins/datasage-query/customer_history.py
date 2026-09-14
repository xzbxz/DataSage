"""Read-only product/department purchase relationships in an explicit existing weekly pool."""
from datetime import date, datetime
from calendar import monthrange
import hashlib
from collections.abc import Mapping
from .analytical_handlers import validate_parameters
from .analytical_queries import (AnalysisQueryError, _dataset, _approved, _quote_table,
    _value_filter, _entity_bindings, _bound_value, frozen_baseline_cohort,
    recorded_flow_predicates, validate_frozen_pool_rows)

TIME_SOURCE = 'fixed_12_calendar_month_customer_history'

FACT_FIELDS = {'history_customer_no','history_known_first_outbound_at','history_known_last_outbound_at','history_known_order_count','history_customer_ref','history_product_ref','history_relation_ref',
    'history_first_outbound_at','history_last_outbound_at','history_order_count',
    'history_outbound_rows','history_return_rows','history_outbound_quantity','history_return_quantity',
    'history_outbound_known_quantity','history_return_known_quantity','history_outbound_rolls','history_return_rolls',
    'history_outbound_known_rolls','history_return_known_rolls','history_outbound_missing_qty','history_return_missing_qty',
    'history_outbound_missing_rolls','history_return_missing_rolls','history_missing_order_rows',
    'history_scope_relations','history_scope_customers','history_known_relations','history_known_customers',
    'history_display_groups','history_outbound_unknown_rows','history_returns_unknown_rows',
    'history_current_owner','history_customer_master_rows','history_customer_details_missing',
    'history_customer_name_variants','history_product_name_variants'}


def build_history_query(request, metric, datasets, semantics, limit, *, observed_on):
    validate_parameters(metric.get('query_kind'), request)
    week=request.get('baseline_week')
    try:
        if not isinstance(week,str) or len(week)!=8 or week[4:6]!='-W':raise ValueError()
        start=date.fromisocalendar(int(week[:4]),int(week[6:]),1)
        if start>observed_on:raise ValueError()
    except (ValueError,TypeError):raise AnalysisQueryError('BASELINE_WEEK_REQUIRED','必须明确选择当前或过去的既有周基线；不猜默认池、不创建基线。',path='baseline_week')
    chosen=request.get('dimensions') or ['customer','product','warehouse_department','unit']
    if set(chosen)!={'customer','product','warehouse_department','unit'}:
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','历史关系按客户、产品、仓库部门及记录单位分列。',path='dimensions')
    filters=request.get('metric_filters') or {}
    if not set(filters)<={'customer','product','warehouse_department','unit'}:
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','首批仅支持客户、产品、仓库部门、记录单位筛选。')
    base = metric.get('pool_definition')
    if (not isinstance(base, Mapping) or set(base) != {'table', 'baseline_source_table', 'flow_sources'}
            or not all(isinstance(base.get(k), str) and base[k] for k in ('table', 'baseline_source_table'))
            or not isinstance(base.get('flow_sources'), Mapping)
            or set(base['flow_sources']) != {'outbound', 'returns', 'sales', 'warehouses'}
            or not all(isinstance(v, str) and v for v in base['flow_sources'].values())
            or metric.get('table') != base['table']):
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE', '历史客户缺少一致的受控池来源定义。')
    _dataset(base['baseline_source_table'], datasets)
    ctes,params=frozen_baseline_cohort(base,datasets,week,week)
    bindings=_entity_bindings(request)
    def condition(alias,fields):
        parts=[]
        for key,col in fields.items():
            if key not in filters:continue
            _,value=_bound_value(bindings,key,filters[key])
            parts.append('('+_value_filter(alias,col,value,params)+f' OR {alias}.{col} IS NULL)')
        return ' AND '.join(parts) or '1=1'
    ctes.append('pool_products AS (SELECT DISTINCT goods_id,whse_dept COLLATE utf8mb4_bin AS whse_dept FROM base_raw k WHERE '+condition('k',{'product':'goods_id','warehouse_department':'whse_dept'})+')')
    ctes.append('history_clock AS (SELECT DATE_SUB(closing_read_at,INTERVAL 12 MONTH) AS history_start,closing_read_at AS history_end FROM clock)')
    sources={**base['flow_sources'],'customers':metric['customer_table'],'products':metric['product_table']}
    cols={
      'outbound':['barcode_detail_id','goods_id','whse_dept','unit','goods_num','piece_num','delivery_time','whse_id','bill_type','is_inner_cus','sale_bill_goods_id'],
      'returns':['barcode_detail_id','customer_id','goods_id','whse_dept','unit','return_goods_num','return_piece_num','statement_time','in_whse_id','sale_bill_type','is_inner_cus','status','complnt_type','channel_type'],
      'sales':['goods_detail_id','bill_status','sale_bill_id','customer_id','customer_name','goods_id'],
      'warehouses':['whse_id','dept_name'], 'customers':['customer_id','customer_no','customer_name','sales_name','is_delete','is_void'],
      'products':['goods_id','goods_name']}
    for key,columns in cols.items():
        ds=_dataset(sources[key],datasets)
        for col in columns:_approved(col,ds)
    qt={k:_quote_table(v) for k,v in sources.items()}
    candidate='EXISTS(SELECT 1 FROM pool_products k WHERE (f.goods_id=k.goods_id OR f.goods_id IS NULL) AND (f.whse_dept=k.whse_dept OR f.whse_dept IS NULL))'
    delivery_columns=','.join('f.'+col for col in cols['outbound'])
    ctes.append(f"delivery_candidates AS (SELECT {delivery_columns} FROM {qt['outbound']} f WHERE {candidate} AND (f.delivery_time IS NULL OR (f.delivery_time>=(SELECT history_start FROM history_clock) AND f.delivery_time<(SELECT history_end FROM history_clock))))")
    # A grouped parent index never amplifies physical rows. Duplicates remain unknown, never selected arbitrarily.
    ctes.append(f"sales_index AS (SELECT goods_detail_id,COUNT(*) AS n,MAX(customer_id) AS customer_id,MAX(goods_id) AS goods_id,MAX(customer_name) AS customer_name,MAX(sale_bill_id) AS sale_bill_id FROM {qt['sales']} WHERE goods_detail_id IN (SELECT sale_bill_goods_id FROM delivery_candidates) GROUP BY goods_detail_id)")
    for side,outgoing in [('outbound',True),('returns',False)]:
        at,qty,rolls=('delivery_time','goods_num','piece_num') if outgoing else ('statement_time','return_goods_num','return_piece_num')
        legacy,valid,warehouse=recorded_flow_predicates(outgoing,qt)
        params.extend(['%-HT','bulk'])
        customer='s.customer_id' if outgoing else 'f.customer_id'
        # MySQL's untyped NULL has binary charset; COLLATE below needs utf8mb4.
        label='s.customer_name' if outgoing else 'CONVERT(NULL USING utf8mb4)'
        parent='s.n=1 AND s.goods_id=f.goods_id' if outgoing else '1=1'
        order='s.sale_bill_id' if outgoing else 'NULL'
        join=' LEFT JOIN sales_index s ON s.goods_detail_id=f.sale_bill_goods_id' if outgoing else ''
        source='delivery_candidates' if outgoing else qt[side]
        ctes.append(f"{side}_raw AS (SELECT f.barcode_detail_id,{customer} AS customer_id,NULLIF(TRIM({label}),'') COLLATE utf8mb4_bin AS customer_name,f.goods_id,f.whse_dept,CASE WHEN LOWER(TRIM(f.unit))='m' THEN 'm' WHEN TRIM(f.unit) IN ('y','kg','Pcs') THEN TRIM(f.unit) ELSE NULL END AS unit,f.{qty} AS qty,f.{rolls} AS rolls,f.{at} AS event_at,{order} AS order_id,{legacy} AS document_ok,{valid} AS valid_ok,{warehouse} AS warehouse_ok,({parent}) AS parent_ok FROM {source} f{join} WHERE {candidate} AND (f.{at} IS NULL OR (f.{at}>=(SELECT history_start FROM history_clock) AND f.{at}<(SELECT history_end FROM history_clock))))")
        selected=condition('r',{'customer':'customer_id','unit':'unit'})
        unknown='customer_id IS NULL OR goods_id IS NULL OR whse_dept IS NULL OR event_at IS NULL OR barcode_detail_id IS NULL OR warehouse_ok IS NULL OR warehouse_ok=0 OR parent_ok IS NULL OR parent_ok=0 OR valid_ok IS NULL OR document_ok IS NULL'
        if 'unit' in filters:unknown += ' OR unit IS NULL'
        ctes.append(f"{side}_classified AS (SELECT r.*,CASE WHEN valid_ok=0 OR document_ok=0 THEN 'excluded' WHEN {unknown} THEN 'unknown' ELSE 'matched' END AS match_state FROM {side}_raw r WHERE {selected})")
        ctes.append(f"{side}_quality AS (SELECT COUNT(barcode_detail_id)-COUNT(DISTINCT barcode_detail_id) AS history_{side}_duplicate_ids,COALESCE(SUM(CASE WHEN match_state='unknown' THEN 1 ELSE 0 END),0) AS history_{side}_unknown_rows FROM {side}_classified)")
        ctes.append(f"{side}_grouped AS (SELECT customer_id,goods_id,whse_dept,unit,COUNT(*) AS n,SUM(qty) AS qty,SUM(rolls) AS rolls,SUM(CASE WHEN qty IS NULL THEN 1 ELSE 0 END) AS missing_qty,SUM(CASE WHEN rolls IS NULL THEN 1 ELSE 0 END) AS missing_rolls FROM {side}_classified WHERE match_state='matched' GROUP BY customer_id,goods_id,whse_dept,unit)")
    ctes.append("relations AS (SELECT customer_id,goods_id,whse_dept,MIN(event_at) AS history_first_outbound_at,MAX(event_at) AS history_last_outbound_at,COUNT(DISTINCT order_id) AS history_order_count,SUM(CASE WHEN order_id IS NULL THEN 1 ELSE 0 END) AS history_missing_order_rows,CASE WHEN COUNT(DISTINCT NULLIF(customer_name,''))=1 THEN MAX(customer_name) ELSE NULL END AS historical_customer_name,COUNT(DISTINCT NULLIF(customer_name,'')) AS history_customer_name_variants FROM outbound_classified WHERE match_state='matched' GROUP BY customer_id,goods_id,whse_dept)")
    ctes.append("history_population AS (SELECT COUNT(*) AS history_known_relations,COUNT(DISTINCT customer_id) AS history_known_customers FROM relations)")
    keys='customer_id,goods_id,whse_dept,unit'
    ctes.append(f"display_keys AS (SELECT {keys} FROM outbound_grouped UNION SELECT {','.join('r.'+k for k in keys.split(','))} FROM returns_grouped r WHERE EXISTS(SELECT 1 FROM relations b WHERE b.customer_id=r.customer_id AND b.goods_id=r.goods_id AND b.whse_dept=r.whse_dept))")
    ctes.append("display_population AS (SELECT COUNT(*) AS history_display_groups FROM display_keys)")
    ctes.append(f"customer_labels AS (SELECT customer_id,COUNT(*) AS history_customer_master_rows,CASE WHEN COUNT(*)=1 AND MAX(is_delete)='n' AND MAX(is_void)='n' THEN MAX(NULLIF(TRIM(customer_name),'')) ELSE NULL END AS current_customer_name,CASE WHEN COUNT(*)=1 AND MAX(is_delete)='n' AND MAX(is_void)='n' THEN MAX(NULLIF(TRIM(sales_name),'')) ELSE NULL END AS history_current_owner,CASE WHEN COUNT(*)=1 AND MAX(is_delete)='n' AND MAX(is_void)='n' THEN MAX(NULLIF(TRIM(customer_no),'')) ELSE NULL END AS history_customer_no FROM {qt['customers']} WHERE customer_id IN (SELECT customer_id FROM relations) GROUP BY customer_id)")
    ctes.append(f"product_labels AS (SELECT goods_id,COUNT(DISTINCT goods_name) AS history_product_name_variants,CASE WHEN COUNT(DISTINCT goods_name)=1 THEN MAX(goods_name) ELSE NULL END AS goods_name FROM {qt['products']} WHERE goods_id IN (SELECT goods_id FROM pool_products) GROUP BY goods_id)")
    relation_join=' AND '.join('g.'+k+'=b.'+k for k in ['customer_id','goods_id','whse_dept'])
    join=lambda a:' AND '.join('g.'+k+' <=> '+a+'.'+k for k in keys.split(','))
    fields=[]
    for prefix,a,quality in [('outbound','o','oq.history_outbound_unknown_rows'),('return','r','rq.history_returns_unknown_rows')]:
        fields.append(f'COALESCE({a}.n,0) AS history_{prefix}_rows')
        for value,source in [('quantity','qty'),('rolls','rolls')]:
            miss='missing_qty' if source=='qty' else 'missing_rolls'
            fields.extend([f'CASE WHEN g.unit IS NOT NULL THEN COALESCE({a}.{source},0) ELSE NULL END AS history_{prefix}_known_{value}',f'CASE WHEN {quality}=0 AND COALESCE({a}.{miss},0)=0 AND g.unit IS NOT NULL THEN COALESCE({a}.{source},0) ELSE NULL END AS history_{prefix}_{value}',f'COALESCE({a}.{miss},0) AS history_{prefix}_{miss}'])
    fields_sql=','.join(fields)
    complete="oq.history_outbound_unknown_rows=0 AND rq.history_returns_unknown_rows=0 AND COALESCE(o.missing_qty,0)+COALESCE(o.missing_rolls,0)+COALESCE(r.missing_qty,0)+COALESCE(r.missing_rolls,0)=0 AND g.unit IS NOT NULL AND b.history_missing_order_rows=0"
    sql='WITH '+',\n'.join(ctes)+f" SELECT clock.*,meta.*,hc.*,oq.*,rq.*,hp.*,dp.*,g.*,CASE WHEN oq.history_outbound_unknown_rows=0 THEN b.history_first_outbound_at ELSE NULL END AS history_first_outbound_at,CASE WHEN oq.history_outbound_unknown_rows=0 THEN b.history_last_outbound_at ELSE NULL END AS history_last_outbound_at,CASE WHEN oq.history_outbound_unknown_rows=0 AND b.history_missing_order_rows=0 THEN b.history_order_count ELSE NULL END AS history_order_count,b.history_first_outbound_at AS history_known_first_outbound_at,b.history_last_outbound_at AS history_known_last_outbound_at,b.history_order_count AS history_known_order_count,b.history_missing_order_rows,b.history_customer_name_variants,COALESCE(cl.current_customer_name,b.historical_customer_name) AS customer_name,cl.history_current_owner,cl.history_customer_no,COALESCE(cl.history_customer_master_rows,0) AS history_customer_master_rows,CASE WHEN cl.current_customer_name IS NULL OR cl.history_current_owner IS NULL OR cl.history_customer_no IS NULL THEN 1 ELSE 0 END AS history_customer_details_missing,pl.goods_name,pl.history_product_name_variants,{fields_sql},CASE WHEN oq.history_outbound_unknown_rows=0 THEN hp.history_known_relations ELSE NULL END AS history_scope_relations,CASE WHEN oq.history_outbound_unknown_rows=0 THEN hp.history_known_customers ELSE NULL END AS history_scope_customers,CASE WHEN {complete} THEN 'complete' ELSE 'incomplete' END AS metric_data_state,CASE WHEN oq.history_outbound_unknown_rows=0 THEN COALESCE(o.n,0) ELSE NULL END AS metric_value,COALESCE(o.n,0) AS known_subset_value,COALESCE(o.n,0) AS known_value_count,oq.history_outbound_unknown_rows AS missing_value_count,CASE WHEN g.customer_id IS NULL THEN 0 ELSE 1 END AS __matched_row_count FROM clock CROSS JOIN meta CROSS JOIN history_clock hc CROSS JOIN outbound_quality oq CROSS JOIN returns_quality rq CROSS JOIN history_population hp CROSS JOIN display_population dp LEFT JOIN display_keys g ON TRUE LEFT JOIN relations b ON {relation_join} LEFT JOIN outbound_grouped o ON {join('o')} LEFT JOIN returns_grouped r ON {join('r')} LEFT JOIN customer_labels cl ON cl.customer_id=g.customer_id LEFT JOIN product_labels pl ON pl.goods_id=g.goods_id ORDER BY g.customer_id,g.goods_id,g.whse_dept,g.unit LIMIT %s"
    params.append(limit+1)
    return sql,params,{'metric':request['metric'],'dataset':None,'source_datasets':[base['table'],*sources.values()], 'dimension_outputs':['customer_id','customer_name','goods_id','goods_name','whse_dept','unit'],'effective_dimensions':chosen,'filters':filters,'time_range':{'source':TIME_SOURCE},'warnings':[metric['answer_note']]}


def history_observation(rows):
    if not rows:raise AnalysisQueryError('HISTORY_EVIDENCE_MISSING','缺少历史窗口或池证据。')
    for name in ('history_start','history_end','history_outbound_duplicate_ids','history_returns_duplicate_ids','history_known_relations','history_known_customers','history_display_groups','history_outbound_unknown_rows','history_returns_unknown_rows'):
        if any(r.get(name)!=rows[0].get(name) for r in rows):raise AnalysisQueryError('HISTORY_EVIDENCE_INVALID','历史证据不一致。')
    for name in ('history_outbound_duplicate_ids','history_returns_duplicate_ids'):
        if int(rows[0].get(name) or 0)>0:raise AnalysisQueryError('HISTORY_SOURCE_DUPLICATE','出入库明细身份重复，不能保证历史统计不放大。')
    if not any(r.get('__matched_row_count') for r in rows) and int(rows[0].get('history_outbound_unknown_rows') or 0)>0:
        raise AnalysisQueryError('HISTORY_SCOPE_UNASSESSABLE','仅有身份或时间未明的候选，不能把空名单解释为没有历史购买。')
    metadata=validate_frozen_pool_rows(rows)
    try:
        read=datetime.fromisoformat(str(rows[0]['history_end']));start=datetime.fromisoformat(str(rows[0]['history_start']))
        expected=read.replace(year=read.year-1,day=min(read.day,monthrange(read.year-1,read.month)[1]))
        if start!=expected or str(rows[0]['history_end'])!=str(rows[0]['closing_read_at']) or datetime.fromisoformat(str(rows[0]['baseline_frozen_at']))>read:raise ValueError()
        utc=datetime.fromisoformat(str(rows[0]['closing_utc_at']))
        offset=int(rows[0]['observed_clock_offset_seconds'])
        if (read-utc).total_seconds()!=offset or abs(offset)>14*3600 or offset%60:raise ValueError()
    except (ValueError,TypeError,KeyError):raise AnalysisQueryError('HISTORY_EVIDENCE_INVALID','固定12个月窗口或数据库业务时钟证据无效。')
    zone=f"UTC{'+' if offset>=0 else '-'}{abs(offset)//3600:02d}:{abs(offset)%3600//60:02d}"
    return {**metadata,**{k:rows[0].get(k) for k in ('history_scope_relations','history_scope_customers','history_known_relations','history_known_customers','history_display_groups','history_outbound_unknown_rows','history_returns_unknown_rows')},'source':TIME_SOURCE,'history_start':rows[0]['history_start'],'history_end':rows[0]['history_end'],'history_business_timezone_at_read':zone,'end_exclusive':True,'window_anchor':'database_read_clock','matching':'same_product_same_warehouse_department_not_sku'}


def observe_history_result(rows):
    observation = history_observation(rows)
    for row in rows:
        if row.get('__matched_row_count'):
            for role, value in (
                ('customer', row['customer_id']), ('product', row['goods_id']),
                ('relation', (row['customer_id'], row['goods_id'], row['whse_dept'])),
            ):
                row['history_' + role + '_ref'] = role + '_' + hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:16]
    return observation


def public_history_time(value):
    return {k: v for k, v in value.items() if k.startswith('history_') or k in {
        'source', 'baseline_week', 'frozen_at', 'read_at', 'read_utc_at',
        'observed_db_utc_offset_seconds', 'end_exclusive', 'window_anchor', 'matching',
    }}


def describe_history_time(value):
    return [f"既有周基线{value.get('baseline_week')}（冻结{value.get('frozen_at')}）；同产品同仓库部门历史出库客户；固定12个日历月{value.get('history_start')}至{value.get('history_end')}（结束不含），数据库业务时钟UTC偏移{value.get('observed_db_utc_offset_seconds')}秒；全退客户关系保留、退货独立"]
