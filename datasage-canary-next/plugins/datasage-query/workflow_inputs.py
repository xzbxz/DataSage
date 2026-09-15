"""Fixed legacy notification inputs through the existing read-only executor."""
from datetime import datetime
from calendar import monthrange
from collections import defaultdict
from .workflow_io import IOErrorBoundary

def complete(db,sql,params=(),limit=10000):
    rows,cut,_=db.execute(sql,list(params),limit)
    if cut or len(rows)>limit:raise IOErrorBoundary('WORKFLOW_INPUT_TRUNCATED')
    return rows

def customer_mapping(db,pairs):
    pairs=sorted(set(pairs))
    if not pairs:return {'productsByCustomer':{},'customerInfo':{},'wecomBySales':{},'employeeBySales':{}}
    if len(pairs)>5000:raise IOErrorBoundary('CUSTOMER_PAIR_BUDGET_EXCEEDED')
    clock=complete(db,'SELECT NOW(6) AS at',limit=1)[0]['at'];end=datetime.fromisoformat(str(clock))
    start=end.replace(year=end.year-1,day=min(end.day,monthrange(end.year-1,end.month)[1]))
    relations=set()
    for offset in range(0,len(pairs),200):
        batch=pairs[offset:offset+200];relation=' UNION ALL '.join('SELECT %s AS goods_no,%s AS whse_dept' for _ in batch)
        params=[v for pair in batch for v in pair]+[start,end,'bulk']
        sql='SELECT DISTINCT d.customer_id,d.goods_no,d.whse_dept FROM vk_dwd.delivery_bill_barcode_detail_dwd d JOIN ('+relation+") p ON d.goods_no COLLATE utf8mb4_unicode_ci=p.goods_no COLLATE utf8mb4_unicode_ci AND d.whse_dept COLLATE utf8mb4_unicode_ci=p.whse_dept COLLATE utf8mb4_unicode_ci WHERE d.delivery_time>=%s AND d.delivery_time<%s AND (d.whse_dept LIKE '%%-HT' OR d.bill_type=%s) AND d.is_inner_cus='n' LIMIT 10001"
        for r in complete(db,sql,params):
            if r['customer_id'] is None:raise IOErrorBoundary('CUSTOMER_RELATION_ID_MISSING')
            relations.add((str(r['customer_id']),r['goods_no'],r['whse_dept']))
        if len(relations)>10000:raise IOErrorBoundary('CUSTOMER_RELATION_BUDGET_EXCEEDED')
    products=defaultdict(list)
    for cid,g,d in sorted(relations):products[cid].append({'goods_no':g,'whse_dept':d})
    info={};owners=set();ids=list(products)
    for offset in range(0,len(ids),500):
        batch=ids[offset:offset+500]
        sql="SELECT customer_id,customer_no,customer_name,sales_name FROM vk_dwd.customer_dwd WHERE is_delete='n' AND is_void='n' AND customer_id IN ("+','.join('%s' for _ in batch)+') LIMIT 10001'
        for r in complete(db,sql,batch):
            cid=str(r['customer_id']);value={'customer_no':r['customer_no'],'name':r['customer_name'],'sales':r['sales_name']}
            if cid in info and info[cid]!=value:raise IOErrorBoundary('CUSTOMER_MASTER_AMBIGUOUS')
            info[cid]=value
            if r['sales_name']:owners.add(r['sales_name'])
    employees={};names=sorted(owners)
    for offset in range(0,len(names),500):
        batch=names[offset:offset+500]
        sql="SELECT person_name,wecom_account,region,main_dept,position FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_status='payroll' AND COALESCE(wecom_account,'')<>'' AND person_name IN ("+','.join('%s' for _ in batch)+') LIMIT 10001'
        for r in complete(db,sql,batch):
            name=r['person_name']
            if name in employees and (employees[name]['wecom_account'],employees[name]['region'])!=(r['wecom_account'],r['region']):raise IOErrorBoundary('OWNER_ACCOUNT_AMBIGUOUS')
            employees[name]=r
    return {'productsByCustomer':dict(products),'customerInfo':info,'employeeBySales':employees,'wecomBySales':{n:r['wecom_account'] for n,r in employees.items()},'window':{'start':start.isoformat(),'end':end.isoformat()},'meaning':'Legacy notification buyer relationship, not a replacement for the governed historical-customer metric.'}

def report_inputs(handler,region,week,month,*,phase=None):
    """Get current governed result packets, not old unchecked report calculations."""
    import json
    result={}
    if phase not in (None,'weekly','monthly'):raise IOErrorBoundary('REPORT_PHASE_INVALID')
    for name,metric,dimensions,period in (
        ('weekly_pool','registered_slow_pool_baseline_groups',['product','pool_sku','warehouse_department','unit'],{'baseline_week':week}),
        ('weekly_flow','registered_slow_pool_baseline_net_outbound',['product','pool_sku','warehouse_department','unit','salesperson'],{'baseline_week':week}),
        ('monthly_pool','registered_slow_monthly_groups',['product','pool_sku','warehouse_department','unit'],{'calendar_month':month}),
        ('monthly_flow','registered_slow_monthly_net_outbound',['product','pool_sku','warehouse_department','unit','salesperson'],{'calendar_month':month})):
        if phase and not name.startswith(phase):continue
        request={'request_id':name,'domain':'inventory','mode':'metric','metric':metric,'dimensions':dimensions,'metric_filters':{'warehouse_department':region},'limit':1000,**period}
        packet=json.loads(handler({'requests':[request]}))
        if packet.get('status')!='success' or len(packet.get('results',[]))!=1 or packet['results'][0].get('status')!='success' or packet['results'][0].get('truncated'):raise IOErrorBoundary('GOVERNED_REPORT_INCOMPLETE')
        result[name]=packet
    return result

def legacy_report_packet(pool_packet,flow_packet,label_rows,region,period):
    """Map complete governed facts into old Detail columns; never invent missing labels."""
    from . import contracts,capability_contract,legacy_workflow as wf
    _,sem=contracts.execution_contracts('inventory')
    pool_metric='registered_slow_monthly_groups' if len(period)==7 else 'registered_slow_pool_baseline_groups'
    flow_metric='registered_slow_monthly_net_outbound' if len(period)==7 else 'registered_slow_pool_baseline_net_outbound'
    def dim(row,code,metric):
        label=capability_contract.effective_dimension_definitions(sem,metric)[code]['label']
        values=[d.get('value') for d in row.get('dimensions',[]) if d.get('label')==label]
        return values[0] if len(values)==1 else None
    pool=pool_packet['results'][0]['rows'];flow=flow_packet['results'][0]['rows'];labels=defaultdict(set)
    for r in label_rows:
        if r.get('goods_no'):labels[(str(r['goods_sku_id']),r['whse_dept'])].add((r['goods_no'],r.get('attr_val')))
    def unique(field,rows):
        vals=[r.get('facts',{}).get(field) for r in rows]
        if not vals or any(v is None for v in vals):return None
        if len({str(v) for v in vals})!=1:raise IOErrorBoundary('REPORT_SHARED_FACT_CONFLICT')
        return wf.number(vals[0])
    grouped=defaultdict(list);sales={};units={}
    for r in flow:
        grouped[(str(dim(r,'pool_sku',flow_metric)),str(dim(r,'unit',flow_metric)))].append(r)
        f=r['facts'];identity=f.get('sales_identity_ref')
        if identity not in sales:sales[identity]={'sales_name':dim(r,'salesperson',flow_metric) or 'Unknown','net_rolls':f.get('sales_net_rolls')}
        elif wf.number(sales[identity]['net_rolls'])!=wf.number(f.get('sales_net_rolls')):raise IOErrorBoundary('REPORT_SALES_TOTAL_CONFLICT')
        unit=dim(r,'unit',flow_metric)
        if unit is not None:
            if str(unit) in units and wf.number(units[str(unit)])!=wf.number(f.get('unit_net_quantity')):raise IOErrorBoundary('REPORT_UNIT_TOTAL_CONFLICT')
            units[str(unit)]=f.get('unit_net_quantity')
    details=[];open_keys=set();close_keys=set();missing_labels=False
    for r in pool:
        f=r['facts'];sku=str(dim(r,'pool_sku',pool_metric));unit=str(dim(r,'unit',pool_metric));state=r.get('states',{}).get('pool_movement_state','Unassessable')
        options=labels.get((sku,region),set());item,color=next(iter(options)) if len(options)==1 else (None,None)
        if item is None or state=='Unassessable':missing_labels=True
        key=(item,color,region)
        if state!='New':open_keys.add(key)
        if state!='Exited':close_keys.add(key)
        matched=grouped.get((sku,unit),[]);numbers=[wf.number(v['facts'].get('net_rolls')) for v in matched]
        net=None if state=='New' or any(v is None for v in numbers) else sum(numbers,wf.number(0)) if numbers else wf.number(0) if unique('scope_net_rolls',flow) is not None else None
        old=wf.number(f.get('opening_rolls'));new=wf.number(f.get('closing_rolls'));delta=new-old if old is not None and new is not None else None
        names=sorted({str(dim(v,'salesperson',flow_metric) or 'Unknown') for v in matched})
        details.append([item or 'Unknown',color or 'Unknown',region,state,f.get('opening_quantity'),unit,f.get('closing_quantity'),old,new,delta,net,', '.join(names) if state!='New' else None])
    def sum_side(index,excluded):
        values=[wf.number(r[index]) for r in details if r[3]!=excluded]
        return None if any(v is None for v in values) else sum(values,wf.number(0))
    summary={'opening_skus':None if missing_labels else len(open_keys),'closing_skus':None if missing_labels else len(close_keys),'opening_rolls':sum_side(7,'New'),'closing_rolls':sum_side(8,'Exited'),'new':sum(r[3]=='New' for r in details),'exited':sum(r[3]=='Exited' for r in details),'net_outbound_rolls':unique('scope_net_rolls',flow),'high_net_rolls':unique('scope_high_net_rolls',flow),'net_outbound_qty_by_unit':units}
    return {'period':period,'summary':summary,'sales_rows':list(sales.values()),'detail_rows':details,'detail_complete':False,'mapping_note':'Source labels missing/ambiguous remain Unknown; summary scope quantities are not recomputed from truncated lists.'}
