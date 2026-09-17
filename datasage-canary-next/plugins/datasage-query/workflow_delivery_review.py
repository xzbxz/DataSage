"""Compare business content against actual accepted test deliveries, not instance IDs."""
from pathlib import Path
import json,hashlib,zipfile,io as binary_io,re
from datetime import datetime
from . import acceptance_delivery as delivery,workflow_io as io,workflow_storage as base,workflow_cycle as cycle

PREFIXES=('【真实来源验收，非生产派发】\n','【独立测试表构造变价，非生产价格变化】\n')
def body(notice):
    value=notice['body'].strip()
    for prefix in PREFIXES:
        if value.startswith(prefix.strip()):value=value[len(prefix.strip()):].lstrip()
    return value
def signature(notice):
    parts=[]
    for path in notice.get('attachments',[]):
        _,raw,_,_=delivery.file_snapshot(path,delivery.runtime_home(base.profile()));parts.append(content_digest(path,raw))
    return base.digest([notice['channel'],notice.get('message_format','text'),notice.get('mention_all',False),body(notice),parts])
def content_digest(path,raw):
    # Validation/bounds are owned by file_snapshot. Ignore only outer artifact
    # filename and ZIP container times; retain sheet names, cells and PNG bytes.
    digest=hashlib.sha256();suffix=Path(path).suffix.lower();digest.update(suffix.encode())
    if suffix in ('.zip','.xlsx'):
        with zipfile.ZipFile(binary_io.BytesIO(raw)) as archive:
            for name in sorted(archive.namelist()):digest.update(name.encode()+b'\0'+hashlib.sha256(archive.read(name)).digest())
    else:digest.update(raw)
    return digest.hexdigest()
def accepted(receipt,expected_binding=None):
    return delivery.classify_receipt(receipt,expected_binding).get('reusable') is True
def same_delivery_week(key,document,receipt,source):
    target=re.search(r'(\d{4})-w(\d{2})',key,re.I)
    if not target:return False
    expected=(int(target[1]),int(target[2]));evidence=document.get('evidence',{})
    explicit=[source if source.startswith('ls-') else '',evidence.get('baseline_week',''),evidence.get('period','')]
    for value in explicit:
        match=re.search(r'(\d{4})-w(\d{2})',str(value),re.I)
        if match:return (int(match[1]),int(match[2]))==expected
    for notice in document.get('notices') or ([document['notice']] if 'notice' in document else []):
        match=re.search(r'Week\s*:\s*(\d{4})-W(\d{2})',notice.get('body',''),re.I)
        if match:return (int(match[1]),int(match[2]))==expected
    legacy_week=re.search(r'(?:-|_)w(\d{2})(?:-|_|$)',receipt.get('case_id',''),re.I)
    observed=evidence.get('observed_at') or evidence.get('completeness',{}).get('observed_to')
    if legacy_week and observed:
        try:calendar=datetime.fromisoformat(observed).isocalendar()
        except ValueError:return False
        return calendar[:2]==expected and int(legacy_week[1])==expected[1]
    return False
def sealed(document,items):
    expected=document.get('source_content_digest') or document.get('notice_digest')
    material=document['notice'] if 'notice' in document else items
    if not expected or base.digest(material)!=expected:return False
    for notice in items:
        for path in notice.get('attachments',[]):
            metadata=document.get('files',{}).get(path)
            expected_file=metadata.get('sha256') if isinstance(metadata,dict) else metadata
            if not expected_file:return False
            _,raw,_,_=delivery.file_snapshot(path,delivery.runtime_home(base.profile()))
            if hashlib.sha256(raw).hexdigest()!=expected_file:return False
    return True


def _stage_key(key,notice):
    logical=str(notice.get('logical_id',''))
    return logical if logical.startswith(str(key)+'-') else str(key)


def _binding_for_manifest(key,manifest):
    notices=manifest.get('notices') or []
    if not notices:raise ValueError('DELIVERY_NOTICE_MANIFEST_EMPTY')
    stage_key=_stage_key(key,notices[0])
    return delivery.binding_for_manifest(base.profile(),stage_key,notices,manifest=manifest)


def _prior_binding(document,receipt,items):
    binding=receipt.get('delivery_binding') if isinstance(receipt,dict) else None
    if not delivery.validate_delivery_binding(binding):return None,'delivery_binding_missing_or_invalid'
    try:
        expected=delivery.binding_for_manifest(base.profile(),binding['cycle_id'],items,manifest=document,case_id=binding['case_id'],period=binding['period'],generation=binding['generation'],phase=binding['phase'])
    except (ValueError,OSError,io.IOErrorBoundary):
        return None,'prior_manifest_binding_unavailable'
    if expected!=binding:return None,'prior_manifest_binding_mismatch'
    return binding,None


def summarize_receipts(entries):
    """Project provider, historical, and binding-verified receipt counts separately."""
    provider=[];fresh=[];verified=[];historical=[];unverified=[];recorded=[];entry_count=0
    for entry in entries:
        entry_count+=1
        if isinstance(entry,dict):receipt=entry.get('receipt');manifest=entry.get('manifest')
        else:
            try:receipt,manifest=entry[0],entry[1]
            except (IndexError,TypeError):receipt,manifest=None,None
        if isinstance(receipt,dict) and receipt.get('status')=='provider_accepted_not_human_read':
            recorded.append(receipt)
            provider.append(receipt)
            if not receipt.get('reused_prior_delivery'):fresh.append(receipt)
        elif isinstance(receipt,dict):
            recorded.append(receipt)
        if not isinstance(receipt,dict) or receipt.get('status')!='provider_accepted_not_human_read':
            unverified.append({'classification':'not_provider_accepted','receipt':receipt});continue
        binding=receipt.get('delivery_binding')
        if not delivery.validate_delivery_binding(binding):
            classified=delivery.classify_receipt(receipt)
            historical.append({'classification':'historical_provider_accepted','receipt':receipt,'reason':classified.get('reason'),'evidence_level':classified.get('evidence_level','historical_record_declared_provider_acceptance')});continue
        notices=manifest.get('notices') if isinstance(manifest,dict) else None
        if not isinstance(notices,list) or not notices:
            unverified.append({'classification':'unverified_no_current_context','receipt':receipt});continue
        try:
            prior_binding,reason=_prior_binding(manifest,receipt,notices)
            if prior_binding is None or not sealed(manifest,notices):
                unverified.append({'classification':'unverified_receipt','receipt':receipt,'reason':reason or 'current_manifest_not_sealed'});continue
            classified=delivery.classify_receipt(receipt,prior_binding)
        except (ValueError,OSError,io.IOErrorBoundary) as exc:
            unverified.append({'classification':'unverified_receipt','receipt':receipt,'reason':str(exc)});continue
        if classified.get('classification')=='verified_for_reuse':verified.append(receipt)
        else:unverified.append({'classification':classified.get('classification'),'receipt':receipt,'reason':classified.get('reason')})

    def total(items,field):
        return sum(value.get(field,0) for value in items if isinstance(value,dict) and type(value.get(field)) is int and value.get(field)>=0)
    return {
        'recorded_logical_notifications':total(recorded,'logical_notifications'),
        'recorded_components':total(recorded,'components'),
        'provider_logical_notifications':total(provider,'logical_notifications'),
        'provider_components':total(provider,'components'),
        'fresh_provider_logical_notifications':total(fresh,'logical_notifications'),
        'fresh_provider_components':total(fresh,'components'),
        'verified_logical_notifications':total(verified,'logical_notifications'),
        'verified_components':total(verified,'components'),
        'historical_logical_notifications':total([item['receipt'] for item in historical],'logical_notifications'),
        'historical_components':total([item['receipt'] for item in historical],'components'),
        'unverified_logical_notifications':total([item['receipt'] for item in unverified],'logical_notifications'),
        'unverified_components':total([item['receipt'] for item in unverified],'components'),
        'receipt_count':entry_count,
        'provider_accepted_receipt_count':sum(item.get('status')=='provider_accepted_not_human_read' for item in provider),
        'verified_receipt_count':len(verified),
        'historical_provider_accepted_count':len(historical),
        'unverified_receipt_count':len(unverified),
        'reused_prior_receipts':len(provider)-len(fresh),
        'all_component_receipts_verified':bool(entry_count) and len(verified)==entry_count,
    }


def review(store,key,manifest):
    notices=manifest['notices'];matches=[];checked=0;unverified=0;legacy_unverified=[];binding_mismatches=[]
    if len(notices)!=1:return {'version':3,'prior_matches':[],'checked':0,'reason':'multi_notice_no_automatic_equivalence'}
    current=notices[0];wanted=signature(current);candidates=[]
    try:
        current_binding=_binding_for_manifest(key,manifest)
        manifest['delivery_binding']=current_binding
    except (ValueError,OSError,io.IOErrorBoundary) as exc:
        return {'version':3,'prior_matches':[],'checked':0,'unverified_candidates':0,'reason':'current_delivery_binding_unavailable','binding_error':str(exc)}
    root=io.private_root(delivery.runtime_home(base.profile()))
    paths=list(root.rglob('*manifest.json'))
    if len(paths)>1000:raise ValueError('PRIOR_DELIVERY_REVIEW_BUDGET_EXCEEDED')
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):continue
        try:
            doc=json.loads(path.read_text(encoding='utf-8'));items=doc.get('notices') or ([doc['notice']] if 'notice' in doc else [])
            receipts=[path.parent/'send-result.json',path.with_name(path.name.replace('-manifest.json','-receipt.json'))]
            receipt=next((json.loads(r.read_text(encoding='utf-8')) for r in receipts if r!=path and r.is_file() and not r.is_symlink() and r.resolve().is_relative_to(root.resolve())),None)
            if receipt:candidates.append((items,receipt,str(path),doc))
        except (ValueError,OSError):unverified+=1
    for row in store.rows('cycles'):
        if row['cycle_id']==key or not row['cycle_id'].startswith('ls-'):continue
        data=cycle.payload(row)
        for phase,value in data.get('phases',{}).items():
            if 'receipt' in value:candidates.append((value['notices'],value['receipt'],row['cycle_id']+':'+phase,value))
    for items,receipt,source,document in candidates:
        checked+=1
        prior_binding,reason=_prior_binding(document,receipt,items)
        if prior_binding is None:
            if isinstance(receipt,dict) and receipt.get('status')=='provider_accepted_not_human_read':
                legacy_unverified.append({'source':source,'classification':'historical_provider_accepted','evidence_level':'historical_record_declared_provider_acceptance','reason':reason or 'delivery_binding_missing_or_invalid','components':receipt.get('components'),'case_id':receipt.get('case_id'),'progress_observation':delivery.inspect_progress(receipt)})
            else:unverified+=1
            if reason:binding_mismatches.append({'source':source,'reason':reason})
            continue
        try:
            if len(items)!=1:
                unverified+=1;binding_mismatches.append({'source':source,'reason':'multi_notice_no_automatic_equivalence'});continue
            receipt_state=delivery.classify_receipt(receipt,prior_binding)
            if receipt_state.get('classification')!='verified_for_reuse':
                unverified+=1;binding_mismatches.append({'source':source,'reason':receipt_state.get('reason')});continue
            if not delivery.cross_entry_compatible(prior_binding,current_binding) or signature(items[0])!=wanted:
                unverified+=1;binding_mismatches.append({'source':source,'reason':'delivery_context_mismatch'});continue
            if sealed(document,items):
                matches.append({'source':source,'receipt':receipt,'business_digest':wanted,'content_manifest_digest':current_binding['content_manifest_digest'],'delivery_binding':prior_binding})
            else:
                unverified+=1;binding_mismatches.append({'source':source,'reason':'prior_content_not_sealed'})
        except (ValueError,OSError,io.IOErrorBoundary):
            unverified+=1
    return {'version':3,'business_digest':wanted,'content_manifest_digest':current_binding['content_manifest_digest'],'delivery_binding':current_binding,'prior_matches':matches,'checked_receipt_candidates':checked,'unverified_candidates':unverified,'legacy_unverified':legacy_unverified,'binding_mismatches':binding_mismatches,'comparison':'same period, generation, phase, final target binding and existing business signature; prior content manifest, component keys and progress scope are verified against that prior receipt'}
