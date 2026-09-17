"""Bounded read-only investigation of whitespace variants in quarantined purchase keys."""
from datetime import datetime
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle,workflow_inputs as inputs,operations as op

def run():
    store=live.Store()
    try:
        rows=[r for r in store.rows('cycles','purchase') if r['cycle_id'].startswith('lp-purchase-')]
        latest=max(rows,key=lambda r:cycle.payload(r)['observation']['observed_at']);data=cycle.payload(latest)
        variants=[]
        for anomaly in data.get('key_anomalies',[]):
            value=(anomaly.get('after') or {}).get('labels',{}).get('goods_no')
            if isinstance(value,str) and value.strip() and value!=value.strip():variants.append((value,value.strip()))
        variants=sorted(set(variants))
        if len(variants)>3:raise ValueError('DIAGNOSTIC_VARIANT_BUDGET_EXCEEDED')
    finally:store.close()
    if not variants:return {'status':'no_current_variant_prior_evidence_preserved','production_queries':0,'evidence_file':str(live.root()/'purchase-anomaly-evidence.json')}
    records=[]
    with base.tools._ConsistentSnapshotExecutor(deadline_at=base.tools._call_deadline(None)) as db:
        clock=inputs.complete(db,'SELECT NOW(6) AS at,UTC_TIMESTAMP(6) AS utc_at',limit=1)[0]
        for raw,trimmed in variants:
            prefix=trimmed.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
            queries=[('ready_goods','SELECT DISTINCT goods_id,goods_no,HEX(goods_no) AS goods_no_hex,CHAR_LENGTH(goods_no) AS character_count,dept,type FROM vk_dwd.ready_goods_dwd WHERE goods_no LIKE %s ESCAPE \'!\' AND dept IN (%s,%s,%s,%s) AND type<>%s ORDER BY goods_no_hex,dept LIMIT 101',[prefix,'HCM','HN','BKK','IDK',op.policy()['purchase']['cancelled_ready_type']]),
                     ('active_promotion',"SELECT goods_no,HEX(goods_no) AS goods_no_hex,CHAR_LENGTH(goods_no) AS character_count,promotion_area FROM data_assistant.push_goods_report WHERE goods_no LIKE %s ESCAPE '!' AND promotion_area IN (%s,%s,%s,%s) AND CURRENT_DATE()>=promotion_start_time AND CURRENT_DATE()<=COALESCE(promotion_end_time,default_end_time) ORDER BY goods_no_hex,promotion_area LIMIT 101",[prefix,'HCM','HN','BKK','IDK']),
                     ('purchase_quotes',"SELECT goods_no,HEX(goods_no) AS goods_no_hex,CHAR_LENGTH(goods_no) AS character_count,detail_id,color_label,supplier_no,tax_inclue_price,tax_exclue_price,currency_no,unit_cuur,gmt_modified FROM vk_dwd.purchase_price_bill_detail_dwd WHERE goods_no LIKE %s ESCAPE '!' AND parent_org_ids=%s ORDER BY goods_no_hex,gmt_modified DESC,detail_id LIMIT 101",[prefix,op.policy()['purchase']['parent_org_ids']])]
            found=[]
            for name,sql,args in queries:found.append({'source':name,'sql':sql,'params':args,'rows':inputs.complete(db,sql,args,100)})
            records.append({'raw_goods_no':raw,'raw_codepoints':['U+'+format(ord(c),'04X') for c in raw],'trimmed_search_variant':trimmed,'queries':found,'normalization_applied_to_business_keys':False})
        result={'observed_at':str(clock['at']),'observed_utc':str(clock['utc_at']),'snapshot_marker':db.marker,'records':records,'production_writes':False,'test_reference_changed':False,'scope':'only whitespace-variant keys already quarantined in latest sealed purchase observation'}
    previous=live.root()/'purchase-anomaly-evidence.json'
    if previous.exists():
        import json
        earlier=json.loads(previous.read_text(encoding='utf-8'))
        stamp=datetime.fromisoformat(earlier['observed_at']).strftime('%Y%m%d%H%M%S%f')
        live.save('purchase-anomaly-evidence-'+stamp+'.json',earlier)
    stamp=datetime.fromisoformat(result['observed_at']).strftime('%Y%m%d%H%M%S%f')
    live.save('purchase-anomaly-evidence-'+stamp+'.json',result);live.save('purchase-anomaly-evidence.json',result)
    return {'status':'read_only_diagnostic_complete','observed_at':result['observed_at'],'variants':len(records),'source_counts':[{q['source']:len(q['rows']) for q in r['queries']} for r in records],'evidence_file':str(live.root()/'purchase-anomaly-evidence.json')}
def replay():
    from . import workflow_price_continuity as policy,workflow_live_prices as prices
    store=live.Store();checks=[]
    try:
        for side in ('sales','purchase'):
            rows=[r for r in store.rows('cycles',side) if r['status']=='committed' and cycle.payload(r).get('document',{}).get('continuation')]
            for row in rows:
                data=cycle.payload(row)
                result=policy.plan(side,data['before_records'],data['current'],datetime.fromisoformat(data['observation']['observed_at']))
                if prices.snapshot_digest(side,result['after'])!=data['after_digest']:raise ValueError('SEALED_CONTINUITY_REPLAY_MISMATCH')
                checks.append({'cycle':row['cycle_id'],'snapshot_matches_committed':True,'events':result['document']['event_counts'],'continuation':result['document']['continuation'],'quote_identity_links':[e['source_quote_identity_link'] for e in result['document']['events'] if 'source_quote_identity_link' in e]})
        from . import workflow_delivery_review as review
        reused=[]
        for row in store.rows('cycles'):
            if not row['cycle_id'].startswith('ls-'):continue
            for phase,manifest in cycle.payload(row).get('phases',{}).items():
                receipt=manifest.get('receipt',{})
                if not receipt.get('reused_prior_delivery'):continue
                proof=review.review(store,row['cycle_id'],manifest)
                if not any(m['source']==receipt['matched_prior_source'] for m in proof['prior_matches']):raise ValueError('REUSED_DELIVERY_PROOF_MISMATCH')
                reused.append({'cycle':row['cycle_id'],'phase':phase,'prior_source':receipt['matched_prior_source'],'same_delivery_week_and_sealed_content_verified':True,'provider_receipt_verified':True})
        result={'status':'verified','production_reads':0,'database_writes':0,'messages_sent':0,'checks':checks,'reused_delivery_proof':reused}
        live.save('continuity-replay.json',result);return result
    finally:store.close()
