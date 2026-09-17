"""Local complete customer-package audit and at most two additional test samples."""
from collections import Counter,defaultdict
from datetime import datetime
from decimal import Decimal,ROUND_HALF_UP
from pathlib import Path
import hashlib,io,json,re,zipfile
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle
from . import workflow_live_slow as slow,workflow_inputs as inputs,legacy_workflow as wf
from . import acceptance_delivery as delivery,workflow_io as transport

MAX_TOTAL_BYTES=512*1024*1024
def current_week(store):
    at=store._read('SELECT NOW(6) AS at')[0]['at'];return f'{at.isocalendar().year}-W{at.isocalendar().week:02d}'
def batch_record(store,week,scope=None):
    """Find a sample batch only when its immutable audit scope matches.

    The optional scope is the current department-to-generation map.  Calls
    without it retain the legacy lookup for read-only history displays; live
    preparation and coverage always pass the scope and therefore cannot reuse
    an unscoped or older-generation batch.
    """
    key=scope.get('key') if isinstance(scope,dict) else 'customer-sample-'+week.lower()+'-v1'
    rows=store.rows('cycles','customer-audit')
    existing=next((r for r in rows if r['cycle_id']==key),None)
    if existing:
        if isinstance(scope,dict) and cycle.payload(existing).get('scope')!=scope:raise ValueError('CUSTOMER_SAMPLE_SCOPE_MISMATCH')
        return existing,key
    if isinstance(scope,dict):return None,key
    legacy=next((r for r in rows if r['cycle_id']=='customer-sample-batch-v1'),None)
    if legacy and {p['week'] for p in cycle.payload(legacy).get('selected',[])}=={week}:return legacy,legacy['cycle_id']
    return None,key
def root():
    path=transport.private_root(delivery.runtime_home(base.profile()))/'wca'
    if path.is_symlink():raise ValueError('CUSTOMER_AUDIT_PATH_INVALID')
    path.mkdir(exist_ok=True);return path
def package_digest(package):return base.digest(package)

def _text(value):
    return '' if value is None else str(value).strip()

def _audit_token(row):
    """Stable business-row identity used for the independent plan reconciliation."""
    return (row.get('product_dept'),row.get('goods_no'),str(row.get('color') or ''),
            _text(row.get('customer_no')),_text(row.get('customer_name')),_text(row.get('sales_owner')),
            _text(row.get('account')),_text(row.get('exception')))

def _expected_customer_population(baseline,mapping):
    """Derive the expected population from sealed inputs, independently of contact_plan().

    This deliberately recomputes the expected owner/customer/exception sets from the
    baseline and customer mapping.  The generated plan is compared to this result; it
    is never used as its own oracle.
    """
    pool=defaultdict(Decimal)
    for row in baseline:
        try:rolls=Decimal(str(row.get('total_piece')))
        except Exception:raise ValueError('CUSTOMER_EXPECTED_ROLLS_INVALID')
        if not rolls.is_finite():raise ValueError('CUSTOMER_EXPECTED_ROLLS_INVALID')
        key=wf.customer_product_key(row)
        pool[key]+=rolls
    products_by_customer=mapping.get('productsByCustomer') or {}
    customer_info=mapping.get('customerInfo') or {}
    packages={};matched_pool=set();relations=set();audit_rows=Counter()
    for raw_cid,purchased in sorted(products_by_customer.items(),key=lambda item:_text(item[0])):
        cid=str(raw_cid);pairs={(p.get('whse_dept'),p.get('goods_no')) for p in (purchased or []) if isinstance(p,dict)}
        selected=[(dept,goods,color,rolls) for (dept,goods,color),rolls in pool.items() if (dept,goods) in pairs]
        matched_pool.update((dept,goods,color) for dept,goods,color,_ in selected)
        relations.update((cid,dept,goods,color) for dept,goods,color,_ in selected)
        info=customer_info.get(raw_cid) or customer_info.get(cid) or {}
        info=dict(info) if isinstance(info,dict) else {}
        assignment=wf.customer_assignment(info,mapping);owner=assignment['sales'];customer_no=assignment['customer_no'];name=assignment['name'];account=assignment['account'];employee=assignment['employee'];reason=assignment['reason']
        for dept,goods,color,_ in selected:
            audit_rows[(dept,goods,color,customer_no,name,owner,account,reason or '')]+=1
        if not selected or reason:continue
        region=assignment['region'];image_products=defaultdict(Decimal)
        for dept,goods,color,rolls in selected:image_products[(goods,color)]+=rolls
        customer={'customer_id':cid,'customer_no':customer_no,'customer_name':name,'products':[[goods,color,int(value.quantize(Decimal('1'),rounding=ROUND_HALF_UP))] for (goods,color),value in sorted(image_products.items())]}
        package=packages.setdefault(account,{'account':account,'sales_name':owner,'sales_names':[],'region':region,'customers':[]})
        if owner not in package['sales_names']:package['sales_names'].append(owner)
        package['customers'].append(customer)
    for dept,goods,color in sorted(set(pool)-matched_pool):
        audit_rows[(dept,goods,color,'','','','','No Matched Customer')]+=1
    for package in packages.values():
        package['sales_names'].sort(key=str.casefold);package['sales_name']=package['sales_names'][0]
        package['customers'].sort(key=lambda row:(_text(row.get('customer_no')),_text(row.get('customer_name')), _text(row.get('customer_id'))))
    return {'packages':packages,'relations':relations,'audit_rows':audit_rows,'matched_pool':matched_pool}
def audit_data(row):
    value=cycle.payload(row)
    if value.get('kind')!='customer_audit_pointer':return value
    path=Path(value['artifact_path'])
    if path.is_symlink() or not path.resolve().is_relative_to(live.root().resolve()):raise ValueError('CUSTOMER_AUDIT_POINTER_INVALID')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=value['sha256']:raise ValueError('CUSTOMER_AUDIT_ARTIFACT_CHANGED')
    return json.loads(raw.decode('utf-8'))

def _generation(row,data):
    summary=data.get('summary') or {}
    value=summary.get('generation')
    if value is not None:
        if type(value) is not int or not 0<=value<=999:raise ValueError('CUSTOMER_AUDIT_GENERATION_INVALID')
        return value
    scope=summary.get('scope') or row.get('cycle_id') or ''
    match=re.search(r'-g(\d+)$',str(scope),re.I)
    generation=int(match.group(1)) if match else -1
    if generation>999:raise ValueError('CUSTOMER_AUDIT_GENERATION_INVALID')
    return generation

def _current_audit_records(store,week):
    """Return one latest audit record per department and keep older records separate."""
    grouped={};history=[]
    for row in store.rows('cycles','customer-audit'):
        if not str(row.get('cycle_id','')).startswith('ca-'):continue
        data=audit_data(row);summary=data.get('summary') or {}
        if summary.get('week')!=week:continue
        department=summary.get('department')
        if department not in live.DEPARTMENTS:raise ValueError('CUSTOMER_AUDIT_DEPARTMENT_INVALID')
        item={'row':row,'data':data,'department':department,'generation':_generation(row,data)}
        previous=grouped.get(department)
        if previous is None or item['generation']>previous['generation']:
            if previous is not None:history.append(previous)
            grouped[department]=item
        elif item['generation']==previous['generation']:
            raise ValueError('CUSTOMER_AUDIT_CURRENT_GENERATION_AMBIGUOUS')
        else:history.append(item)
    # An audit is current only when it covers the latest frozen slow cycle.
    # A newer freeze without a corresponding audit must never silently fall
    # back to the older customer population.
    for department in live.DEPARTMENTS:
        latest=slow.latest(store,department,week)
        item=grouped.get(department)
        if latest is None:
            continue
        latest_data=cycle.payload(latest);latest_generation=latest_data.get('generation')
        if type(latest_generation) is not int or not 0<=latest_generation<=999:raise ValueError('CUSTOMER_AUDIT_LATEST_GENERATION_INVALID')
        if item is None:raise ValueError('CUSTOMER_AUDIT_LATEST_GENERATION_UNAUDITED')
        if item['generation']<latest_generation:raise ValueError('CUSTOMER_AUDIT_LATEST_GENERATION_UNAUDITED')
        if item['generation']>latest_generation:raise ValueError('CUSTOMER_AUDIT_GENERATION_AHEAD_OF_FREEZE')
    return [grouped[d] for d in live.DEPARTMENTS if d in grouped],history

def _audit_scope(current,week):
    """Derive a deterministic batch scope from current audited generations."""
    entries=[]
    for item in current:
        summary=item['data'].get('summary') or {}
        scope=str(summary.get('scope') or item['row'].get('cycle_id','').removeprefix('ca-'))
        entries.append({'department':item['department'],'week':week,'generation':item['generation'],'scope':scope})
    entries.sort(key=lambda value:value['department'])
    if not entries:raise ValueError('CUSTOMER_SAMPLE_SCOPE_EMPTY')
    departments={entry['department']:{'generation':entry['generation'],'scope':entry['scope']} for entry in entries}
    scope_digest=base.digest({'version':2,'week':week,'departments':departments})[:16]
    return {'version':2,'week':week,'departments':departments,'scope_digest':scope_digest,
            'key':'customer-sample-'+week.lower()+'-s'+scope_digest+'-v2'}

def _validate_sample_scope(data,scope):
    if data.get('scope')!=scope:raise ValueError('CUSTOMER_SAMPLE_SCOPE_MISMATCH')
    for package in data.get('selected',[]):
        if not isinstance(package,dict):raise ValueError('CUSTOMER_SAMPLE_PACKAGE_SCOPE_INVALID')
        department=package.get('department');expected=scope['departments'].get(department)
        if expected is None or package.get('week')!=scope['week'] or package.get('generation')!=expected['generation'] or package.get('audit_scope')!=expected['scope']:
            raise ValueError('CUSTOMER_SAMPLE_PACKAGE_SCOPE_INVALID')

def _manifest_binding(manifest,default_cycle_id):
    """Build the D-owned binding for a phase manifest without sending."""
    from . import workflow_delivery_review as review
    if not isinstance(manifest,dict):return None
    existing=manifest.get('delivery_binding')
    if delivery.validate_delivery_binding(existing):return existing
    notices=manifest.get('notices') or []
    if not notices:return None
    logical_id=notices[0].get('logical_id') if isinstance(notices[0],dict) else None
    cycle_id=logical_id if isinstance(logical_id,str) and logical_id.startswith(str(default_cycle_id)) else str(default_cycle_id)
    try:return review.binding_for_manifest(base.profile(),cycle_id,notices,manifest=manifest)
    except Exception:return None

def _receipt_binding(receipt,expected_binding=None):
    """Use D's binding API; acceptance must match the current phase/package."""
    from . import workflow_delivery_review as review
    if not isinstance(receipt,dict):
        return {'verified':False,'provider_accepted':False,'human_confirmed':'unknown','receipt_status':'not_attempted'}
    if not delivery.validate_delivery_binding(expected_binding):
        return {'verified':False,'provider_accepted':False,'human_confirmed':'unknown','receipt_status':receipt.get('status')}
    binding=receipt.get('delivery_binding')
    if (not isinstance(binding,dict) or expected_binding.get('business_scope') is None or
            binding.get('business_scope')!=expected_binding.get('business_scope') or
            binding.get('phase')!=expected_binding.get('phase')):
        return {'verified':False,'provider_accepted':False,'human_confirmed':'unknown','receipt_status':receipt.get('status')}
    try:verified=bool(review.accepted(receipt,expected_binding))
    except Exception:verified=False
    human=receipt.get('human_confirmed') if type(receipt.get('human_confirmed')) is bool else 'unknown'
    return {'verified':verified,'provider_accepted':verified,'human_confirmed':human,'receipt_status':receipt.get('status')}

def _prior_customer_delivery(data):
    selected=data.get('selected_packages') or []
    manifest=(data.get('phases') or {}).get('customer') or {}
    proof=_receipt_binding(manifest.get('receipt'),_manifest_binding(manifest,'ls-'+str(data.get('scope') or '')))
    declared=manifest.get('customer_package_digests') or (manifest.get('evidence') or {}).get('customer_package_digests') or data.get('selected_package_digests')
    actual=[package_digest(p.get('package') if isinstance(p,dict) and isinstance(p.get('package'),dict) else p) for p in selected if isinstance(p,dict)]
    if not isinstance(declared,list) or len(declared)!=len(actual) or set(declared)!=set(actual):
        proof={**proof,'verified':False,'provider_accepted':False,'binding_reason':'customer_package_digest_mismatch'}
    if not proof['provider_accepted']:
        return {'owners':set(),'digests':set(),'proof':proof}
    return {'owners':{p.get('account') for p in selected if isinstance(p,dict) and p.get('account')},
            'digests':{package_digest(p) for p in selected if isinstance(p,dict)},'proof':proof}

def _package_lifecycle(package,*,prepared,prior=None):
    prior=prior or {'owners':set(),'digests':set(),'proof':{'verified':False,'provider_accepted':False,'human_confirmed':'unknown','receipt_status':'not_attempted'}}
    digest=package_digest(package)
    accepted=package.get('account') in prior['owners']
    same=accepted and digest in prior['digests']
    return {'selected':True,'prepared':bool(prepared),'provider_accepted':accepted,
            'human_confirmed':prior['proof'].get('human_confirmed','unknown') if accepted else 'unknown',
            'receipt_bound':bool(accepted and prior['proof'].get('verified')),
            'receipt_status':prior['proof'].get('receipt_status'),'same_content_as_accepted':same,
            'delivery_state':'provider_accepted' if accepted else 'not_attempted'}

def _stored_lifecycle(package):
    life=package.get('lifecycle')
    if not isinstance(life,dict):
        return {'selected':True,'prepared':package.get('status')=='validated','provider_accepted':False,
                'human_confirmed':'unknown','receipt_bound':False,'same_content_as_accepted':False,
                'delivery_state':'legacy_unverified',
                'legacy_provider_accepted_claim':package.get('previously_sampled_owner') is True,
                'legacy_same_content_claim':package.get('previously_sampled_identical') is True,
                'legacy_claim_unverified':True}
    accepted=life.get('provider_accepted') is True and life.get('receipt_bound') is True
    return {**life,'provider_accepted':accepted,'receipt_bound':bool(accepted),
            'legacy_provider_accepted_claim':False,'legacy_same_content_claim':False,
            'legacy_claim_unverified':False}
def persist_audit(store,key,data):
    name=key+'-full-'+base.digest(data)[:16]+'.json';live.save(name,data);path=live.root()/name
    pointer={'kind':'customer_audit_pointer','summary':data['summary'],'artifact_path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    with store.transaction():store.cycle(key,'customer-audit','observed',pointer)
def verify_plan(baseline,mapping,plan):
    """Reconcile a generated plan against independently derived population evidence."""
    pool=defaultdict(Decimal)
    for row in baseline:pool[wf.customer_product_key(row)]+=Decimal(str(row['total_piece']))
    population=_expected_customer_population(baseline,mapping)
    seen=set();customers=0;product_rows=0
    for package in plan['sales_packages']:
        if package['account'] in seen:raise ValueError('DUPLICATE_PACKAGE_OWNER')
        seen.add(package['account']);customer_ids=set()
        for customer in package['customers']:
            cid=customer.get('customer_id');info=(mapping.get('customerInfo') or {}).get(cid)
            if info is None:info=(mapping.get('customerInfo') or {}).get(str(cid))
            if not isinstance(info,dict):raise ValueError('CUSTOMER_IDENTITY_MISSING')
            assignment=wf.customer_assignment(info,mapping);owner=assignment['sales'];employee=assignment['employee']
            normalized_cid=_text(cid)
            if normalized_cid in customer_ids:raise ValueError('DUPLICATE_CUSTOMER_CARD')
            customer_ids.add(normalized_cid)
            if assignment['reason']:raise ValueError('INELIGIBLE_CUSTOMER_IN_PACKAGE')
            if package['account']!=assignment['account'] or str(employee.get('wecom_account') or '').strip()!=package['account'] or str(employee.get('region') or '').strip()!=package['region']:raise ValueError('PACKAGE_OWNER_RECONCILIATION_FAILED')
            if customer['customer_no']!=assignment['customer_no'] or customer['customer_name']!=assignment['name']:raise ValueError('CUSTOMER_LABEL_RECONCILIATION_FAILED')
            purchased={(p.get('whse_dept'),p.get('goods_no')) for p in (mapping.get('productsByCustomer') or {}).get(cid, (mapping.get('productsByCustomer') or {}).get(str(cid), []))}
            product_rolls=defaultdict(Decimal)
            for (dept,goods,color),rolls in pool.items():
                if (dept,goods) in purchased:product_rolls[(goods,color)]+=rolls
            expected_rows=[[g,c,int(n.quantize(Decimal('1'),rounding=ROUND_HALF_UP))] for (g,c),n in sorted(product_rolls.items())]
            if expected_rows!=customer['products'] or not expected_rows:raise ValueError('CUSTOMER_PRODUCTS_RECONCILIATION_FAILED')
            customers+=1;product_rows+=len(expected_rows)
    expected_accounts=set(population['packages']);actual_accounts=set(seen)
    if actual_accounts!=expected_accounts:raise ValueError('CUSTOMER_PACKAGE_POPULATION_MISMATCH')
    for account in expected_accounts:
        expected_package=population['packages'][account]
        actual_package=next(p for p in plan['sales_packages'] if p['account']==account)
        expected_ids={_text(c['customer_id']) for c in expected_package['customers']}
        actual_ids={_text(c.get('customer_id')) for c in actual_package.get('customers',[])}
        if actual_ids!=expected_ids:raise ValueError('CUSTOMER_CARD_POPULATION_MISMATCH')
        if (actual_package.get('region'),actual_package.get('sales_name'),actual_package.get('sales_names'))!=(expected_package['region'],expected_package['sales_name'],expected_package['sales_names']):
            raise ValueError('PACKAGE_METADATA_RECONCILIATION_FAILED')
    actual_audit=Counter(_audit_token(row) for row in (plan.get('audit_rows') or []))
    if actual_audit!=population['audit_rows']:raise ValueError('CUSTOMER_EXCEPTION_POPULATION_MISMATCH')
    return {'owners':len(actual_accounts),'customer_cards':customers,'display_product_rows':product_rows,
            'expected_owners':len(expected_accounts),'expected_customer_cards':sum(len(p['customers']) for p in population['packages'].values()),
            'expected_audit_rows':sum(population['audit_rows'].values()),'actual_audit_rows':sum(actual_audit.values()),
            'expected_relations':len(population['relations']),'expected_unmatched_pool_rows':len(set(pool)-population['matched_pool']),
            'population_reconciled':True,'roll_rounding':'sum exact source rolls, then HALF_UP integer as legacy contract'}
def inspect_zip(path,package):
    from PIL import Image
    names=[wf.safe_name(c['customer_no'])+'_'+wf.safe_name(c['customer_name'])+'.png' for c in package['customers']]
    if len({n.casefold() for n in names})!=len(names):raise ValueError('CUSTOMER_ARCHIVE_NAME_COLLISION')
    size=path.stat().st_size
    if not 5<=size<=wf.policy()['customer_artifacts']['max_zip_bytes']:raise ValueError('CUSTOMER_ARCHIVE_SIZE_INVALID')
    dimensions=[]
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None or sorted(archive.namelist())!=sorted(names):raise ValueError('CUSTOMER_ARCHIVE_MEMBERSHIP_MISMATCH')
        for name in names:
            with Image.open(io.BytesIO(archive.read(name))) as image:
                image.load()
                if image.format!='PNG' or image.width<=0 or image.height<=0:raise ValueError('CUSTOMER_CARD_IMAGE_INVALID')
                dimensions.append({'width':image.width,'height':image.height})
    return {'bytes':size,'png_count':len(names),'max_image_height':max(d['height'] for d in dimensions),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'all_pngs_decoded':True}
def run():
    store=live.Store();summary=[];total_bytes=0
    try:
        with live.lock(store,'customer-audit'):
            at=store._read('SELECT NOW(6) AS at')[0]['at'];week=f'{at.isocalendar().year}-W{at.isocalendar().week:02d}'
            for department in live.DEPARTMENTS:
                prior=slow.latest(store,department,week)
                if not prior:raise ValueError('CUSTOMER_AUDIT_BASELINE_REQUIRED')
                data=cycle.payload(prior);key='ca-'+data['scope']
                existing=next((r for r in store.rows('cycles') if r['cycle_id']==key),None)
                if existing:
                    cached=audit_data(existing);summary.append(cached['summary']);total_bytes+=cached['summary']['archive_bytes'];continue
                baseline=cycle.clean(store.rows('slow_baseline',data['scope']))
                if base.digest(base.normalized('slow_baseline',baseline))!=data['freeze_digest']:raise ValueError('CUSTOMER_AUDIT_BASELINE_CHANGED')
                with base.tools._ConsistentSnapshotExecutor(deadline_at=base.tools._call_deadline(None)) as db:
                    mapping=inputs.customer_mapping(db,[(r['goods_no'],r['whse_dept']) for r in baseline]);marker=db.marker
                plan=wf.contact_plan(baseline,mapping);checks=verify_plan(baseline,mapping,plan)
                prior=_prior_customer_delivery(data)
                audit_root=root();folder=audit_root/data['scope']
                if folder.is_symlink() or not folder.resolve().is_relative_to(audit_root.resolve()):raise ValueError('CUSTOMER_AUDIT_SCOPE_PATH_INVALID')
                folder.mkdir(parents=True,exist_ok=True);packages=[]
                for index,package in enumerate(plan['sales_packages']):
                    artifact_folder=folder/('p'+package_digest(package)[:12])
                    if artifact_folder.is_symlink() or (artifact_folder.exists() and not artifact_folder.is_dir()):raise ValueError('CUSTOMER_AUDIT_ARTIFACT_PATH_INVALID')
                    artifact_folder.mkdir(exist_ok=True)
                    manifest_path=artifact_folder/'artifact.json'
                    try:
                        if manifest_path.is_symlink():raise ValueError('CUSTOMER_AUDIT_ARTIFACT_PATH_INVALID')
                        expected_digest=package_digest(package);regenerate=True;saved=None
                        if manifest_path.exists():
                            saved=json.loads(manifest_path.read_text(encoding='utf-8'))
                            if saved.get('package_digest')!=expected_digest:raise ValueError('INCOMPLETE_AUDIT_PACKAGE_SOURCE_CHANGED')
                            if saved.get('status')=='blocked':raise wf.WorkflowError(saved['code'])
                            if saved.get('archive'):
                                archive=Path(saved['archive'])
                                if archive.is_symlink() or not archive.resolve().is_relative_to(artifact_folder.resolve()):raise ValueError('CUSTOMER_ARCHIVE_PATH_INVALID')
                                if archive.is_file():artifact=inspect_zip(archive,package);regenerate=False
                        if regenerate:
                            archive=wf.customer_zip(package,artifact_folder,week);artifact=inspect_zip(archive,package)
                            manifest={'archive':str(archive),'package_digest':expected_digest,'inspection':artifact}
                            if isinstance(saved,dict) and saved.get('status')=='blocked':manifest['recovered_from']={'status':'blocked','code':saved.get('code')}
                            base.operations._atomic(manifest_path,manifest)
                    except Exception as exc:
                        code=str(exc) if isinstance(exc,wf.WorkflowError) else type(exc).__name__
                        if not manifest_path.exists():base.operations._atomic(manifest_path,{'status':'blocked','code':code,'package_digest':package_digest(package)})
                        life=_package_lifecycle(package,prepared=False,prior=prior)
                        packages.append({'index':index,'package':package,'package_digest':package_digest(package),'status':'blocked','code':code,'lifecycle':life,'previously_sampled_owner':life['provider_accepted'],'previously_sampled_identical':life['same_content_as_accepted']})
                        continue
                    total_bytes+=artifact['bytes']
                    if total_bytes>MAX_TOTAL_BYTES:raise ValueError('CUSTOMER_AUDIT_TOTAL_BYTES_BUDGET_EXCEEDED')
                    life=_package_lifecycle(package,prepared=True,prior=prior)
                    packages.append({'index':index,'status':'validated','package':package,'package_digest':package_digest(package),'archive':str(archive),'inspection':artifact,'customer_count':len(package['customers']),'max_products_per_customer':max(len(c['products']) for c in package['customers']),'lifecycle':life,'previously_sampled_owner':life['provider_accepted'],'previously_sampled_identical':life['same_content_as_accepted']})
                counts=Counter(r.get('exception') or 'matched' for r in plan['audit_rows'])
                stats={'department':department,'week':week,'generation':data.get('generation'),'scope':data.get('scope'),'frozen_at':data['frozen_at'],'mapping_window':mapping.get('window'),'source_snapshot':marker,**checks,'archive_bytes':sum(p.get('inspection',{}).get('bytes',0) for p in packages),'validated_packages':sum(p['status']=='validated' for p in packages),'blocked_packages':sum(p['status']=='blocked' for p in packages),'previously_sampled_owners':sum(_stored_lifecycle(p)['provider_accepted'] for p in packages),'previously_sampled_identical':sum(_stored_lifecycle(p)['same_content_as_accepted'] for p in packages),'provider_accepted_packages':sum(_stored_lifecycle(p)['provider_accepted'] for p in packages),'human_confirmed_packages':sum(_stored_lifecycle(p)['human_confirmed'] is True for p in packages),'prior_customer_receipt_verified':prior['proof']['verified'],'audit_reasons':dict(counts),'all_packages_locally_checked':True,'independent_population_reconciled':checks['population_reconciled'],'independent_population_unverified':False,'sent':False}
                result={'summary':stats,'packages':packages,'audit_rows':plan['audit_rows'],'baseline_digest':data['freeze_digest']}
                persist_audit(store,key,result)
                live.save(key+'-summary.json',stats);summary.append(stats)
            result={'status':'local_audit_complete','departments':summary,'owners':sum(d['owners'] for d in summary),'customer_cards':sum(d['customer_cards'] for d in summary),'archive_bytes':total_bytes,'real_messages_sent':0}
            live.save('customer-package-audit.json',result);return result
    finally:store.close()
def prepare_samples():
    store=live.Store()
    try:
        with live.lock(store,'customer-audit'):
            week=current_week(store)
            current,_history=_current_audit_records(store,week);scope=_audit_scope(current,week)
            if len(current)!=len(live.DEPARTMENTS):raise ValueError('COMPLETE_LOCAL_AUDIT_REQUIRED')
            existing,key=batch_record(store,week,scope)
            if existing:return {'status':'existing_sample_batch_preserved','evidence':str(live.root()/(key+'.json'))}
            candidates=[];historical_accepted_excluded=0
            for item in current:
                for package in item['data']['packages']:
                    lifecycle=_stored_lifecycle(package)
                    # A legacy accepted marker is retained as historical evidence,
                    # but it must not trigger an automatic resend.  Only packages
                    # with no accepted claim enter a new deliberate sample.
                    if lifecycle['same_content_as_accepted'] or lifecycle['legacy_provider_accepted_claim']:
                        historical_accepted_excluded+=1;continue
                    if package['status']=='validated':
                        candidates.append({**package,'lifecycle':lifecycle,'department':item['department'],'week':week,'generation':item['generation'],'audit_scope':(item['data'].get('summary') or {}).get('scope') or item['row'].get('cycle_id','').removeprefix('ca-')})
            unique={}
            for candidate in candidates:unique.setdefault(candidate['package_digest'],candidate)
            candidates=list(unique.values())
            if len(candidates)<2:
                if historical_accepted_excluded:raise ValueError('ADDITIONAL_SAMPLE_HISTORICAL_ACCEPTANCE_REVIEW_REQUIRED')
                raise ValueError('ADDITIONAL_SAMPLE_CANDIDATES_INSUFFICIENT')
            repaired=[p for p in candidates if p.get('prior_artifact_error')]
            first=max(repaired or candidates,key=lambda p:(p['max_products_per_customer'],p['inspection']['bytes']))
            second=max((p for p in candidates if p['package_digest']!=first['package_digest']),key=lambda p:(p['customer_count'],p['inspection']['bytes']))
            selected=[first,second];notices=[]
            for index,p in enumerate(selected):
                inspect_zip(Path(p['archive']),p['package'])
                notices.append({'logical_id':'additional-customer-'+str(p['audit_scope']).lower()+'-'+str(index),'channel':'private','role':'额外客户包抽验；'+p['department']+'；多客户/多产品；非原销售或客户触达','body':wf.customer_package_message(p['package'],p['week']),'attachments':[p['archive']]})
            result={'scope':scope,'selected':selected,'notices':notices,'notice_digest':base.digest(notices),'purpose':'two packages without a receipt-bound provider acceptance: prefer repaired long-cell/multi-product case and most customers','sent':False,'human_confirmed':'unknown','receipts':[],'receipt_bindings':[]}
            with store.transaction():store.cycle(key,'customer-audit','planned',result)
            live.save(key+'.json',result)
            return {'status':'prepared_not_sent','scope':scope,'samples':[{'department':p['department'],'customers':p['customer_count'],'max_products_per_customer':p['max_products_per_customer'],'bytes':p['inspection']['bytes']} for p in selected],'notices':2,'preview':str(live.root()/(key+'.json'))}
    finally:store.close()
def repair_artifacts():
    store=live.Store();repairs=[]
    try:
        with live.lock(store,'customer-audit'):
            for row in store.rows('cycles','customer-audit'):
                if not row['cycle_id'].startswith('ca-'):continue
                data=audit_data(row);changed=False
                for item in data['packages']:
                    if item['status']!='blocked' or item['code']!='CUSTOMER_IMAGE_CELL_TOO_LONG':continue
                    folder=root()/row['cycle_id'].removeprefix('ca-')/('v2-'+item['package_digest'][:12]);folder.mkdir(exist_ok=True)
                    archive=folder/('sales_'+wf.account_token(item['package']['account'])[:12]+'_'+data['summary']['week']+'.zip')
                    if not archive.exists():archive=wf.customer_zip(item['package'],folder,data['summary']['week'])
                    inspection=inspect_zip(archive,item['package'])
                    item.update(status='validated',prior_artifact_error=item['code'],archive=str(archive),inspection=inspection,customer_count=len(item['package']['customers']),max_products_per_customer=max(len(c['products']) for c in item['package']['customers']),renderer='wrapped_cells_v2')
                    repairs.append({'department':data['summary']['department'],'package_digest':item['package_digest'],'prior_code':item['code'],'png_count':inspection['png_count'],'bytes':inspection['bytes']});changed=True
                if changed:
                    stats=data['summary'];stats.update(validated_packages=sum(p['status']=='validated' for p in data['packages']),blocked_packages=sum(p['status']=='blocked' for p in data['packages']),archive_bytes=sum(p.get('inspection',{}).get('bytes',0) for p in data['packages']))
                    persist_audit(store,row['cycle_id'],data)
                    live.save(row['cycle_id']+'-summary.json',stats)
            result={'status':'repaired_local_artifacts','repairs':repairs,'original_failed_artifacts_preserved':True,'production_queries':0,'sent':False};live.save('customer-artifact-repairs.json',result);return result
    finally:store.close()
def send_samples():
    store=live.Store()
    try:
        with live.lock(store,'customer-audit'):
            week=current_week(store);current,_history=_current_audit_records(store,week);scope=_audit_scope(current,week)
            row,key=batch_record(store,week,scope)
            if not row:raise ValueError('ADDITIONAL_SAMPLE_PREPARATION_REQUIRED')
            if row['status']=='committed':return {'status':'already_sent_no_resend'}
            if row['status'] in ('unknown','sending'):raise ValueError('ADDITIONAL_SAMPLE_UNKNOWN_REVIEW_REQUIRED')
            data=cycle.payload(row)
            _validate_sample_scope(data,scope)
            if base.digest(data['notices'])!=data['notice_digest']:raise ValueError('ADDITIONAL_SAMPLE_NOTICE_CHANGED')
            for p in data['selected']:
                checked=inspect_zip(Path(p['archive']),p['package'])
                if checked['sha256']!=p['inspection']['sha256']:raise ValueError('ADDITIONAL_SAMPLE_ARCHIVE_CHANGED')
            from . import workflow_delivery_review as review
            if 'prior_delivery_reviews' not in data:
                reviews=[]
                for p,notice in zip(data['selected'],data['notices']):
                    scope_id=str(p['audit_scope']).lower()
                    if not re.fullmatch(r'[a-z0-9-]{1,48}',scope_id):raise ValueError('CUSTOMER_SAMPLE_PACKAGE_SCOPE_INVALID')
                    manifest={'notices':[notice],'business_scope':p['department'],'files':{str(notice['attachments'][0]):{'sha256':p['inspection']['sha256']}}}
                    reviews.append(review.review(store,'ca-'+scope_id,manifest))
                data['prior_delivery_reviews']=reviews
                cycle.persist(store,key,'customer-audit',row['status'],data)
            cycle.persist(store,key,'customer-audit','sending',data)
            receipts=[];bindings=[];new_sends=0;reused=0
            for index,(package,notice,review_result) in enumerate(zip(data['selected'],data['notices'],data['prior_delivery_reviews'])):
                matches=review_result.get('prior_matches') or []
                if matches:
                    match=matches[0];receipt={**match['receipt'],'reused_prior_delivery':True,'matched_prior_source':match['source'],'new_components':0};reused+=1
                else:
                    scope_id=str(package['audit_scope']).lower()
                    dispatch_key='ls-'+scope_id+'-customer-extra-'+str(index)
                    try:receipt=cycle.dispatch(dispatch_key,[notice],'planned')
                    except Exception as exc:
                        data['receipts']=receipts;data['receipt_bindings']=bindings;data['sent']=new_sends>0
                        cycle.persist(store,key,'customer-audit','failed' if str(exc)=='DELIVERY_COMPONENT_FAILED' else 'unknown',data);raise
                    new_sends+=1
                expected_binding=review_result.get('delivery_binding') if isinstance(review_result,dict) else None
                if matches and not expected_binding:expected_binding=matches[0].get('delivery_binding')
                if not matches:expected_binding=_sample_binding(package,notice,index)
                proof=_receipt_binding(receipt,expected_binding);receipts.append(receipt);bindings.append(proof)
                life=_stored_lifecycle(package);life.update(selected=True,prepared=True,provider_accepted=proof['provider_accepted'],receipt_bound=proof['verified'],same_content_as_accepted=proof['provider_accepted'],receipt_status=proof.get('receipt_status'),human_confirmed=proof.get('human_confirmed','unknown'),delivery_state='provider_accepted' if proof['provider_accepted'] else 'unknown');package['lifecycle']=life
                if proof['provider_accepted']:package['previously_sampled_owner']=True;package['previously_sampled_identical']=True
                if not proof['provider_accepted']:
                    data['receipts']=receipts;data['receipt_bindings']=bindings;data['sent']=new_sends>0;data['human_confirmed']='unknown';cycle.persist(store,key,'customer-audit','unknown',data);raise ValueError('ADDITIONAL_SAMPLE_RECEIPT_UNBOUND')
            data['receipts']=receipts;data['receipt_bindings']=bindings;data['receipt']=receipts[0] if len(receipts)==1 else None;data['receipt_binding']=bindings[0] if len(bindings)==1 else None;data['sent']=new_sends>0;data['reused_prior_delivery']=reused>0;data['human_confirmed']=bindings[0].get('human_confirmed','unknown') if len(bindings)==1 else 'unknown';cycle.persist(store,key,'customer-audit','committed',data)
            live.save('customer-sample-send-result.json',{'receipts':receipts,'scope':scope,'new_sends':new_sends,'reused_prior_deliveries':reused});return {'status':'sample_batch_completed','receipts':receipts,'scope':scope,'sent':new_sends>0,'new_sends':new_sends,'reused_prior_deliveries':reused}
    finally:store.close()

def _sample_receipt_pairs(sample):
    """Return selected packages paired with their own receipt, if present."""
    selected=sample.get('selected') or []
    receipts=sample.get('receipts')
    if isinstance(receipts,list) and len(receipts)==len(selected):
        return list(zip(selected,receipts))
    receipt=sample.get('receipt')
    if len(selected)==1 and isinstance(receipt,dict):return [(selected[0],receipt)]
    return []

def _sample_binding(package,notice,index):
    """Build the expected D binding for one selected sample package."""
    from . import workflow_delivery_review as review
    scope_id=str(package.get('audit_scope') or '').lower()
    if not re.fullmatch(r'[a-z0-9-]{1,48}',scope_id):return None
    attachments=notice.get('attachments') or []
    digest=package.get('inspection',{}).get('sha256') if isinstance(package.get('inspection'),dict) else None
    if len(attachments)!=1 or not isinstance(digest,str):return None
    manifest={'notices':[notice],'business_scope':package.get('department'),'files':{str(attachments[0]):{'sha256':digest}}}
    try:return review.binding_for_manifest(base.profile(),'ls-'+scope_id+'-customer-extra-'+str(index),[notice],manifest=manifest)
    except Exception:return None

def summarize_records(current,history,week,*,scope=None,sample=None,sample_committed=False,legacy_batch=None):
    """Pure coverage projection for injected audit/workflow/batch records.

    It performs no database or state writes.  Receipt verification is delegated
    to the shared read-only binding verifier, so callers can replace that
    verifier for isolated tests without running the customer-coverage entry.
    """
    current=list(current or []);history=list(history or []);packages=[];departments=[];historical_departments=[];historical_packages=[]
    for item in current:
        row=item['row'];data=item['data'];packages.extend(data.get('packages',[]));summary=dict(data.get('summary') or {});summary.setdefault('generation',item['generation']);summary.setdefault('scope',row.get('cycle_id','').removeprefix('ca-'));summary['independent_population_unverified']=summary.get('independent_population_reconciled') is not True;departments.append(summary)
    for item in history:
        row=item['row'];data=item['data'];summary=dict(data.get('summary') or {});summary.setdefault('generation',item['generation']);summary.setdefault('scope',row.get('cycle_id','').removeprefix('ca-'));summary['independent_population_unverified']=summary.get('independent_population_reconciled') is not True;historical_departments.append(summary);historical_packages.extend(data.get('packages',[]))
    if scope is None and current:scope=_audit_scope(current,week)
    sample=sample if isinstance(sample,dict) else {};sample_scope_valid=bool(sample and scope)
    if sample_scope_valid:
        try:_validate_sample_scope(sample,scope)
        except ValueError:sample_scope_valid=False
    added=[];sample_proofs=[]
    if sample_scope_valid and sample_committed:
        for index,(package,receipt) in enumerate(_sample_receipt_pairs(sample)):
            notices=sample.get('notices') or [];notice=notices[index] if index<len(notices) and isinstance(notices[index],dict) else {}
            proof=_receipt_binding(receipt,_sample_binding(package,notice,index));sample_proofs.append(proof)
            if proof['provider_accepted']:added.append(package)
    sample_proof={'verified':bool(sample_proofs) and all(p['verified'] for p in sample_proofs),'provider_accepted':bool(sample_proofs) and all(p['provider_accepted'] for p in sample_proofs),'human_confirmed':sample_proofs[0].get('human_confirmed','unknown') if len(sample_proofs)==1 else 'unknown'}
    lifecycle=[_stored_lifecycle(p) for p in packages];history_lifecycle=[_stored_lifecycle(p) for p in historical_packages];all_lifecycle=lifecycle+history_lifecycle
    legacy_owner_claims=sum(l['legacy_provider_accepted_claim'] for l in all_lifecycle);legacy_content_claims=sum(l['legacy_same_content_claim'] for l in all_lifecycle)
    if not legacy_owner_claims:legacy_owner_claims=sum(int(d.get('previously_sampled_owners') or 0) for d in departments+historical_departments if d.get('independent_population_unverified') is True)
    if not legacy_content_claims:legacy_content_claims=sum(int(d.get('previously_sampled_identical') or 0) for d in departments+historical_departments if d.get('independent_population_unverified') is True)
    legacy_batch=legacy_batch if isinstance(legacy_batch,dict) else {};legacy_payload=legacy_batch.get('payload') if isinstance(legacy_batch.get('payload'),dict) else {}
    # The old two-package sample was recorded outside the audit package rows.
    # Keep its explicit historical claim only when the old batch is committed,
    # marked sent, and carries an accepted status; never promote it to v2 proof.
    legacy_batch_owner=0;legacy_batch_content=0
    old_receipt=legacy_payload.get('receipt') if isinstance(legacy_payload.get('receipt'),dict) else {}
    old_accepted=legacy_payload.get('legacy_provider_accepted') is True or old_receipt.get('status') in ('provider_accepted','provider_accepted_not_human_read')
    if legacy_batch.get('status')=='committed' and legacy_payload.get('sent') is True and old_accepted:
        seen_legacy=set()
        for package in legacy_payload.get('selected',[]):
            if not isinstance(package,dict):continue
            source=package.get('package') if isinstance(package.get('package'),dict) else package;token=package_digest(source)
            if token in seen_legacy:continue
            seen_legacy.add(token)
            legacy_batch_owner+=1;legacy_batch_content+=1
    legacy_owner_claims+=legacy_batch_owner;legacy_content_claims+=legacy_batch_content
    verified_logical=sum(l['provider_accepted'] for l in lifecycle)+len(added);verified_content=sum(l['same_content_as_accepted'] for l in lifecycle)+len(added)
    return {'status':'local_package_coverage','coverage_schema':'customer-package-coverage/v2','coverage_meaning':{'logical_package_delivery_coverage':'verified provider-accepted receipt bound to the current department/generation scope','current_content_delivery_coverage':'verified provider-accepted receipt with the same content digest bound to the current scope','recorded_history_*':'legacy previously_sampled_* counts retained for comparison only; they are not receipt proof'},'departments':departments,'historical_departments':historical_departments,'current_scope':scope,'current_generation_by_department':{d['department']:d.get('generation') for d in departments},'department_owner_packages':len(packages),'historical_department_owner_packages':len(historical_packages),'validated_packages':sum(p.get('status')=='validated' for p in packages),'blocked_packages':sum(p.get('status')=='blocked' for p in packages),'expected_customer_cards':sum(d.get('customer_cards',0) for d in departments),'historical_customer_cards':sum(d.get('customer_cards',0) for d in historical_departments),'decoded_pngs':sum(p.get('inspection',{}).get('png_count',0) for p in packages),'archive_bytes':sum(p.get('inspection',{}).get('bytes',0) for p in packages),'historical_archive_bytes':sum(p.get('inspection',{}).get('bytes',0) for p in historical_packages),'historical_sampled_owner_packages':legacy_owner_claims,'historical_same_content_packages':legacy_content_claims,'recorded_history_logical_package_delivery_coverage':legacy_owner_claims,'recorded_history_current_content_delivery_coverage':legacy_content_claims,'legacy_unverified_owner_packages':legacy_owner_claims,'legacy_unverified_content_packages':legacy_content_claims,'receipt_bound_provider_packages':sum(l['provider_accepted'] for l in lifecycle),'human_confirmed_packages':sum(l['human_confirmed'] is True for l in lifecycle),'additional_sent_packages':len(added),'additional_sent_receipt_verified':sample_proof['provider_accepted'],'additional_sample_receipt_verified_count':sum(p['provider_accepted'] for p in sample_proofs),'additional_new_sends':sample.get('new_sends',0) if sample_scope_valid else 0,'logical_package_delivery_coverage':verified_logical,'current_content_delivery_coverage':verified_content,'verified_current_scope_logical_package_delivery_coverage':verified_logical,'verified_current_scope_content_delivery_coverage':verified_content,'historical_receipt_bound_packages':sum(l['provider_accepted'] for l in history_lifecycle),'independent_population_unverified':any(d.get('independent_population_unverified') for d in departments),'historical_population_unverified':any(d.get('independent_population_unverified') for d in historical_departments),'sample_scope_valid':sample_scope_valid,'legacy_sample_batch_preserved':bool(legacy_batch.get('cycle_id')),'legacy_sample_batch_cycle':legacy_batch.get('cycle_id'),'legacy_sample_batch_status':legacy_batch.get('status'),'legacy_sample_batch_recorded_owner_packages':legacy_batch_owner,'legacy_sample_batch_recorded_content_packages':legacy_batch_content,'actual_customer_delivery_claimed':False,'source_requeried':False,'large_audit_payloads':'immutable private JSON with digest pointers in test ledger'}

def compact_and_summarize():
    store=live.Store()
    try:
        with live.lock(store,'customer-audit'):
            week=current_week(store);current,history=_current_audit_records(store,week)
            for item in current:
                row=item['row'];data=item['data']
                if cycle.payload(row).get('kind')!='customer_audit_pointer':persist_audit(store,row['cycle_id'],data)
            scope=_audit_scope(current,week) if current else None
            sample_row,_=batch_record(store,week,scope) if scope else (None,None)
            sample=cycle.payload(sample_row) if sample_row else {}
            legacy_row,_=batch_record(store,week)
            result=summarize_records(current,history,week,scope=scope,sample=sample,sample_committed=bool(sample_row and sample_row.get('status')=='committed'),legacy_batch={'cycle_id':legacy_row.get('cycle_id'),'status':legacy_row.get('status'),'payload':cycle.payload(legacy_row)} if legacy_row else None)
            live.save('customer-package-coverage.json',result);return result
    finally:store.close()
