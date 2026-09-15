"""Review-only legacy workflows: pure planning and real local attachments.

No source connection, message sender, cron registration or accepted-price write.
The only write simulation accepts an in-memory sqlite3 connection, never MySQL.
"""
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date,datetime,time,timedelta,timezone
from collections import defaultdict
import hashlib,json,re,sqlite3,zipfile
from . import contract_store
from .legacy_xlsx import gen_workbook_xlsx

class WorkflowError(ValueError):pass

def policy():return contract_store.read_yaml('plugins/datasage-query/contracts/legacy-workflows.json')
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def account_token(value):return hashlib.sha256(str(value).strip().encode()).hexdigest()
def number(value):
    if value is None or isinstance(value,bool):return None
    try:
        n=Decimal(str(value));return n if n.is_finite() else None
    except InvalidOperation:return None
def show(value):return 'Unknown' if value is None else str(value)
def safe_name(value):return re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',str(value)).strip(' .')[:110] or 'Unknown'

def legacy_periods(now):
    if now.tzinfo is None:raise WorkflowError('CLOCK_TIMEZONE_REQUIRED')
    local=now.astimezone(timezone(timedelta(hours=8)));year,week,_=local.isocalendar()
    monday=date.fromisocalendar(year,week,1)
    return {'week':f'{year}-W{week:02d}','calendar_month':local.strftime('%Y-%m'),'planned_start':datetime.combine(monday,time(9),tzinfo=local.tzinfo).isoformat(),'planned_end':datetime.combine(monday+timedelta(days=5),time(19),tzinfo=local.tzinfo).isoformat(),'note':'Calendar labels only; recorded-flow execution keeps its actual frozen_at/read_at bounds.'}

BASELINE_COLUMNS=('week_label','source_row_id','goods_id','goods_sku_id','goods_no','goods_name','attr_val','color_label','whse_dept','unit','source_unit','promotion_unit','total_qty','total_piece','slow_label','currency_no','ddp_price','promotion_price','promotion_currency_no','remarks','source_creator_id','source_created_at','source_modifier_id','source_modified_at','source_table','baseline_version')

def source_query_specs(week):
    """Fixed read-only inputs for the future approved operator run; not executed here."""
    try:
        if not re.fullmatch(r'\d{4}-W\d{2}',week):raise ValueError()
        date.fromisocalendar(int(week[:4]),int(week[6:]),1)
    except (TypeError,ValueError):raise WorkflowError('INVALID_WEEK')
    cfg=policy();depts=list(dict.fromkeys(d for r in cfg['regions'].values() for d in r['departments']))
    sales_depts=list(dict.fromkeys(d for r in cfg['regions'].values() for d in r['task_sales_departments']))
    return [
        {'role':'freeze_source','sql':'SELECT id,goods_id,goods_sku_id,goods_no,goods_name,attr_val,color_label,whse_dept,source_unit,unit,goods_num,piece_num,slow_label,currency_no,ddp_price,promotion_price,promotion_currency_no,remarks,creator_id,gmt_create,modifier_id,gmt_modified,is_whitelist FROM vk_ods.slow_moving_goods_ods WHERE goods_num>%s AND is_whitelist=%s AND whse_dept IN ('+','.join('%s' for _ in depts)+') ORDER BY id LIMIT 10001','params':[cfg['freeze']['threshold_exclusive'],cfg['freeze']['whitelist'],*depts]},
        {'role':'existing_week','sql':'SELECT '+','.join(BASELINE_COLUMNS)+',frozen_at FROM vk_ai.slow_moving_baseline WHERE week_label=%s ORDER BY source_row_id LIMIT 10001','params':[week]},
        {'role':'dynamic_recipients','sql':"SELECT region,main_dept,person_name,wecom_account,position,is_delete,wecom_status FROM vk_dwd.employee_dwd WHERE is_delete='n' AND wecom_status='payroll' AND COALESCE(wecom_account,'')<>'' AND region IN ("+','.join('%s' for _ in cfg['regions'])+') AND main_dept IN ('+','.join('%s' for _ in sales_depts)+') ORDER BY region,wecom_account LIMIT 10001','params':[*cfg['regions'],*sales_depts]},
    ]

def collect_readonly_inputs(snapshot_executor,week):
    """Use the existing operator's consistent-snapshot executor, never a new connector."""
    result={}
    for spec in source_query_specs(week):
        rows,truncated,_=snapshot_executor.execute(spec['sql'],spec['params'],10000)
        if truncated or len(rows)>10000:raise WorkflowError('WORKFLOW_SOURCE_INCOMPLETE')
        result[{'freeze_source':'source_rows','existing_week':'existing_rows','dynamic_recipients':'employees'}[spec['role']]]=rows
    return result

def freeze_plan(source_rows,existing_rows,week,current_week,*,refreeze=True):
    for value in (week,current_week):
        try:
            if not re.fullmatch(r'\d{4}-W\d{2}',value):raise ValueError()
            date.fromisocalendar(int(value[:4]),int(value[6:]),1)
        except (TypeError,ValueError):raise WorkflowError('INVALID_WEEK')
    if type(refreeze) is not bool:raise WorkflowError('INVALID_REFREEZE')
    cfg=policy()['freeze'];regions=policy()['regions'];depts={d for r in regions.values() for d in r['departments']}
    plan={'kind':'weekly_freeze_dry_run','week':week,'current_week':current_week,'source':cfg['source_table'],'target':cfg['target_table'],'executed':False,'existing_rows_in_week':sum(r.get('week_label')==week for r in existing_rows),'issues':[],'insert_rows':[],'legacy_write_contract':{'lock_name':'datasage_slow_baseline_'+week,'lock_wait_seconds':10,'transaction':['delete_requested_week','bulk_insert_v2_rows','verify_row_count','verify_v2_source_identity','verify_single_frozen_at','commit_or_rollback'],'release_lock_in_finally':True,'live_execution_enabled':False}}
    if week!=current_week:plan['issues'].append('NON_CURRENT_WEEK_REQUIRES_SEPARATE_BACKFILL_REVIEW')
    current=[r for r in existing_rows if r.get('week_label')==week]
    if current and not refreeze:
        ids=[r.get('source_row_id') for r in current]
        if None in ids or len(set(ids))!=len(ids) or any(r.get('baseline_version')!=2 for r in current):plan['issues'].append('EXISTING_BASELINE_INVALID')
        plan.update(action='reuse_existing',status='blocked' if plan['issues'] else 'ready')
        return plan
    seen=set()
    for source in source_rows:
        if source.get('whse_dept') not in depts or source.get('is_whitelist')!=cfg['whitelist']:continue
        qty=number(source.get('goods_num'));rolls=number(source.get('piece_num'))
        if qty is None:plan['issues'].append('UNKNOWN_QUANTITY');continue
        if qty<=cfg['threshold_exclusive']:continue
        identity=source.get('id');unit=source.get('source_unit')
        if identity is None or identity in seen:plan['issues'].append('DUPLICATE_OR_MISSING_SOURCE_ID');continue
        seen.add(identity)
        if unit not in ('m','M','y','Y','kg','Pcs'):plan['issues'].append('SOURCE_UNIT_MISSING_OR_UNKNOWN_NO_PROMOTION_FALLBACK');continue
        if rolls is None or rolls<0:plan['issues'].append('ROLLS_MISSING_OR_NEGATIVE_NO_ZERO_FALLBACK');continue
        if any(source.get(k) in (None,'') for k in ('goods_id','goods_sku_id','goods_no')):plan['issues'].append('MISSING_PRODUCT_IDENTITY');continue
        row={k:source.get(k) for k in BASELINE_COLUMNS}
        row.update(week_label=week,source_row_id=identity,source_unit=unit,promotion_unit=source.get('unit'),unit=unit.upper() if unit.lower() in ('m','y') else unit.lower(),total_qty=str(qty),total_piece=str(rolls),source_table=cfg['source_table'],baseline_version=2,source_creator_id=source.get('creator_id'),source_created_at=source.get('gmt_create'),source_modifier_id=source.get('modifier_id'),source_modified_at=source.get('gmt_modified'))
        plan['insert_rows'].append(row)
    if not plan['insert_rows']:plan['issues'].append('EMPTY_SOURCE_KEEP_EXISTING')
    plan.update(action='replace_requested_week' if current else 'create_requested_week',status='blocked' if plan['issues'] else 'ready',delete_scope={'week_label':week},insert_count=len(plan['insert_rows']))
    plan['issues']=sorted(set(plan['issues']));plan['plan_digest']=digest(plan)
    return plan

def simulate_freeze(connection,plan,*,fail_after_delete=False):
    if type(connection) is not sqlite3.Connection or any(r[2] for r in connection.execute('PRAGMA database_list')):raise WorkflowError('IN_MEMORY_SIMULATION_ONLY')
    if plan.get('status')!='ready':raise WorkflowError('PLAN_BLOCKED')
    if plan.get('action')=='reuse_existing':return {'written':0,'simulation_only':True}
    if 'frozen_at' not in {r[1] for r in connection.execute('PRAGMA vk_ai.table_info(slow_moving_baseline)')}:raise WorkflowError('SIMULATION_SCHEMA_MISSING_FROZEN_AT')
    connection.execute('BEGIN')
    try:
        connection.execute('DELETE FROM vk_ai.slow_moving_baseline WHERE week_label=?',(plan['week'],))
        if fail_after_delete:raise WorkflowError('SYNTHETIC_FAILURE')
        sql='INSERT INTO vk_ai.slow_moving_baseline ('+','.join(BASELINE_COLUMNS)+') VALUES ('+','.join('?' for _ in BASELINE_COLUMNS)+')'
        for row in plan['insert_rows']:
            if row['week_label']!=plan['week']:raise WorkflowError('PLAN_WEEK_MISMATCH')
            connection.execute(sql,tuple(row[k] for k in BASELINE_COLUMNS))
        count=connection.execute('SELECT COUNT(*) FROM vk_ai.slow_moving_baseline WHERE week_label=?',(plan['week'],)).fetchone()[0]
        if count!=plan['insert_count']:raise WorkflowError('COUNT_MISMATCH')
        clocks=connection.execute('SELECT COUNT(DISTINCT frozen_at),COUNT(frozen_at) FROM vk_ai.slow_moving_baseline WHERE week_label=?',(plan['week'],)).fetchone()
        if tuple(clocks)!=(1,count):raise WorkflowError('FREEZE_TIMESTAMP_MISMATCH')
        connection.commit();return {'written':count,'simulation_only':True}
    except Exception:connection.rollback();raise

def dynamic_sales_by_region(region_map,employees,regions):
    result={};account_regions={}
    for region in regions:
        c=region_map[region];allowed=set(c.get('dynamic_sales_departments',policy()['regions'][region]['task_sales_departments']))
        active=[]
        for e in employees:
            if str(e.get('region') or '').strip()!=region or str(e.get('main_dept') or '').strip() not in allowed or e.get('is_delete')!='n' or e.get('wecom_status')!='payroll':continue
            account=str(e.get('wecom_account') or '').strip()
            if not account:continue
            if account in account_regions and account_regions[account]!=region:raise WorkflowError('ACCOUNT_IN_MULTIPLE_REGIONS')
            if account in account_regions:continue
            account_regions[account]=region;active.append({'account':account,'name':e.get('person_name') or account})
        result[region]=active
    return result

def task_recipients(region_map,employees,regions):
    """Legacy active sales + configured executors/managers; no live lookup here."""
    result={};dynamic=dynamic_sales_by_region(region_map,employees,regions)
    for region in regions:
        c=region_map[region];active=dynamic[region]
        if not active:raise WorkflowError('DYNAMIC_SALES_RECIPIENTS_MISSING:'+region)
        merged={}
        for role,items in [('executor',c.get('executors',[])),('sales',active),('manager',[{'account':a,'name':a} for a in c.get('managers',[])])]:
            for item in items:
                account=str(item.get('account') or '').strip()
                if not account:continue
                r=merged.setdefault(account,{'account':account,'name':item.get('name') or account,'roles':[]})
                if role not in r['roles']:r['roles'].append(role)
        result[region]=[merged[k] for k in sorted(merged)]
    return result

def recipient_plan_decision(week,current_targets,existing=None,*,preview=True):
    if preview:return {'action':'recompute_for_preview','regions':current_targets}
    if existing is None:return {'action':'would_persist_new_week_plan','regions':current_targets}
    if existing.get('version')!=2 or existing.get('week')!=week or not isinstance(existing.get('regions'),dict):raise WorkflowError('EXISTING_RECIPIENT_PLAN_INVALID')
    return {'action':'reuse_frozen_week_plan','regions':existing['regions']}

def customer_plan_decision(week,baseline_digest,existing=None,*,rebuild=False,preview=True):
    if rebuild:return 'rebuild_for_refreeze'
    if preview or existing is None:return 'build_for_preview' if preview else 'would_persist_new_plan'
    if existing.get('version')!=4 or existing.get('week')!=week or existing.get('baseline_digest')!=baseline_digest:raise WorkflowError('EXISTING_CUSTOMER_PLAN_MISMATCH')
    return 'reuse_frozen_customer_plan'

def price_recipients(region_map,employees,regions,fixed_managers):
    dynamic=dynamic_sales_by_region(region_map,employees,regions);result={}
    keywords=('销售经理','销售主管','Sales Manager','Sales Supervisor')
    for region in regions:
        executors={r['account'] for r in region_map[region].get('executors',[])}
        managers=set(fixed_managers)
        managers.update(e['wecom_account'] for e in employees if e.get('is_delete')=='n' and e.get('wecom_account') in executors and any(k in str(e.get('position') or '') for k in keywords))
        result[region]={'sales':dynamic[region],'managers':sorted(managers),'manager_attachment_requires_regional_buyers':True}
    return result

def report_recipients(region_map,region):
    return sorted({e['account'] for e in region_map[region].get('executors',[])}|set(region_map[region].get('managers',[])))

def delivery_preview(mode,period,region,account,components,receipts=(),*,force=False):
    """Project official receipt evidence into intended actions; no sending or state writes."""
    if type(force) is not bool:raise WorkflowError('INVALID_FORCE_RESEND')
    result=[]
    for component in components:
        key=digest([mode,period,region,account,component])
        matches=[r for r in receipts if r.get('component_key')==key]
        latest=matches[-1].get('status') if matches else 'not_attempted'
        if len({r.get('status') for r in matches})>1:
            if all(type(r.get('sequence')) is int for r in matches) and len({r['sequence'] for r in matches})==len(matches):latest=max(matches,key=lambda r:r['sequence']).get('status')
            else:latest='unknown'
        if latest in ('unknown','in_flight') or latest not in ('provider_accepted','failed','not_attempted'):action='hold_for_review'
        elif latest=='provider_accepted' and not force:action='skip_confirmed_component'
        else:action='would_send'
        result.append({'component_key':key,'component':component,'prior_status':latest,'proposed_action':action,'executed':False,'human_received':'unknown'})
    return result

def legacy_progress_receipts(document,mode,period,region,accounts):
    """Interpret provided V2 component markers; never infer success from log 'ok'."""
    if document.get('version')!=2 or any(document.get(k)!=v for k,v in [('mode',mode),('week',period),('region',region)]):raise WorkflowError('LEGACY_PROGRESS_SCOPE_INVALID')
    tokens=document.get('completed_recipient_tokens')
    if not isinstance(tokens,list) or any(not isinstance(t,str) or not re.fullmatch(r'[0-9a-f]{64}',t) for t in tokens):raise WorkflowError('LEGACY_PROGRESS_CORRUPT')
    return [{'component_key':digest([mode,period,region,a,c]),'status':'provider_accepted','evidence':'legacy_v2_component_marker_not_human_receipt'} for a in accounts for c in ('text','file') if hashlib.sha256(f'{str(a).strip()}|{c}'.encode()).hexdigest() in tokens]

def official_receipt(send_result,component_key):
    """Adapt Hermes SendResult evidence only. Never call a transport or mark human receipt."""
    def field(key):return send_result.get(key) if isinstance(send_result,dict) else getattr(send_result,key,None)
    raw=field('raw_response');message_id=field('message_id')
    if field('delivered') is False:status='not_delivered'
    elif isinstance(raw,dict) and raw.get('errcode') not in (None,0):status='failed'
    elif field('success') is True and (message_id or raw):status='provider_accepted'
    elif field('success') is True:status='unverified_success'
    else:status='unknown'
    return {'component_key':component_key,'status':status,'evidence':'official_adapter_result','human_received':'unknown'}

def contact_plan(baseline,mapping):
    """Per-customer PNG and per-sales ZIP membership, using supplied reviewed identities."""
    products={};audit=[];matched_keys=set();packages={}
    for r in baseline:
        key=(r['whse_dept'],r['goods_no'],str(r.get('attr_val') or ''))
        rolls=number(r.get('total_piece'))
        if rolls is None:raise WorkflowError('CUSTOMER_CARD_ROLLS_UNKNOWN')
        products[key]=products.get(key,Decimal(0))+rolls
    for cid,purchased in sorted(mapping.get('productsByCustomer',{}).items()):
        selected={k:v for k,v in products.items() if any(p.get('goods_no')==k[1] and p.get('whse_dept')==k[0] for p in purchased)}
        if not selected:continue
        matched_keys.update(selected);info=dict(mapping.get('customerInfo',{}).get(cid,{}))
        for field in ('customer_no','name','sales'):info[field]=str(info.get(field) or '').strip()
        sales=info['sales'];account=str(mapping.get('wecomBySales',{}).get(sales) or '').strip();employee=dict(mapping.get('employeeBySales',{}).get(sales,{}))
        if employee:
            for field in ('wecom_account','region'):employee[field]=str(employee.get(field) or '').strip()
        reason='Missing Customer No' if not info.get('customer_no') else 'Missing Customer Name' if not info.get('name') else 'Missing Sales Owner' if not sales else 'Missing WeCom Account' if not account else 'Sales Owner Not Active' if not employee else 'Personnel Account Mismatch' if employee.get('wecom_account')!=account else 'Personnel Region Mismatch' if employee.get('region') not in policy()['regions'] else None
        for key in selected:audit.append({'product_dept':key[0],'goods_no':key[1],'color':key[2],'customer_no':info.get('customer_no'),'customer_name':info.get('name'),'sales_owner':sales,'account':account,'exception':reason,'dispatch_status':'not_sent','customer_follow_up':'Not Assigned' if reason else 'Pending'})
        if reason:continue
        package=packages.setdefault(account,{'account':account,'sales_name':sales,'sales_names':[],'region':employee['region'],'customers':[]})
        if sales not in package['sales_names']:package['sales_names'].append(sales)
        image_products={}
        for (_,goods,color),rolls in selected.items():image_products[(goods,color)]=image_products.get((goods,color),Decimal(0))+rolls
        package['customers'].append({'customer_id':str(cid),'customer_no':info['customer_no'],'customer_name':info['name'],'products':[[g,c,int(n.quantize(Decimal('1'),rounding=ROUND_HALF_UP))] for (g,c),n in sorted(image_products.items())]})
    for key in products.keys()-matched_keys:audit.append({'product_dept':key[0],'goods_no':key[1],'color':key[2],'exception':'No Matched Customer','dispatch_status':'not_sent','customer_follow_up':'Not Assigned'})
    for package in packages.values():
        package['sales_names'].sort(key=str.casefold);package['sales_name']=package['sales_names'][0]
        package['customers'].sort(key=lambda r:(r['customer_no'],r['customer_name'],r['customer_id']))
    return {'sales_packages':[packages[k] for k in sorted(packages)],'audit_rows':audit,'delivery_state':'not_requested'}

TASK_HEADERS=['Item No','Color','Color Label','Warehouse Dept','Opening Qty','Inventory Unit','Opening Rolls','Slow Type','Promotion Offer','Remarks']
REPORT_HEADERS=['Item No','Color','Warehouse Dept','Movement','Opening Slow Qty','Unit','Closing Slow Qty','Opening Slow Rolls','Closing Slow Rolls','Rolls Change','Net Outbound Rolls','Sold By']

def promotion_offer(row):
    price=number(row.get('promotion_price'))
    if row.get('promotion_price') is None or price==0 or price is not None and price<0:return 'Not Set'
    if price is None:raise WorkflowError('INVALID_PROMOTION_PRICE')
    return f"{price} {row.get('promotion_currency_no') or '[Currency Missing]'} / {row.get('promotion_unit') or '[Unit Missing]'}"

def task_draft(region,week,start,end,rows):
    count=len({(r['goods_no'],str(r.get('attr_val')),r['whse_dept']) for r in rows})
    numbers=[number(r.get('total_piece')) for r in rows]
    rolls=sum(numbers,Decimal(0)) if all(n is not None for n in numbers) else None
    text=f'Slow sales-stock Products\nDept: {region}\nPeriod: {start} ~ {end}\nTotal SKUs: {count}\nTotal Rolls: {show(rolls)}'
    values=[[r.get('goods_no'),r.get('attr_val'),r.get('color_label'),r.get('whse_dept'),number(r.get('total_qty')),r.get('source_unit'),number(r.get('total_piece')),r.get('slow_label'),promotion_offer(r),r.get('remarks')] for r in rows]
    return text,('Products',TASK_HEADERS,values)

def report_draft(region,period,summary,sales_rows,*,monthly=False):
    heading='Monthly' if monthly else 'Weekly'
    text=[f'**{region} Slow-moving Inventory {heading} Report | {period}**','', '**1. Key Metrics**',f"SKUs: {show(summary.get('opening_skus'))} -> {show(summary.get('closing_skus'))}",f"Rolls: {show(summary.get('opening_rolls'))} -> {show(summary.get('closing_rolls'))}",f"New SKUs: {show(summary.get('new'))}",f"Exited SKUs: {show(summary.get('exited'))}",'','**2. Sold This '+('Month' if monthly else 'Week')+'**',f"Total: {show(summary.get('net_outbound_rolls'))} rolls",f"High-Discount: {show(summary.get('high_net_rolls'))} rolls",'','**3. Sold by Sales**']
    text += [str(r.get('sales_name','Unknown'))+': '+show(r.get('net_rolls'))+' rolls' for r in sales_rows] or ['(none)']
    if region.endswith('-HT'):
        def qty_text(values):return ' | '.join(str(unit)+': '+show(value) for unit,value in sorted(values.items())) if isinstance(values,dict) and values else 'Unknown'
        text=[('Total: '+qty_text(summary.get('net_outbound_qty_by_unit'))) if line.startswith('Total: ') else ('High-Discount: '+qty_text(summary.get('high_net_qty_by_unit'))) if line.startswith('High-Discount: ') else line for line in text]
    text+=['','Detailed SKU list is in the attachment.','Inventory state changes and recorded net outbound are separate measures.']
    return '\n'.join(text)

def validate_complete_detail(packet):
    if packet.get('detail_complete') is not True:return {'checked':[],'not_checked':['detail_population_not_declared_complete']}
    rows=packet['detail_rows'];summary=packet['summary']
    if any(len(r)!=len(REPORT_HEADERS) or r[3] not in ('New','Exited','Reduced','No Change','Increased','Unassessable') for r in rows):raise WorkflowError('COMPLETE_DETAIL_SHAPE_INVALID')
    checks={'new':sum(r[3]=='New' for r in rows),'exited':sum(r[3]=='Exited' for r in rows)};unverified=[]
    if not any(r[3]=='Unassessable' for r in rows):checks.update(opening_skus=sum(r[3]!='New' for r in rows),closing_skus=sum(r[3]!='Exited' for r in rows))
    else:unverified.extend(['opening_skus','closing_skus'])
    for key,index,excluded in [('opening_rolls',7,'New'),('closing_rolls',8,'Exited'),('net_outbound_rolls',10,'New')]:
        values=[number(r[index]) for r in rows if r[3]!=excluded]
        if any(v is None for v in values):unverified.append(key);continue
        checks[key]=sum(values,Decimal(0))
    for key in list(checks):
        if number(summary.get(key)) is None:unverified.append(key);del checks[key]
    if any(number(summary[k])!=number(v) for k,v in checks.items()):raise WorkflowError('SUMMARY_DETAIL_MISMATCH')
    return {'checked':list(checks),'not_checked':unverified}

def card_png(customer,path):
    from PIL import Image,ImageDraw,ImageFont
    cfg=policy()['customer_artifacts'];widths=cfg['column_widths'];width=sum(widths)+16;rows=customer['products']
    if len(rows)>5000:raise WorkflowError('CUSTOMER_IMAGE_TOO_LARGE')
    font_path=Path('C:/Windows/Fonts/msyh.ttc')
    font=ImageFont.truetype(str(font_path),13) if font_path.exists() else ImageFont.load_default()
    title_font=ImageFont.truetype(str(font_path),15) if font_path.exists() else font
    title=str(customer['customer_name']);title_lines=[title[i:i+24] for i in range(0,len(title),24)] or ['Unknown']
    top=12+len(title_lines)*23;image=Image.new('RGB',(width,top+30+26*len(rows)+8),'white');draw=ImageDraw.Draw(image)
    for i,line in enumerate(title_lines):draw.text((8,8+i*23),line,font=title_font,fill='#172533')
    for ri,row in enumerate([cfg['headers'],*rows]):
        y=top+(0 if ri==0 else 30+(ri-1)*26);height=30 if ri==0 else 26;x=8
        for value,cw in zip(row,widths):
            text=show(value)
            if draw.textbbox((0,0),text,font=font)[2]>cw-12:raise WorkflowError('CUSTOMER_IMAGE_CELL_TOO_LONG')
            draw.rectangle((x,y,x+cw,y+height),fill='#e9eff5' if ri==0 else 'white',outline='#8998a8');draw.text((x+6,y+5),text,font=font,fill='#172533');x+=cw
    image.save(path,'PNG')

def customer_zip(package,outdir,week):
    token=account_token(package['account'])[:12];folder=outdir/('images_'+token);folder.mkdir()
    path=outdir/f'sales_{token}_{week}.zip';names=set()
    with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
        for i,customer in enumerate(package['customers']):
            filename=safe_name(customer['customer_no'])+'_'+safe_name(customer['customer_name'])+'.png'
            if filename.casefold() in names:raise WorkflowError('CUSTOMER_IMAGE_FILENAME_COLLISION')
            names.add(filename.casefold());image=folder/f'{i:05d}.png';card_png(customer,image)
            info=zipfile.ZipInfo(filename,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o600<<16;z.writestr(info,image.read_bytes())
    if path.stat().st_size>policy()['customer_artifacts']['max_zip_bytes']:raise WorkflowError('ZIP_SIZE_LIMIT_EXCEEDED')
    return path

def price_draft(side,changes):
    """Presentation of explicit changes only; initial observations are not fabricated changes."""
    if not changes:return 'No price-change notification: no confirmed comparable change in this preview.'
    if side=='purchase':
        groups={'🔻 采购价下调':[],'🔺 采购价上调':[],'缺失状态变化（需核对）':[]}
        for row in changes:
            deltas={};parts=[f"**供应商：{show(row.get('supplier_name') or row.get('supplier_no'))}**",f"货号：{show(row.get('goods_no'))}（{show(row.get('goods_name'))}）",f"色标：{row.get('color_label') or '-'}"]
            if row.get('validity_state')=='unknown_validity':parts.append('有效期未确认，仅记录报价变化。')
            for key,label in [('inc','含税价'),('exc','不含税价')]:
                old=number(row.get('old_'+key));new=number(row.get('new_'+key))
                if old==new:continue
                parts.append(f'{label}：{show(old)} → {show(new)}（{show(row.get("unit_cuur"))}；{show(row.get("currency_no"))}）')
                deltas[key]=new-old if old is not None and new is not None else None
            if not deltas:continue
            # Legacy direction is tax-excluded first, even when the two sides
            # move in opposite directions. Missing values are not converted to 0.
            direction=deltas.get('exc') or deltas.get('inc')
            group='缺失状态变化（需核对）' if any(v is None for v in deltas.values()) else '🔻 采购价下调' if direction<0 else '🔺 采购价上调'
            groups[group].append('\n'.join(parts))
        header='**采购报价变更提醒**\n调整日期：'+'、'.join(sorted({str(r.get('adjust_date') or 'Unknown')[:10] for r in changes}))
        return header+'\n\n'+'\n\n**————————————**\n\n'.join('**'+k+'（'+str(len(v))+'条）**\n\n'+'\n\n────────────\n\n'.join(v) for k,v in groups.items() if v)
    return '**Ready Goods Price Change**\n\n'+'\n\n'.join(f"{show(r.get('goods_no'))} | {show(r.get('dept'))} | {show(r.get('customer_grade'))} | {show(r.get('color_label'))}\nDDP: {show(r.get('old_ddp_price'))} -> {show(r.get('new_ddp_price'))} {show(r.get('currency_no'))}" for r in changes)

def operation_preview_input(job,document):
    """Translate existing local observations; never manufacture a first-run change."""
    expected={'idk':'idk_unpriced','sales_price':'sales_prices','purchase_price':'purchase_prices'}
    if document.get('kind')!=expected.get(job) or document.get('status')!='success':raise WorkflowError('OBSERVATION_KIND_OR_STATUS_INVALID')
    result={'evidence_origin':'existing_local_observation','observed_at':document.get('observed_at')}
    if job=='idk':return {**result,'region':'IDK','records':document['records']}
    changes=[]
    if document.get('baseline_id') is not None:
        for event in document.get('events',[]):
            if event.get('event') not in ('price_changed','recorded_quote_changed'):continue
            old=event['before'];new=event['after'];row={**new.get('labels',{}),**new.get('basis',{})}
            if job=='sales_price':
                row.update(dept=new['key'][1],customer_grade=new['key'][2],color_label=new['key'][3],old_ddp_price=old['prices']['ddp_price'],new_ddp_price=new['prices']['ddp_price'])
            else:
                row.update(goods_no=new['key'][0],color_label=new['key'][1],supplier_no=new['key'][2],old_inc=old['prices']['tax_inclue_price'],new_inc=new['prices']['tax_inclue_price'],old_exc=old['prices']['tax_exclue_price'],new_exc=new['prices']['tax_exclue_price'],adjust_date=new.get('source_modified_at'),validity_state=new.get('validity_state'))
            changes.append(row)
    return {**result,'changes':changes,'baseline_state':'accepted_reference' if document.get('baseline_id') is not None else 'unaccepted_observation_no_change_claim','customer_mapping_complete':False}

def preview_from_file(profile,job):
    """Fixed input/report ID wiring. Does not import old code or configure a DB."""
    from .local_report import _assert_local_context
    _assert_local_context()
    if job not in policy()['jobs']:raise WorkflowError('UNKNOWN_WORKFLOW')
    source=profile/'report_inputs'/'legacy'/f'{job}.json'
    for p in (profile/'report_inputs',source.parent,source):
        if p.is_symlink() or not p.resolve().is_relative_to(profile.resolve()):raise WorkflowError('INPUT_PATH_INVALID')
    if not source.is_file():raise WorkflowError('WORKFLOW_PREVIEW_INPUT_MISSING')
    if source.stat().st_size>16*1024*1024:raise WorkflowError('WORKFLOW_PREVIEW_INPUT_TOO_LARGE')
    data=json.loads(source.read_text(encoding='utf-8'))
    if data.get('evidence_origin') not in ('synthetic','existing_local_observation'):raise WorkflowError('PREVIEW_PROVENANCE_REQUIRED')
    import uuid
    root=profile/'report_runs'/'legacy'
    for p in (profile/'report_runs',root):
        if p.is_symlink() or not p.resolve().is_relative_to(profile.resolve()):raise WorkflowError('OUTPUT_PATH_INVALID')
    root.mkdir(parents=True,exist_ok=True);out=root/(job+'-'+uuid.uuid4().hex);out.mkdir()
    try:result=build_preview(job,data,out)
    except Exception:
        (out/'manifest.json').write_text(json.dumps({'job':job,'status':'preview_failed','enabled':False,'sent':False,'frozen':False,'price_baseline_accepted':False}),encoding='utf-8')
        raise
    (out/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    return out

def build_preview(job,data,out):
    files=[];messages=[];extra={};week=data.get('week');region=data.get('region','IDK' if job=='idk' else 'HCM')
    if region not in policy()['regions']:raise WorkflowError('REGION_NOT_REGISTERED')
    if job in ('slow_task','slow_report'):
        try:
            if not re.fullmatch(r'\d{4}-W\d{2}',week):raise ValueError()
            date.fromisocalendar(int(week[:4]),int(week[6:]),1)
        except (TypeError,ValueError):raise WorkflowError('INVALID_WEEK')
    if job=='slow_task':
        plan=freeze_plan(data['source_rows'],data.get('existing_rows',[]),week,data['current_week'],refreeze=data.get('refreeze',True));extra['freeze_plan']=plan
        extra['read_only_source_specs']=source_query_specs(week)
        if plan['status']=='ready':
            baseline=plan['insert_rows'] if plan['action']!='reuse_existing' else [r for r in data['existing_rows'] if r['week_label']==week]
            by_region={r:[v for v in baseline if v['whse_dept']==r] for r in dict.fromkeys(v['whse_dept'] for v in baseline)}
            extra['task_targets']=task_recipients(data['recipient_map'],data['employees'],list(by_region))
            extra['recipient_plan_behavior']={'preview':recipient_plan_decision(week,extra['task_targets'],data.get('existing_recipient_plan'),preview=True),'non_preview':recipient_plan_decision(week,extra['task_targets'],data.get('existing_recipient_plan'),preview=False) if 'existing_recipient_plan' in data else {'action':'existing_week_plan_state_not_supplied; do not assume absent'}}
            extra['customer_plan_behavior']=customer_plan_decision(week,digest(baseline),data.get('existing_customer_plan'),rebuild=data.get('refreeze',True),preview=True)
            extra['component_actions']=[]
            for task_region,rows in by_region.items():
                message,sheet=task_draft(task_region,week,data['start'],data['end'],rows);messages.append(message)
                path=out/f'{week}_{safe_name(task_region)}_Products.xlsx';gen_workbook_xlsx([sheet],path);files.append(path.name)
                for target in extra['task_targets'][task_region]:
                    extra['component_actions'].append({'mode':'task','region':task_region,'account':target['account'],'components':delivery_preview('task',week,task_region,target['account'],['text','file'],data.get('receipts',[]),force=data.get('force_resend',True))})
            contacts=contact_plan(baseline,data.get('customer_mapping',{}));extra['contact_plan']=contacts
            for package in contacts['sales_packages']:
                path=customer_zip(package,out,week);files.append(path.name)
                messages.append(f"Sales owner: {', '.join(package['sales_names'])}\nSlow sales-stock Products - Customer Package\n\nBaseline week: {week}\nCustomers: {len(package['customers'])}. One image per customer in one ZIP.")
                content=digest({'week':week,'account':package['account'],'customers':package['customers']})
                extra['component_actions'].append({'mode':'customer-zip','account':package['account'],'components':delivery_preview('customer-zip',week,'all',package['account'],['summary:'+content,'zip:'+content],data.get('receipts',[]),force=data.get('force_resend',True))})
            for sales_region in dict.fromkeys(p['region'] for p in contacts['sales_packages']):
                packages=[p for p in contacts['sales_packages'] if p['region']==sales_region]
                good=[p for p in packages if data.get('simulated_zip_status',{}).get(p['account'],'Planned') in ('Planned','Sent to Sales','Already Sent to Sales')]
                failed=[p for p in packages if p not in good];selected=good or failed;all_failed=not good
                sheets=[]
                for package in selected:
                    customers=sorted({(r['customer_no'],r['customer_name']) for r in package['customers']})
                    sheets.append((package['sales_name'],['Customer No','Customer'],[list(r) for r in customers]))
                if sheets:
                    suffix='Failure' if all_failed else 'Dispatch'
                    path=out/f'{week}_{safe_name(sales_region)}_Customer_Image_{suffix}.xlsx';gen_workbook_xlsx(sheets,path);files.append(path.name)
                    messages.append(f'Slow sales-stock Customer Image {suffix} PREVIEW\nRegion: {sales_region}\nWeek: {week}\nIncluded sales owners: {len(selected)}\nSimulated failed owners: {len(failed)}\nThe workbook has one sheet per sales owner. Nothing has been dispatched in this preview.')
                    targets={e['account'] for e in data['recipient_map'][sales_region].get('executors',[])}
                    if all_failed:targets.update(data['recipient_map'][sales_region].get('managers',[]))
                    extra.setdefault('audit_targets',{})[sales_region]=sorted(targets)
    elif job=='slow_report':
        groups=data['region_reports'] if 'region_reports' in data else {region:data}
        if not isinstance(groups,dict) or not groups or any(r not in policy()['regions'] for r in groups):raise WorkflowError('REPORT_REGIONS_INVALID')
        extra['targets']={r:report_recipients(data['recipient_map'],r) for r in groups};extra['component_actions']=[]
        if type(data.get('include_monthly',True)) is not bool:raise WorkflowError('INVALID_MONTHLY_SWITCH')
        for monthly in ((False,True) if data.get('include_monthly',True) else (False,)):
            for report_region,group in groups.items():
                key='monthly' if monthly else 'weekly';packet=group[key]
                extra.setdefault('detail_checks',{})[report_region+':'+key]=validate_complete_detail(packet)
                if monthly:
                    try:
                        if not re.fullmatch(r'\d{4}-\d{2}',packet['period']):raise ValueError()
                        date.fromisoformat(packet['period']+'-01')
                    except (KeyError,TypeError,ValueError):raise WorkflowError('INVALID_MONTH')
                messages.append(report_draft(report_region,packet['period'],packet['summary'],packet.get('sales_rows',[]),monthly=monthly))
                path=out/(f'Monthly_{packet["period"]}_{safe_name(report_region)}.xlsx' if monthly else f'{week}_{safe_name(report_region)}_Report.xlsx')
                gen_workbook_xlsx([('Detail',REPORT_HEADERS,packet['detail_rows'])],path,borders=True,landscape=True);files.append(path.name)
                for account in extra['targets'][report_region]:extra['component_actions'].append({'mode':key,'region':report_region,'account':account,'components':delivery_preview('monthly' if monthly else 'report',packet['period'],report_region,account,['text','file'],data.get('receipts',[]),force=True if monthly else data.get('force_resend',False))})
    elif job=='idk':
        records=data.get('records',[]);messages=['**IDK Slow-Moving Products Without Promotion Price**\nScope: all slow-moving pool (as of '+str(data.get('observed_at','recorded review observation'))+')\n'+str(len(records))+' product source row(s) without promotion price:\n'+'\n'.join(f"{i+1}. {r.get('product','Unknown')} | Color {r.get('color') or '-'}" for i,r in enumerate(records))+'\nPlease set promotion prices for the above products.']
        extra['targets']=data.get('idk_executors',[])
    elif job in ('sales_price','purchase_price'):
        changes=data.get('changes',[]);messages=[price_draft('sales' if job=='sales_price' else 'purchase',changes)]
        if job=='sales_price' and changes:
            if data.get('recipient_map') is not None:
                affected=list(dict.fromkeys(r['dept'] for r in changes))
                extra['target_plan']=price_recipients(data['recipient_map'],data.get('employees',[]),affected,data.get('fixed_managers',[]))
            sheets=[(goods,['Customer No','Customer'],rows) for goods,rows in data.get('customers_by_goods',{}).items() if rows]
            if sheets:
                path=out/f'customer_list_{safe_name(region)}_{safe_name(data.get("sales_name","sales"))}.xlsx';gen_workbook_xlsx(sheets,path);files.append(path.name)
            if data.get('customer_mapping_complete') is True:
                messages[0]+='\n\n'+('See attachment for your customers who purchased these products (one Sheet per product).' if sheets else 'You have no customers who purchased these products, so no attachment is included.')
            else:messages[0]+='\n\nCustomer matching is not confirmed complete; do not interpret a missing attachment as no buyers.'
            manager_sheets=[(goods,['Sales','Customer No','Customer'],rows) for goods,rows in data.get('manager_rows_by_goods',{}).items() if rows]
            if manager_sheets:
                path=out/f'customer_list_{safe_name(region)}.xlsx';gen_workbook_xlsx(manager_sheets,path);files.append(path.name)
                messages.append('Ready Goods Price Change — Regional Customer Summary\nThe attachment lists buyers by product and sales owner for this changed region.')
        extra['targets']=data.get('targets',[]) if changes else []
        extra['customer_mapping_state']='declared_complete' if data.get('customer_mapping_complete') is True else 'not_confirmed_complete'
        extra['initial_observation_only']=not bool(changes)
    elif job=='fabric':
        sheets=[]
        for title,table in data['sheets'].items():
            formats={int(k):v for k,v in table.get('number_formats',{}).items()}
            if any(v!='percent' for v in formats.values()):raise WorkflowError('UNSUPPORTED_DISPLAY_FORMAT')
            sheets.append((title,table['headers'],table['rows'],formats))
        path=out/'Fabric_Source_Review.xlsx';gen_workbook_xlsx(sheets,path,borders=True,landscape=True);files.append(path.name)
        messages=['Fabric source review workbook. Source labels and documented gaps are preserved; no loss or responsibility determination is inferred.']
    else:raise WorkflowError('UNKNOWN_WORKFLOW')
    prefix='[SYNTHETIC PREVIEW — NOT SENT]\n' if data['evidence_origin']=='synthetic' else '[EXISTING LOCAL OBSERVATION — NOT SENT]\n'
    for i,message in enumerate(messages):
        path=out/f'message-{i+1:02d}.txt';path.write_text(prefix+message,encoding='utf-8');files.append(path.name)
    return {'job':job,'status':'blocked' if extra.get('freeze_plan',{}).get('status')=='blocked' else 'preview_ready','fixed_report_id':policy()['jobs'][job]['report_id'],'evidence_origin':data['evidence_origin'],'legacy_reference':policy()['reference'],'files':files,'schedule_candidate':policy()['jobs'][job]['schedule'],'timezone':policy()['timezone'],'enabled':False,'sent':False,'frozen':False,'price_baseline_accepted':False,'transport':'Hermes official; not invoked',**extra}
