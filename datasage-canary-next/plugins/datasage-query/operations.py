"""Local operator business observations. No scheduling, sending or database writes.

The existing report admission and database executor remain the boundaries. Local
snapshots are reviewed observations, never a substitute for official delivery state.
"""
from pathlib import Path
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib, html, json, os, re
from . import contract_store

class OperationError(ValueError):
    pass

FACT_FIELDS={'idk_null_price_rows','idk_zero_price_rows','idk_negative_price_rows','idk_quantity','idk_known_quantity','idk_rolls','idk_known_rolls','idk_scope_rows','idk_read_at','idk_identity_errors'}

def build_idk_query(request,metric,datasets,semantics,limit,*,observed_on=None):
    from .analytical_queries import AnalysisQueryError,_dataset,_approved,_value_filter
    from .analytical_handlers import validate_parameters
    validate_parameters('idk_unpriced',request)
    chosen=request.get('dimensions') or ['unit'];filters=request.get('metric_filters') or {}
    if chosen!=['unit'] or set(filters)-{'unit'}:raise AnalysisQueryError('UNSUPPORTED_DIMENSION','IDK未定价汇总按源库存单位分列。')
    cfg=policy()['idk'];table=cfg['table'];ds=_dataset(table,datasets)
    for field in ('id','source_unit','goods_num','piece_num','promotion_price','whse_dept','is_whitelist'):_approved(field,ds)
    params=[cfg['department'],cfg['minimum_quantity_exclusive'],cfg['whitelist_value']]
    sql="WITH selected AS (SELECT id,COALESCE(source_unit,'未记录') AS unit,goods_num,piece_num,promotion_price FROM "+_table(table)+' WHERE whse_dept=%s AND goods_num>%s AND is_whitelist=%s AND (promotion_price IS NULL OR promotion_price<=0)), scoped AS (SELECT * FROM selected s'
    if filters:sql+=' WHERE '+_value_filter('s','unit',filters['unit'],params)
    sql+=') SELECT unit,CASE WHEN COUNT(*)=COUNT(DISTINCT id) THEN COUNT(*) ELSE NULL END AS metric_value,COUNT(*)-COUNT(DISTINCT id) AS idk_identity_errors,SUM(CASE WHEN promotion_price IS NULL THEN 1 ELSE 0 END) AS idk_null_price_rows,SUM(CASE WHEN promotion_price=0 THEN 1 ELSE 0 END) AS idk_zero_price_rows,SUM(CASE WHEN promotion_price<0 THEN 1 ELSE 0 END) AS idk_negative_price_rows,CASE WHEN COUNT(*)=COUNT(DISTINCT id) AND COUNT(goods_num)=COUNT(*) THEN SUM(goods_num) ELSE NULL END AS idk_quantity,SUM(goods_num) AS idk_known_quantity,CASE WHEN COUNT(*)=COUNT(DISTINCT id) AND COUNT(piece_num)=COUNT(*) THEN SUM(piece_num) ELSE NULL END AS idk_rolls,SUM(piece_num) AS idk_known_rolls,SUM(COUNT(*)) OVER() AS idk_scope_rows,NOW(6) AS idk_read_at,COUNT(*) AS known_value_count,COUNT(*)-COUNT(piece_num) AS missing_value_count FROM scoped GROUP BY unit ORDER BY unit LIMIT %s'
    params.append(limit+1)
    return sql,params,{'metric':request.get('metric'),'dataset':None,'source_datasets':[table],'dimension_outputs':['unit'],'effective_dimensions':['unit'],'filters':filters,'time_range':{'source':'current_snapshot'},'warnings':[]}

def policy():
    return contract_store.read_yaml('plugins/datasage-query/contracts/operations.yaml')

def _table(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z_]+\.[a-z_]+',value):
        raise OperationError('OPERATION_SOURCE_INVALID')
    return '.'.join('`'+v+'`' for v in value.split('.'))

def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)

def scope_fingerprint(binding):
    """Only this observation's selection/comparison contract invalidates its baseline."""
    cfg=policy();kind=binding['kind']
    side={'idk_unpriced':'idk','sales_prices':'sales','purchase_prices':'purchase'}.get(kind)
    definition={k:v for k,v in cfg.get(side,{}).items() if k!='meaning'} if side else {}
    if side=='sales':definition['organizations']={r:cfg['organizations'][r] for r in binding['regions']}
    return hashlib.sha256(_json({'snapshot_format':1,'binding':binding,'definition':definition}).encode()).hexdigest()

def _number(value):
    if value is None or isinstance(value,bool):return None
    try:
        number=Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:return None

def validate_binding(binding):
    from . import tools
    common={'kind','limit'}
    options={
        'idk_unpriced':{'window_days'},
        'sales_prices':{'regions'},
        'purchase_prices':{'regions'},
        'fabric_review':{'time_range','inventory_scope'},
        'slow_assignment':{'department','baseline_week','max_baseline_age_days','products','include_customer_cards'},
    }
    kind=binding.get('kind')
    if kind not in options or set(binding)-common-options[kind] or type(binding.get('limit')) is not int or not 1<=binding['limit']<=10000:
        raise OperationError('OPERATION_BINDING_INVALID')
    if kind in ('sales_prices','purchase_prices'):
        regions=binding.get('regions')
        if not isinstance(regions,list) or not regions or len(set(regions))!=len(regions) or not set(regions)<=set(policy()['regions']):
            raise OperationError('OPERATION_REGIONS_INVALID')
    if kind=='idk_unpriced' and (type(binding.get('window_days',0)) is not int or not 0<=binding.get('window_days',0)<=366):
        raise OperationError('OPERATION_WINDOW_INVALID')
    if kind=='fabric_review':
        window=binding.get('time_range')
        if not isinstance(window,dict) or set(window)!={'start','end'}:raise OperationError('OPERATION_WINDOW_REQUIRED')
        try:
            if date.fromisoformat(window['start'])>=date.fromisoformat(window['end']):raise ValueError()
        except (ValueError,TypeError):raise OperationError('OPERATION_WINDOW_INVALID')
        if binding.get('inventory_scope') not in ('total','on_hand'):raise OperationError('OPERATION_SCOPE_REQUIRED')
    if kind=='slow_assignment':
        from .local_report import resolve_binding
        basic={k:binding[k] for k in ('department','baseline_week','max_baseline_age_days','limit') if k in binding}
        if basic.get('baseline_week')=='current_iso_week':
            year,week,_=tools._business_today().isocalendar();basic['baseline_week']=f'{year}-W{week:02d}'
        basic['views']=['pool_summary']
        resolve_binding({'version':1,'default_report':'check','reports':{'check':basic}})
        products=binding.get('products')
        if not isinstance(products,list) or not 1<=len(products)<=10 or any(not isinstance(p,str) or not p.strip() for p in products) or len(set(products))!=len(products):
            raise OperationError('OPERATION_PRODUCTS_REQUIRED')
        if type(binding.get('include_customer_cards')) is not bool:raise OperationError('OPERATION_CUSTOMER_SCOPE_REQUIRED')
    return dict(binding)

def build_observation(binding):
    """Fixed SQL producers. No user-supplied SQL, identifiers or recipient fields."""
    binding=validate_binding(binding);cfg=policy();kind=binding['kind'];limit=binding['limit']
    if kind=='idk_unpriced':
        c=cfg['idk'];fields=c['fields']
        if any(not re.fullmatch(r'[a-z_]+',f) for f in fields):raise OperationError('OPERATION_FIELD_INVALID')
        sql='SELECT '+','.join(fields)+",NOW(6) AS observed_at FROM "+_table(c['table'])+' WHERE whse_dept=%s AND goods_num>%s AND is_whitelist=%s AND (promotion_price IS NULL OR promotion_price<=0)'
        params=[c['department'],c['minimum_quantity_exclusive'],c['whitelist_value']]
        if binding.get('window_days'):
            sql+=' AND gmt_create>=DATE_SUB(NOW(6),INTERVAL %s DAY)';params.append(binding['window_days'])
        return sql+' ORDER BY id LIMIT %s',params+[limit+1]
    side='sales' if kind=='sales_prices' else 'purchase' if kind=='purchase_prices' else None
    if side is None:raise OperationError('OPERATION_NOT_SOURCE_OBSERVATION')
    c=cfg[side];s=c['sources'];regions=binding['regions'];marks=','.join(['%s']*len(regions));params=[]
    # A missing/deleted promotion flag is observable. Preserve the documented
    # legacy membership policy; do not invent a new deletion interpretation.
    if side=='sales':
        region_cte=' UNION ALL '.join('SELECT %s AS dept,%s AS org' for _ in regions)
        for region in regions:params.extend([region,cfg['organizations'][region]])
        sql='WITH regions AS ('+region_cte+'), pool AS ('
        sql+='SELECT DISTINCT r.goods_id,r.goods_no,r.dept FROM '+_table(s['ready'])+' r JOIN regions z ON r.dept=z.dept WHERE r.type<>%s'
        params.append(c['cancelled_ready_type'])
        sql+=' UNION SELECT DISTINCT p.goods_id,g.goods_no,g.promotion_area FROM '+_table(s['promotion'])+' g JOIN regions z ON g.promotion_area=z.dept JOIN '+_table(s['prices'])+" p ON p.goods_no=g.goods_no AND p.is_void='n' AND p.structure_name IN (z.dept,z.org) WHERE CURRENT_DATE()>=g.promotion_start_time AND CURRENT_DATE()<=COALESCE(g.promotion_end_time,g.default_end_time)), candidates AS ("
        sql+='SELECT x.goods_id,x.goods_no,x.dept,p.goods_name,p.customer_grade,p.color_label,p.ddp_price,p.currency_no,p.unit,p.unit_cuur,p.is_inclue_tax,p.effective_date,p.expiration_date,p.gmt_modified,p.detail_id,'
        sql+='DENSE_RANK() OVER(PARTITION BY x.goods_id,x.dept,p.customer_grade,p.color_label ORDER BY CASE WHEN p.structure_name=x.dept THEN 0 ELSE 1 END,p.gmt_modified DESC) AS rank_in_key '
        sql+='FROM pool x JOIN regions z ON x.dept=z.dept LEFT JOIN '+_table(s['prices'])+" p ON p.goods_id=x.goods_id AND p.is_void='n' AND p.structure_name IN (z.dept,z.org)) SELECT goods_id,goods_no,dept,goods_name,customer_grade,color_label,ddp_price,currency_no,unit,unit_cuur,is_inclue_tax,effective_date,expiration_date,gmt_modified,detail_id,NOW(6) AS observed_at FROM candidates WHERE rank_in_key=1 ORDER BY goods_id,dept,customer_grade,color_label,detail_id LIMIT %s"
    else:
        sql='WITH pool AS (SELECT DISTINCT goods_no FROM '+_table(s['ready'])+' WHERE dept IN ('+marks+') AND type<>%s UNION SELECT DISTINCT goods_no FROM '+_table(s['promotion'])+' WHERE promotion_area IN ('+marks+') AND CURRENT_DATE()>=promotion_start_time AND CURRENT_DATE()<=COALESCE(promotion_end_time,default_end_time)), candidates AS ('
        params=[*regions,c['cancelled_ready_type'],*regions]
        sql+='SELECT x.goods_no,p.goods_name,p.color_label,p.supplier_no,p.supplier_name,p.tax_inclue_price,p.tax_exclue_price,p.currency_no,p.unit_cuur,p.effective_date,p.expiration_date,p.gmt_modified,p.detail_id,'
        sql+='DENSE_RANK() OVER(PARTITION BY x.goods_no,p.color_label,p.supplier_no ORDER BY p.gmt_modified DESC) AS rank_in_key FROM pool x LEFT JOIN '+_table(s['prices'])+' p ON p.goods_no=x.goods_no AND p.parent_org_ids=%s) SELECT goods_no,goods_name,color_label,supplier_no,supplier_name,tax_inclue_price,tax_exclue_price,currency_no,unit_cuur,effective_date,expiration_date,gmt_modified,detail_id,NOW(6) AS observed_at FROM candidates WHERE rank_in_key=1 ORDER BY goods_no,color_label,supplier_no,detail_id LIMIT %s'
        params.append(c['parent_org_ids'])
    return sql,params+[limit+1]

def classify_prices(side,rows,observed_on):
    """Only explicit fields enter local snapshots; exact decimals, no NULL-to-zero."""
    c=policy()[side];grouped=defaultdict(list)
    observation=observed_on if isinstance(observed_on,datetime) else datetime.combine(observed_on,datetime.min.time())
    for row in rows:
        key=tuple(row.get(k) for k in c['key']);grouped[key].append(row)
    result=[]
    for key,items in grouped.items():
        row=items[0];state='comparable'
        required=['goods_id','dept'] if side=='sales' else ['goods_no','supplier_no']
        if len(items)>1:state='ambiguous_latest'
        elif any(row.get(k) in (None,'') for k in required) or row.get('detail_id') is None:state='missing_identity_or_quote'
        elif any(row.get(k) in (None,'') for k in c['basis']):state='unknown_basis'
        elif any(_number(row.get(k)) is None for k in c['prices']):state='missing_price'
        elif any(_number(row.get(k))<0 for k in c['prices']):state='negative_price'
        else:
            try:
                start=datetime.fromisoformat(str(row['effective_date']));end=datetime.fromisoformat(str(row['expiration_date'])) if row.get('expiration_date') is not None else None
                if end and end<start:state='invalid_validity'
                elif start>observation:state='not_yet_effective'
                elif end and end<observation:state='expired'
                elif end and end==observation:state='validity_boundary'
            except (KeyError,ValueError,TypeError):state='unknown_validity'
        validity_state=state if state in ('invalid_validity','unknown_validity','not_yet_effective','expired','validity_boundary') else 'within_recorded_bounds' if state=='comparable' else 'not_assessed'
        if side=='purchase' and state=='unknown_validity':state='recorded_quote_only'
        record={'key':list(key),'state':state,'validity_state':validity_state,'candidate_rows':len(items),'prices':{k:str(_number(row[k])) if _number(row.get(k)) is not None else None for k in c['prices']},'basis':{k:row.get(k) for k in c['basis']},'validity':{k:str(row[k]) if row.get(k) is not None else None for k in ('effective_date','expiration_date')},'labels':{k:row.get(k) for k in ('goods_no','goods_name','supplier_name') if k in row}}
        # An ambiguous source is not made precise by whichever SQL row arrived first.
        if len(items)>1:record.update(prices={},basis={},validity={},labels={})
        result.append(record)
    return sorted(result,key=lambda r:_json(r['key']))

def compare(previous,current):
    def indexed(rows):
        result={}
        for row in rows:
            key=_json(row['key'])
            if key in result:raise OperationError('SNAPSHOT_DUPLICATE_KEY')
            result[key]=row
        return result
    old=indexed(previous or []);new=indexed(current);events=[]
    for key in sorted(old.keys()|new.keys()):
        before=old.get(key);after=new.get(key)
        if after is None:kind='absent_from_selection'
        elif after['state'] not in ('comparable','recorded_quote_only'):kind='unresolved'
        elif before is None:kind='new_baseline_candidate'
        elif before['state'] not in ('comparable','recorded_quote_only'):kind='recovered_baseline_candidate'
        elif before['basis']!=after['basis']:kind='basis_changed'
        elif before['validity']!=after['validity']:kind='validity_changed'
        elif any(_number(before['prices'].get(f))!=_number(v) for f,v in after['prices'].items()):kind='recorded_quote_changed' if 'recorded_quote_only' in (before['state'],after['state']) else 'price_changed'
        else:kind='unchanged'
        events.append({'key':json.loads(key),'event':kind,'before':before,'after':after})
    return events

def classify_idk(rows):
    result=[]
    for row in rows:
        price=_number(row.get('promotion_price'))
        state='null' if row.get('promotion_price') is None else 'invalid' if price is None else 'zero' if price==0 else 'negative' if price<0 else 'priced'
        result.append({'source_ref':row.get('id'),'product':row.get('goods_no'),'product_name':row.get('goods_name'),'sku_ref':row.get('goods_sku_id'),'color':row.get('attr_val'),'unit':row.get('source_unit'),'quantity':row.get('goods_num'),'rolls':row.get('piece_num'),'price_state':state,'source_created_at':row.get('gmt_create'),'source_modified_at':row.get('gmt_modified')})
    return result

def _store_root(profile,report_id):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',report_id):raise OperationError('REPORT_ID_INVALID')
    root=profile/'report_runs'/'operations'/report_id
    for p in (profile/'report_runs',profile/'report_runs'/'operations',root):
        if p.is_symlink() or not p.resolve().is_relative_to(profile.resolve()):raise OperationError('REPORT_PATH_INVALID')
    return root

def _atomic(path,value):
    import uuid
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x',encoding='utf-8') as f:f.write(_json(value))
    temp.replace(path)

def load_baseline(profile,report_id,scope_hash):
    root=_store_root(profile,report_id);pointer=root/'accepted.json'
    if not pointer.exists():return None
    if pointer.is_symlink():raise OperationError('SNAPSHOT_PATH_INVALID')
    data=json.loads(pointer.read_text(encoding='utf-8'))
    if data.get('scope_hash')!=scope_hash:raise OperationError('SNAPSHOT_SCOPE_CHANGED')
    return data

def save_observation(profile,report_id,document):
    root=_store_root(profile,report_id);root.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(_json(document).encode()).hexdigest();path=root/(digest+'.json')
    if path.is_symlink():raise OperationError('REPORT_PATH_INVALID')
    if not path.exists():_atomic(path,document)
    return path

def accept_snapshot(profile,report_id,digest,expected_scope=None):
    """Explicit local operator action; no database state, scheduling or sending."""
    if not re.fullmatch(r'[0-9a-f]{64}',digest):raise OperationError('SNAPSHOT_ID_INVALID')
    root=_store_root(profile,report_id);path=root/(digest+'.json')
    if path.is_symlink():raise OperationError('SNAPSHOT_PATH_INVALID')
    document=json.loads(path.read_text(encoding='utf-8'))
    if hashlib.sha256(_json(document).encode()).hexdigest()!=digest:raise OperationError('SNAPSHOT_DIGEST_INVALID')
    if document.get('status')!='success' or document.get('kind') not in ('sales_prices','purchase_prices','idk_unpriced'):raise OperationError('SNAPSHOT_NOT_ACCEPTABLE')
    if expected_scope is not None and document.get('scope_hash')!=expected_scope:raise OperationError('SNAPSHOT_SCOPE_CHANGED')
    lock=root/'accept.lock'
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise OperationError('SNAPSHOT_ACCEPT_BUSY')
    try:
        os.close(fd);old=load_baseline(profile,report_id,document['scope_hash'])
        if old and old.get('observation_id')==digest:return
        if (old or {}).get('observation_id')!=document.get('baseline_id'):raise OperationError('SNAPSHOT_BASELINE_ADVANCED')
        _atomic(root/'accepted.json',{'observation_id':digest,'scope_hash':document['scope_hash'],'records':document['records']})
    finally:lock.unlink()

def render(document):
    def escape(v):return html.escape('未知' if v is None else str(v))
    labels=policy()['field_labels'];events=policy()['event_labels']
    def table(rows):
        if not rows:return '<p>该部分没有返回行。</p>'
        fields=list(dict.fromkeys(k for row in rows for k in row))
        return '<table><tr>'+''.join('<th>'+escape(labels.get(k,k))+'</th>' for k in fields)+'</tr>'+''.join('<tr>'+''.join('<td>'+escape(row.get(k))+'</td>' for k in fields)+'</tr>' for row in rows)+'</table>'
    body='<h1>业务观察与覆盖</h1><p>本地生成成功不等于投递成功。本产物未发送。未知值不是零，范围变化不是价格变化或因果证明。</p>'
    if document.get('synthetic'):body='<p><strong>合成示例，不是真实业务数据</strong></p>'+body
    body+='<p>'+escape(document.get('meaning',document.get('coverage','')))+' </p>'
    if document.get('observed_at'):body+='<p>库端观察时点：'+escape(document['observed_at'])+'</p>'
    records=[]
    for r in document.get('records',[]):
        if 'price_state' in r:records.append({k:v for k,v in r.items() if k not in ('source_ref','sku_ref')})
        else:records.append({**r.get('labels',{}),'状态':r['state'],'有效性':r.get('validity_state'),**r['prices'],**r['basis'],**r['validity'],'候选记录数':r['candidate_rows']})
    body+=table(records) if 'records' in document else ''
    if document.get('events'):
        body+='<h2>差异状态</h2>'+table([{'状态':events.get(e['event'],e['event']),'数量':n} for e,n in (({'event':k},v) for k,v in document.get('event_counts',{}).items())])
        for event in document['events']:
            if event['event'] not in ('price_changed','recorded_quote_changed','basis_changed','validity_changed'):continue
            body+='<h3>'+escape(events[event['event']])+'</h3>'+table([{'观察':'之前',**(event['before'] or {}).get('labels',{}),**(event['before'] or {}).get('prices',{}),**(event['before'] or {}).get('basis',{})},{'观察':'本次',**(event['after'] or {}).get('labels',{}),**(event['after'] or {}).get('prices',{}),**(event['after'] or {}).get('basis',{})}])
    for packet in document.get('query_packets',[]):
        body+='<h2>'+escape(packet.get('answer_scope_line','受控查询结果'))+'</h2>'
        for result in packet.get('results',[]):
            body+='<p>仅返回部分行：'+escape(result.get('truncated'))+'</p>'
            for row in result.get('rows',[]):
                title='；'.join(str(d.get('label'))+'：'+str(d.get('value')) for d in row.get('dimensions',[]))
                body+='<h3>'+escape(title)+'</h3>'+table([row.get('facts',{})])
        for disclosure in packet.get('disclosures',[]):body+='<p>'+escape(disclosure.get('text'))+'</p>'
    return '<!doctype html><meta charset="utf-8"><title>DataSage 业务观察</title><style>body{font:15px system-ui;margin:32px;color:#182331}table{border-collapse:collapse;display:block;overflow:auto;margin:18px 0}td,th{border:1px solid #ccd5df;padding:8px;white-space:nowrap}th{background:#edf3f8}h3{margin-top:28px}</style>'+body

def execute(profile,report_id,binding):
    from . import tools
    from .local_report import _assert_local_context
    _assert_local_context();binding=validate_binding(binding)
    scope_hash=scope_fingerprint(binding)
    previous=load_baseline(profile,report_id,scope_hash)
    if binding['kind'] in ('fabric_review','slow_assignment'):
        return execute_governed(profile,report_id,binding,scope_hash)
    sql,params=build_observation(binding)
    rows,truncated,_source=tools._execute_with_source(sql,params,binding['limit'])
    if truncated or len(rows)>binding['limit']:raise OperationError('OBSERVATION_TRUNCATED_NO_BASELINE_ADVANCE')
    observed=str(rows[0]['observed_at']) if rows else None
    if observed is None:
        clock,_,_=tools._execute_with_source('SELECT NOW(6) AS observed_at',[],1)
        observed=str(clock[0]['observed_at'])
    kind=binding['kind']
    if kind=='idk_unpriced':
        records=classify_idk(rows)
        if any(r['source_ref'] is None for r in records) or len({_json(r['source_ref']) for r in records})!=len(records):raise OperationError('IDK_SOURCE_IDENTITY_INVALID')
        events=[];old={_json(r['source_ref']):r for r in (previous or {}).get('records',[])}
        for r in records:events.append({'source_ref':r['source_ref'],'event':'new_candidate' if _json(r['source_ref']) not in old else 'still_unpriced'})
        present={_json(r['source_ref']) for r in records}
        events.extend({'source_ref':r['source_ref'],'event':'absent_from_selection'} for k,r in old.items() if k not in present)
    else:
        records=classify_prices('sales' if kind=='sales_prices' else 'purchase',rows,datetime.fromisoformat(observed));events=compare((previous or {}).get('records'),records)
    doc={'status':'success','kind':kind,'scope_hash':scope_hash,'baseline_id':(previous or {}).get('observation_id'),'observed_at':observed,'observation_clock':'database local clock; not claimed UTC','source_rows':len(rows),'records':records,'events':events,'event_counts':dict(Counter(e['event'] for e in events)),'delivery_state':'not_requested','baseline_state':'requires_explicit_accept','meaning':policy()['idk' if kind=='idk_unpriced' else 'sales' if kind=='sales_prices' else 'purchase']['meaning']}
    return doc

def execute_governed(profile,report_id,binding,scope_hash):
    from . import wire, tools
    from .local_report import execute_report
    kind=binding['kind'];requests=[]
    if kind=='slow_assignment':
        binding=dict(binding)
        if binding['baseline_week']=='current_iso_week':
            year,week,_=tools._business_today().isocalendar();binding['baseline_week']=f'{year}-W{week:02d}'
        basic={k:binding[k] for k in ('department','baseline_week','max_baseline_age_days','limit')};basic['views']=['pool_summary']
        checked=execute_report({'version':1,'default_report':'check','reports':{'check':basic}})
        if checked['status']!='success':raise OperationError('BASELINE_REPORT_NOT_READY')
        for product in binding['products']:
            for customer in ([False,True] if binding['include_customer_cards'] else [False]):
                requests.append({'request_id':str(len(requests)),'domain':'inventory','mode':'metric','metric':'registered_slow_historical_customers' if customer else 'registered_slow_pool_baseline_groups','dimensions':['customer','product','warehouse_department','unit'] if customer else ['product','pool_sku','warehouse_department','unit'],'metric_filters':{'product':product,'warehouse_department':binding['department']},'baseline_week':binding['baseline_week'],'limit':min(binding['limit'],100)})
    else:
        for domain,metric in [('delivery','fabric_delivery_source_summary'),('inventory','fabric_inventory_source_summary')]:
            for dims in ([],['fabric_channel'],['fabric_formation']):
                request={'request_id':str(len(requests)),'domain':domain,'mode':'metric','metric':metric,'dimensions':dims,'limit':min(binding['limit'],100)}
                if domain=='delivery':request['time_range']=binding['time_range']
                else:request['inventory_scope']=binding['inventory_scope']
                requests.append(request)
    handler=wire.bounded_json_handler('datasage_query',tools.runtime_guarded_datasage_query)
    packets=[]
    # Respect existing public per-call budgets; no identity spoofing, new engine,
    # result manufacture, or merge of observations into a fake atomic snapshot.
    for request in requests:
        packet=json.loads(handler({'requests':[request]}));packets.append(packet)
        results=packet.get('results')
        valid=(packet.get('status')=='success' and isinstance(results,list) and len(results)==1 and results[0].get('request_id')==request['request_id'] and results[0].get('status')=='success')
        if not valid:break
    ok=len(packets)==len(requests) and all(p.get('status')=='success' and isinstance(p.get('results'),list) and len(p['results'])==1 and p['results'][0].get('status')=='success' and p['results'][0].get('request_id')==r['request_id'] for p,r in zip(packets,requests))
    return {'status':'success' if ok else 'partial','kind':kind,'scope_hash':scope_hash,'query_packets':packets,'delivery_state':'not_requested','assignment_state':'review_only_no_recipient_or_responsibility_inference' if kind=='slow_assignment' else None,'coverage':'Each packet retains its own time and truncation. Visible rows are not the complete population.','unmigrated':'Formal responsibility attribution, unverified upstream quality/return labels, image delivery and automatic recipient mapping are not asserted.'}
