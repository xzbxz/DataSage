"""Read-only migration bridge to the two ACTUAL legacy database snapshots.

No local accepted pointer, snapshot rewrite, DML or notification side effects.
Missing historical basis is disclosed, never populated from current quotations.
"""
from collections import Counter
from datetime import datetime
import hashlib,json,time
from . import operations as op

SPECS={
 'sales':{'table':'vk_ai.ready_goods_price_snapshot','keys':['goods_id','dept','customer_grade','color_label'],
          'prices':['ddp_price'],'basis':['currency_no'],'missing':['unit','unit_cuur','is_inclue_tax','effective_date','expiration_date'],
          'fields':['id','goods_id','dept','customer_grade','color_label','ddp_price','currency_no','matched_detail_id','snapshot_at']},
 'purchase':{'table':'vk_ai.ready_goods_purchase_snapshot','keys':['goods_no','color_label','supplier_no'],
             'prices':['tax_inclue_price','tax_exclue_price'],'basis':['currency_no','unit_cuur'],
             'missing':['effective_date','expiration_date'],
             'fields':['id','goods_no','color_label','supplier_no','supplier_name','goods_name','tax_inclue_price','tax_exclue_price','currency_no','unit_cuur','snapshot_at']}}

def key_of(side,row):
    spec=SPECS[side];parts=[]
    for name in spec['keys']:
        value=row.get(name)
        if name in ('color_label','customer_grade'):value='' if value is None else value
        if value is None or isinstance(value,bool) or name not in ('color_label','customer_grade') and str(value)=='':
            raise op.OperationError('LEGACY_REFERENCE_KEY_INCOMPLETE')
        if name=='goods_id':
            number=op._number(value)
            if number is None or number<=0 or number!=number.to_integral():raise op.OperationError('LEGACY_REFERENCE_KEY_INCOMPLETE')
            value=str(int(number))
        parts.append(str(value))
    return tuple(parts)

def reference(side,rows,observed_at):
    spec=SPECS[side];indexed={};ids=set();times=[]
    for row in rows:
        key=key_of(side,row)
        if key in indexed:raise op.OperationError('LEGACY_REFERENCE_DUPLICATE_KEY')
        if row.get('id') is None or row['id'] in ids:raise op.OperationError('LEGACY_REFERENCE_ROW_ID_INVALID')
        ids.add(row['id'])
        try:stamp=datetime.fromisoformat(str(row['snapshot_at']))
        except (ValueError,TypeError,KeyError):raise op.OperationError('LEGACY_REFERENCE_TIME_MISSING') from None
        if stamp>observed_at:raise op.OperationError('LEGACY_REFERENCE_TIME_IN_FUTURE')
        times.append(stamp)
        prices={name:str(op._number(row[name])) if op._number(row.get(name)) is not None else None for name in spec['prices']}
        state='legacy_record' if all(v is not None and op._number(v)>=0 for v in prices.values()) else 'legacy_price_unknown_or_negative'
        indexed[key]={'key':list(key),'state':state,'prices':prices,'basis':{name:row.get(name) for name in spec['basis']},
            'validity':{},'historical_fields_not_recorded':list(spec['missing']),'snapshot_at':stamp.isoformat(),
            'matched_detail_id':row.get('matched_detail_id') if side=='sales' else None,
            'labels':{name:row.get(name) for name in ('goods_no','goods_name','supplier_name') if name in row}}
    metadata={'table':spec['table'],'rows':len(rows),'unique_keys':len(indexed),
        'snapshot_min':min(times).isoformat() if times else None,'snapshot_max':max(times).isoformat() if times else None,
        'timestamp_state':'empty' if not times else 'single' if len(set(times))==1 else 'mixed_row_times',
        'historical_fields_not_recorded':list(spec['missing']),'read_only':True}
    return indexed,metadata

def compare(side,old_rows,current_rows,observed_at):
    old,metadata=reference(side,old_rows,observed_at)
    current=op.classify_prices(side,current_rows,observed_at)
    raw_by_key={}
    for row in current_rows:
        try:key=key_of(side,row)
        except op.OperationError:continue
        raw_by_key.setdefault(key,[]).append(row)
    events=[];seen=set()
    for after in current:
        raw_key=dict(zip(SPECS[side]['keys'],after['key']))
        try:key=key_of(side,raw_key)
        except op.OperationError:
            events.append({'event':'unresolved_current_identity','before':None,'after':after,'deliverable':False});continue
        if key in seen:raise op.OperationError('CURRENT_REFERENCE_NORMALIZED_KEY_DUPLICATE')
        seen.add(key);before=old.get(key)
        event={'key':list(key),'before':before,'after':after,'deliverable':False,
            'comparison_level':'nominal_ddp_historical_unit_tax_unverified' if side=='sales' else 'recorded_tax_price_same_currency_unit_validity_unverified'}
        raw=raw_by_key.get(key,[])
        current_readable=(len(raw)==1 and raw[0].get('detail_id') is not None and
            after.get('candidate_rows')==1 and all(op._number(after['prices'].get(name)) is not None and op._number(after['prices'][name])>=0 for name in SPECS[side]['prices']))
        if not current_readable:
            event['event']='unresolved_current_record'
        elif before is None:event['event']='new_key_without_legacy_reference'
        elif before['state']!='legacy_record':event['event']='unresolved_legacy_price'
        else:
            basis=SPECS[side]['basis']
            missing=[name for name in basis if before['basis'].get(name) in (None,'') or after['basis'].get(name) in (None,'')]
            changed=[name for name in basis if name not in missing and before['basis'][name]!=after['basis'][name]]
            if missing:event.update(event='unresolved_recorded_basis',basis_missing=missing)
            elif changed:event.update(event='recorded_basis_changed',basis_changed=changed)
            else:
                differences=[name for name in SPECS[side]['prices'] if op._number(before['prices'][name])!=op._number(after['prices'][name])]
                if not differences:event['event']='recorded_price_unchanged'
                elif len(raw_by_key.get(key,[]))!=1:event['event']='unresolved_current_record'
                else:
                    raw=raw_by_key[key][0]
                    if not raw.get('goods_no'):event['event']='unresolved_event_product_label'
                    else:
                        event.update(event='legacy_nominal_price_changed' if side=='sales' else 'legacy_recorded_purchase_price_changed',
                            deliverable=True,changed_price_fields=differences,
                            current_source_modified_at=str(raw.get('gmt_modified')) if raw.get('gmt_modified') is not None else None)
        events.append(event)
    for key,before in old.items():
        if key not in seen:events.append({'key':list(key),'event':'absent_from_current_selection','before':before,'after':None,'deliverable':False})
    # Do not set baseline_id to a fabricated locally accepted observation.
    return {'status':'success','kind':side+'_prices','baseline_source':'legacy_database','reference':metadata,
        'observed_at':observed_at.isoformat(),'observation_clock':'database local time; original snapshot_at storage time',
        'source_rows':len(current_rows),'records':current,'events':events,'event_counts':dict(Counter(e['event'] for e in events)),
        'deliverable_event_count':sum(e['deliverable'] for e in events),'production_baseline_accepted':False,
        'baseline_state':'existing_legacy_database_reference_readonly',
        'scope_notice':('旧销售快照未记录历史库存单位、计价单位、税口径及有效期；以下只核验同币种名义DDP字段变化，不将当前字段补为历史，也不据此认定同口径涨跌或当前可执行报价。' if side=='sales' else
            '旧采购快照的币种和计价单位已逐项比较，含税价与未税价分开核验；历史有效期未记录，以下属于报价记录变化，不保证当前可执行。')}

def observe(binding,*,snapshots=None):
    from . import tools
    from .local_report import _assert_local_context
    _assert_local_context()
    binding=dict(binding);binding.pop('reference_source',None);binding=op.validate_binding(binding)
    if binding['kind'] not in ('sales_prices','purchase_prices') or set(binding['regions'])!=set(op.policy()['regions']):
        raise op.OperationError('LEGACY_REFERENCE_REQUIRES_ORIGINAL_REGION_SCOPE')
    side='sales' if binding['kind']=='sales_prices' else 'purchase';spec=SPECS[side]
    deadline=tools._call_deadline(None)
    factory=snapshots or (lambda:tools._ConsistentSnapshotExecutor(deadline_at=deadline))
    with factory() as db:
        clock,cut,_=db.execute('SELECT NOW(6) AS observed_at,UTC_TIMESTAMP(6) AS observed_utc',[],1)
        if cut or len(clock)!=1:raise op.OperationError('LEGACY_REFERENCE_CLOCK_MISSING')
        observed=datetime.fromisoformat(str(clock[0]['observed_at']))
        old,cut,_=db.execute('SELECT '+','.join(spec['fields'])+' FROM '+spec['table']+' ORDER BY id LIMIT %s',[binding['limit']+1],binding['limit'])
        if cut or len(old)>binding['limit']:raise op.OperationError('LEGACY_REFERENCE_TRUNCATED')
        sql,params=op.build_observation(binding)
        current,cut,_=db.execute(sql,params,binding['limit'])
        if cut or len(current)>binding['limit']:raise op.OperationError('CURRENT_PRICE_OBSERVATION_TRUNCATED')
        if any(datetime.fromisoformat(str(row['observed_at'])).date()!=observed.date() for row in current):
            raise op.OperationError('PRICE_OBSERVATION_DAY_CHANGED')
        doc=compare(side,old,current,observed)
        doc['snapshot_marker']=getattr(db,'marker',None)
        if not doc['snapshot_marker']:raise op.OperationError('LEGACY_REFERENCE_SNAPSHOT_REQUIRED')
        doc['reference_digest']=hashlib.sha256(json.dumps(old,sort_keys=True,default=str).encode()).hexdigest()
        doc['scope_hash']=op.scope_fingerprint({**binding,'reference_source':'legacy_database'})
        return doc

def changes(document):
    """Only confirmed raw-field changes; explicit limitations accompany messages."""
    side='sales' if document['kind']=='sales_prices' else 'purchase';result=[]
    for event in document['events']:
        if not event.get('deliverable'):continue
        old,new=event['before'],event['after'];key=event['key']
        row={**new['labels'],**new['basis'],'comparison_level':event['comparison_level'],'source_modified_at':event['current_source_modified_at']}
        if side=='sales':row.update(dept=key[1],customer_grade=key[2],color_label=key[3],old_ddp_price=old['prices']['ddp_price'],new_ddp_price=new['prices']['ddp_price'])
        else:row.update(goods_no=key[0],color_label=key[1],supplier_no=key[2],old_inc=old['prices']['tax_inclue_price'],new_inc=new['prices']['tax_inclue_price'],old_exc=old['prices']['tax_exclue_price'],new_exc=new['prices']['tax_exclue_price'],adjust_date=event['current_source_modified_at'],validity_state='unknown_validity')
        result.append(row)
    return result
