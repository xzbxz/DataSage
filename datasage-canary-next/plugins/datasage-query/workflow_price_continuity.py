"""Per-key continuation of recorded-price semantics; no invented historic basis."""
from collections import Counter
from decimal import Decimal
from datetime import datetime
import json
from . import legacy_price_bridge as bridge,operations as op,workflow_storage as storage

def normalized_current(side,rows):
    result=[]
    for raw in rows:
        row=dict(raw)
        try:key=bridge.key_of(side,row)
        except op.OperationError:result.append(row);continue
        for name,value in zip(bridge.SPECS[side]['keys'],key):row[name]=int(value) if name=='goods_id' else value
        result.append(row)
    return result
def plan(side,before,current,at):
    normalized=normalized_current(side,current)
    document=bridge.compare(side,before,normalized,at)
    old={bridge.key_of(side,r):dict(r) for r in before};raw_by_key={}
    for row in normalized:
        try:key=bridge.key_of(side,row)
        except op.OperationError:continue
        raw_by_key.setdefault(key,[]).append(row)
    retained=dict(old);next_id=max((int(r['id']) for r in before),default=0)+1
    anomalies=[];advanced=[];initialized=[];notes=Counter()
    healthy={'recorded_price_unchanged','legacy_nominal_price_changed','legacy_recorded_purchase_price_changed','new_key_without_legacy_reference','recovered_reference_without_known_price'}
    for event in document['events']:
        key=tuple(event['key']) if 'key' in event else None
        reason=None
        if event['event']=='absent_from_current_selection':reason='absent_selection_keep_reference'
        elif event['event'] not in healthy:reason=event['event']
        candidate=raw_by_key.get(key,[]) if key else []
        row=candidate[0] if len(candidate)==1 else None
        if side=='purchase' and row is not None and row.get('detail_id') is not None and event['event']!='unresolved_current_record':
            aliases=[]
            for old_key,reference in old.items():
                observed=reference.get('current_record')
                if isinstance(observed,str):observed=json.loads(observed)
                if (old_key!=key and old_key[0].strip()==key[0].strip() and old_key[1:]==key[1:] and observed
                    and str(observed.get('detail_id'))==str(row['detail_id'])):aliases.append((old_key,reference))
            if aliases:
                alias_key,reference=max(aliases,key=lambda item:datetime.fromisoformat(str(item[1]['snapshot_at'])))
                direct=old.get(key)
                if direct is None or datetime.fromisoformat(str(reference['snapshot_at']))>datetime.fromisoformat(str(direct['snapshot_at'])):
                    comparison=dict(reference)
                    for field,value in zip(bridge.SPECS[side]['keys'],key):comparison[field]=value
                    linked=bridge.compare(side,[comparison],[row],at)['events'][0]
                    previous_event=event['event'];event.clear();event.update(linked)
                    event['identity_previous_event']=previous_event
                    event['source_quote_identity_link']={'detail_id':row['detail_id'],'previous_key':list(alias_key),'condition':'same source quote ID, supplier, color and whitespace-only product variant; newer observed reference'}
                    notes['source_quote_identity_reference_reused']+=1
                    reason=None if event['event'] in healthy else event['event']
        if event['event']=='unresolved_legacy_price' and row is not None and all(op._number(row.get(k)) is not None and op._number(row[k])>=0 for k in bridge.SPECS[side]['prices']):
            event['original_event']=event['event'];event['event']='recovered_reference_without_known_price';reason=None
            notes['unknown_previous_price_new_reference_not_change']+=1
        if reason is None and row is not None:
            if any(row.get(k) in (None,'') for k in bridge.SPECS[side]['basis']):reason='current_recorded_basis_missing'
            if side=='sales':
                price=op._number(row.get('ddp_price'))
                if price is not None and price!=price.quantize(Decimal('0.01')):reason='snapshot_decimal_precision_insufficient'
                previous=old.get(key,{}).get('current_record')
                if isinstance(previous,str):previous=json.loads(previous)
                if not previous:notes['historical_unit_tax_unrecorded_nominal_comparison']+=1
                elif any(previous.get(k) not in (None,'') and row.get(k) not in (None,'') and str(previous[k])!=str(row[k]) for k in ('unit','unit_cuur','is_inclue_tax')):reason='observed_unit_tax_changed_keep_reference'
        if reason:
            event['original_event']=event['event'];event['deliverable']=False
            if event['event'] in healthy:event['event']='quarantined_key'
            event['continuation']='retain_previous_reference' if key in old else 'unkeyed_or_new_pending_without_reference'
            event['continuation_reason']=reason
            anomaly={'key':list(key) if key else None,'reason':reason,'previous_reference_retained':key in old,'after':event.get('after'),'before':event.get('before')}
            anomaly['anomaly_id']=storage.digest([side,key,reason,(event.get('after') or {}).get('key')])
            anomalies.append(anomaly);continue
        if row is None:raise ValueError('CONTINUITY_EVENT_RAW_ROW_MISSING')
        prior=old.get(key)
        reference={k:(prior['id'] if prior else next_id) if k=='id' else at.strftime('%Y-%m-%d %H:%M:%S') if k=='snapshot_at' else row.get('detail_id') if k=='matched_detail_id' else row.get(k) for k in bridge.SPECS[side]['fields']}
        reference.update(current_record=storage.canonical(row),reference_kind='observed')
        retained[key]=reference;advanced.append(list(key))
        event['continuation']='advance_after_required_receipts'
        if prior is None:initialized.append(list(key));next_id+=1
    document['event_counts']=dict(Counter(e['event'] for e in document['events']))
    document['deliverable_event_count']=sum(e['deliverable'] for e in document['events'])
    document['continuation']={'policy':'recorded_prices_per_key_v1','advanced_keys':len(advanced),'new_reference_keys':len(initialized),'retained_reference_keys':len(old)-sum(tuple(k) in old for k in advanced),'anomaly_count':len(anomalies),'anomaly_counts':dict(Counter(a['reason'] for a in anomalies)),'notes':dict(notes)}
    document['scope_notice']+=' 异常按键保留旧参考；当前范围未见不等于删除或撤销。新键首次建立参考，不把缺旧价当零。比较各键最近一次可比参考，恢复后发现的变化不一定发生在本小时。仅在来源报价ID、供应商、颜色均相同且货号只差首尾空白时，沿用较新的已观测参考防止别名重复提醒；原始键及证据均保留。'
    return {'document':document,'after':[retained[k] for k in sorted(retained)],'anomalies':anomalies,'advanced_keys':advanced,'initialized_keys':initialized}
