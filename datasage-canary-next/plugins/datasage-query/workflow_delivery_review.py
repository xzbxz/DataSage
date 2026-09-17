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
def accepted(receipt):
    if receipt.get('status')!='provider_accepted_not_human_read':return False
    path=Path(receipt.get('progress_file',''))
    allowed=io.private_root(delivery.runtime_home(base.profile())).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(allowed) or not path.is_file():return False
    state=json.loads(path.read_text(encoding='utf-8'))
    return sum(len(k)==64 and v.get('status')=='provider_accepted' for k,v in state.get('components',{}).items())==receipt.get('components')
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
def review(store,key,manifest):
    notices=manifest['notices'];matches=[];checked=0;unverified=0
    if len(notices)!=1:return {'version':3,'prior_matches':[],'checked':0,'reason':'multi_notice_no_automatic_equivalence'}
    current=notices[0];wanted=signature(current);candidates=[]
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
        for prior in items:
            if prior.get('channel')!=current['channel'] or body(prior)!=body(current):continue
            try:
                if same_delivery_week(key,document,receipt,source) and sealed(document,items) and accepted(receipt) and signature(prior)==wanted:matches.append({'source':source,'receipt':receipt,'business_digest':wanted})
            except (ValueError,OSError,io.IOErrorBoundary):unverified+=1
    return {'version':3,'business_digest':wanted,'prior_matches':matches,'checked_receipt_candidates':checked,'unverified_candidates':unverified,'comparison':'same ISO delivery week, channel, sealed business body/archive content and verified receipt; outer filename and instance labels excluded'}
