"""Finite test initialization and a once-only price-change fixture. Never resets."""
from datetime import datetime,timedelta
from decimal import Decimal
from . import workflow_storage as s,workflow_cycle as cycle,workflow_inputs as inputs,operations as op
from . import legacy_price_bridge as bridge

def seeded(store,key):
    matches=[r for r in store.rows('cycles') if r['cycle_id']==key]
    if matches and matches[0]['status']!='committed':raise ValueError('SEED_STATE_INCOMPLETE_REVIEW_REQUIRED')
    return cycle.payload(matches[0]) if matches else None
def seed_slow(store):
    existing=seeded(store,'seed-slow')
    if existing:return {'status':'seed_preserved',**existing}
    if store.rows('stock_input') or store.rows('monthly_stock_input'):raise ValueError('UNREGISTERED_TEST_SEED_NO_OVERWRITE')
    with s.tools._ConsistentSnapshotExecutor(deadline_at=s.tools._call_deadline(None)) as db:
        at=datetime.fromisoformat(str(inputs.complete(db,'SELECT NOW(6) AS at',limit=1)[0]['at']))
        month=at.strftime('%Y-%m');opening=(at.replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
        chosen=inputs.complete(db,"SELECT DISTINCT s.goods_sku_id FROM vk_ods.slow_moving_goods_ods s WHERE s.whse_dept='HCM' AND s.goods_num>10 AND s.is_whitelist='n' AND EXISTS (SELECT 1 FROM vk_dw.inventory_barcode_detail_bymonth_dw m WHERE m.goods_sku_id=s.goods_sku_id AND m.whse_dept='HCM' AND m.month_date=%s AND m.is_handing_sales='y' AND m.goods_num>0) ORDER BY s.goods_sku_id LIMIT 3",[opening],limit=3)
        ids=[r['goods_sku_id'] for r in chosen]
        if not ids:raise ValueError('NO_TEST_SOURCE_SKUS')
        marks=','.join(['%s']*len(ids))
        stock=inputs.complete(db,"SELECT * FROM vk_ods.slow_moving_goods_ods WHERE whse_dept='HCM' AND goods_sku_id IN ("+marks+') ORDER BY id LIMIT 10001',ids)
        monthly=inputs.complete(db,'SELECT '+','.join(s.MONTH_FIELDS)+" FROM vk_dw.inventory_barcode_detail_bymonth_dw WHERE whse_dept='HCM' AND month_date IN (%s,%s) AND goods_sku_id IN ("+marks+') ORDER BY month_date,id LIMIT 10001',[opening,month,*ids])
        record={'observed_at':at.isoformat(),'month':month,'week':f'{at.isocalendar().year}-W{at.isocalendar().week:02d}','sku_count':len(ids),'stock_rows':len(stock),'monthly_rows':len(monthly),'source_snapshot':db.marker,'scope':'HCM bounded subset, not full business coverage'}
    with store.transaction():
        store.insert('stock_input',stock);store.insert('monthly_stock_input',monthly)
        for role,rows in [('stock_input',stock),('monthly_stock_input',monthly)]:
            if s.normalized(role,store.rows(role))!=s.normalized(role,rows):raise ValueError('TEST_SEED_READBACK_MISMATCH')
        store.cycle('seed-slow','main','committed',record)
    return record
def seed_price(store,side):
    previous=seeded(store,'seed-'+side)
    if previous:return {'status':'seed_preserved',**previous}
    if store.rows(side+'_snapshot') or any(r['side']==side for r in store.rows('price_input')):raise ValueError('UNREGISTERED_PRICE_SEED_NO_OVERWRITE')
    spec=bridge.SPECS[side]
    with s.tools._ConsistentSnapshotExecutor(deadline_at=s.tools._call_deadline(None)) as db:
        at=datetime.fromisoformat(str(inputs.complete(db,'SELECT NOW(6) AS at',limit=1)[0]['at']))
        old=inputs.complete(db,'SELECT '+','.join(spec['fields'])+' FROM '+spec['table']+' ORDER BY id LIMIT 10001')
        sql,args=op.build_observation({'kind':side+'_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000})
        current=inputs.complete(db,sql,args)
        doc=bridge.compare(side,old,current,at)
        keys=[tuple(e['key']) for e in doc['events'] if e['event']=='recorded_price_unchanged'][:2]
        if len(keys)!=2:raise ValueError('NO_COMPARABLE_PRICE_TEST_SUBSET')
        before=[r for r in old if bridge.key_of(side,r) in keys];now=[]
        for row in current:
            try:key=bridge.key_of(side,row)
            except op.OperationError:continue
            if key in keys:now.append(row)
        record={'observed_at':at.isoformat(),'source_snapshot':db.marker,'reference_table':spec['table'],'reference_rows':len(old),'sample_rows':2,'original_sample_digest':s.digest(before),'original_snapshot_times':sorted({str(r['snapshot_at']) for r in before}),'clock_mode':'controlled_hourly_ticks','scope':'explicitly synthetic test labels/changes derived from read-only reference'}
    before=sorted(before,key=lambda r:bridge.key_of(side,r));now=sorted(now,key=lambda r:bridge.key_of(side,r))
    for i,(b,n) in enumerate(zip(before,now)):
        label='TEST-PROFILE-'+side.upper()+'-'+str(i+1)
        n['goods_no']=label;n['goods_name']='Profile独立测试价格样本'
        if side=='purchase':
            b['goods_no']=label
            for row in (b,n):row['supplier_no']='TEST-SUPPLIER-'+str(i+1);row['supplier_name']='测试供应商';row['goods_name']='Profile独立测试价格样本'
        for field in spec['prices']:n[field]=b[field]
    scopes=cycle.SCOPES if side=='sales' else ('main',)
    with store.transaction():
        for scope in scopes:
            store.insert(side+'_snapshot',[{**r,'test_scope':scope} for r in before])
            store.insert('price_input',[{'test_scope':scope,'side':side,'ordinal':i,'payload':s.canonical(row)} for i,row in enumerate(now)])
            check=bridge.compare(side,cycle.clean(store.rows(side+'_snapshot',scope)),cycle.inputs(store,side,scope),at)
            if check['event_counts']!={'recorded_price_unchanged':2}:raise ValueError('PRICE_SEED_READBACK_MISMATCH')
        store.cycle('seed-'+side,'main','committed',record)
    return record
def seed():
    store=s.Store()
    try:
        with cycle.lock(store,'seed','main'):
            result={'slow':seed_slow(store),'sales':seed_price(store,'sales'),'purchase':seed_price(store,'purchase')}
            s.save('seed-result.json',result);return result
    finally:store.close()
def change(side,scope='main'):
    if side not in ('sales','purchase') or scope not in cycle.SCOPES:raise ValueError('FIXTURE_SCOPE_INVALID')
    store=s.Store()
    try:
        with cycle.lock(store,side,scope):
            marker='fixture-'+side+'-'+scope
            if seeded(store,marker):return {'status':'fixture_already_applied_no_change','side':side,'scope':scope}
            if any(r['status']!='committed' for r in cycle.journals(store,side,scope)):raise ValueError('PENDING_CYCLE_BLOCKS_FIXTURE')
            rows=cycle.inputs(store,side,scope)
            if len(rows)!=2:raise ValueError('FIXTURE_EXPECTS_TWO_SEEDED_ROWS')
            before=s.digest(rows);field=bridge.SPECS[side]['prices'][0]
            rows[0][field]=str(Decimal(str(rows[0][field]))+Decimal('1.00'))
            extra=dict(rows[0]);extra['detail_id']=int(extra['detail_id'])+100000000
            if side=='sales':extra['goods_id']=int(extra['goods_id'])+100000000;extra['goods_no']='TEST-PROFILE-SALES-NEW'
            else:extra['supplier_no']='TEST-SUPPLIER-NEW'
            if scope=='main':rows.append(extra)
            with store.transaction():
                for i,row in enumerate(rows):
                    if i<2:store._write('UPDATE '+s.TABLES['price_input']+' SET payload=%s WHERE test_scope=%s AND side=%s AND ordinal=%s',[s.canonical(row),scope,side,i])
                    else:store.insert('price_input',[{'test_scope':scope,'side':side,'ordinal':i,'payload':s.canonical(row)}])
                if cycle.inputs(store,side,scope)!=rows:raise ValueError('FIXTURE_READBACK_MISMATCH')
                record={'side':side,'scope':scope,'before':before,'after':s.digest(rows),'synthetic':True,'new_keys':int(scope=='main')}
                store.cycle(marker,scope,'committed',record)
            return record
    finally:store.close()
