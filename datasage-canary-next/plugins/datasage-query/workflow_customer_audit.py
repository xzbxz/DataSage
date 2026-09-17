"""Local complete customer-package audit and at most two additional test samples."""
from collections import Counter,defaultdict
from datetime import datetime
from decimal import Decimal,ROUND_HALF_UP
from pathlib import Path
import hashlib,io,json,zipfile
from . import workflow_live_store as live,workflow_storage as base,workflow_cycle as cycle
from . import workflow_live_slow as slow,workflow_inputs as inputs,legacy_workflow as wf
from . import acceptance_delivery as delivery,workflow_io as transport

MAX_TOTAL_BYTES=512*1024*1024
def current_week(store):
    at=store._read('SELECT NOW(6) AS at')[0]['at'];return f'{at.isocalendar().year}-W{at.isocalendar().week:02d}'
def batch_record(store,week):
    key='customer-sample-'+week.lower()+'-v1'
    rows=store.rows('cycles','customer-audit')
    existing=next((r for r in rows if r['cycle_id']==key),None)
    if existing:return existing,key
    legacy=next((r for r in rows if r['cycle_id']=='customer-sample-batch-v1'),None)
    if legacy and {p['week'] for p in cycle.payload(legacy).get('selected',[])}=={week}:return legacy,legacy['cycle_id']
    return None,key
def root():
    path=transport.private_root(delivery.runtime_home(base.profile()))/'wca'
    if path.is_symlink():raise ValueError('CUSTOMER_AUDIT_PATH_INVALID')
    path.mkdir(exist_ok=True);return path
def package_digest(package):return base.digest(package)
def audit_data(row):
    value=cycle.payload(row)
    if value.get('kind')!='customer_audit_pointer':return value
    path=Path(value['artifact_path'])
    if path.is_symlink() or not path.resolve().is_relative_to(live.root().resolve()):raise ValueError('CUSTOMER_AUDIT_POINTER_INVALID')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=value['sha256']:raise ValueError('CUSTOMER_AUDIT_ARTIFACT_CHANGED')
    return json.loads(raw.decode('utf-8'))
def persist_audit(store,key,data):
    name=key+'-full-'+base.digest(data)[:16]+'.json';live.save(name,data);path=live.root()/name
    pointer={'kind':'customer_audit_pointer','summary':data['summary'],'artifact_path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    with store.transaction():store.cycle(key,'customer-audit','observed',pointer)
def verify_plan(baseline,mapping,plan):
    """Independent reconciliation of ownership and displayed roll aggregation."""
    pool=defaultdict(Decimal)
    for row in baseline:pool[(row['whse_dept'],row['goods_no'],str(row.get('attr_val') or ''))]+=Decimal(str(row['total_piece']))
    seen=set();customers=0;product_rows=0
    for package in plan['sales_packages']:
        if package['account'] in seen:raise ValueError('DUPLICATE_PACKAGE_OWNER')
        seen.add(package['account']);customer_ids=set()
        for customer in package['customers']:
            cid=customer['customer_id'];info=mapping['customerInfo'][cid];owner=str(info['sales']).strip();employee=mapping['employeeBySales'][owner]
            if cid in customer_ids:raise ValueError('DUPLICATE_CUSTOMER_CARD')
            customer_ids.add(cid)
            if package['account']!=str(mapping['wecomBySales'][owner]).strip() or str(employee['wecom_account']).strip()!=package['account'] or str(employee['region']).strip()!=package['region']:raise ValueError('PACKAGE_OWNER_RECONCILIATION_FAILED')
            if customer['customer_no']!=str(info['customer_no']).strip() or customer['customer_name']!=str(info['name']).strip():raise ValueError('CUSTOMER_LABEL_RECONCILIATION_FAILED')
            purchased={(p['whse_dept'],p['goods_no']) for p in mapping['productsByCustomer'][cid]}
            expected=defaultdict(Decimal)
            for (dept,goods,color),rolls in pool.items():
                if (dept,goods) in purchased:expected[(goods,color)]+=rolls
            expected_rows=[[g,c,int(n.quantize(Decimal('1'),rounding=ROUND_HALF_UP))] for (g,c),n in sorted(expected.items())]
            if expected_rows!=customer['products'] or not expected_rows:raise ValueError('CUSTOMER_PRODUCTS_RECONCILIATION_FAILED')
            customers+=1;product_rows+=len(expected_rows)
    return {'owners':len(seen),'customer_cards':customers,'display_product_rows':product_rows,'roll_rounding':'sum exact source rolls, then HALF_UP integer as legacy contract'}
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
                prior_digests={package_digest(p) for p in data.get('selected_packages',[])}
                prior_owners={p['account'] for p in data.get('selected_packages',[])}
                folder=root()/data['scope'];folder.mkdir(exist_ok=True);packages=[]
                for index,package in enumerate(plan['sales_packages']):
                    artifact_folder=folder/('p'+package_digest(package)[:12]);artifact_folder.mkdir(exist_ok=True)
                    manifest_path=artifact_folder/'artifact.json'
                    try:
                        if manifest_path.exists():
                            saved=json.loads(manifest_path.read_text(encoding='utf-8'))
                            if saved['package_digest']!=package_digest(package):raise ValueError('INCOMPLETE_AUDIT_PACKAGE_SOURCE_CHANGED')
                            if saved.get('status')=='blocked':raise wf.WorkflowError(saved['code'])
                            archive=Path(saved['archive']);artifact=inspect_zip(archive,package)
                        else:
                            archive=wf.customer_zip(package,artifact_folder,week);artifact=inspect_zip(archive,package)
                            base.operations._atomic(manifest_path,{'archive':str(archive),'package_digest':package_digest(package),'inspection':artifact})
                    except Exception as exc:
                        code=str(exc) if isinstance(exc,wf.WorkflowError) else type(exc).__name__
                        if not manifest_path.exists():base.operations._atomic(manifest_path,{'status':'blocked','code':code,'package_digest':package_digest(package)})
                        packages.append({'index':index,'package':package,'package_digest':package_digest(package),'status':'blocked','code':code,'previously_sampled_owner':package['account'] in prior_owners,'previously_sampled_identical':package_digest(package) in prior_digests})
                        continue
                    total_bytes+=artifact['bytes']
                    if total_bytes>MAX_TOTAL_BYTES:raise ValueError('CUSTOMER_AUDIT_TOTAL_BYTES_BUDGET_EXCEEDED')
                    packages.append({'index':index,'status':'validated','package':package,'package_digest':package_digest(package),'archive':str(archive),'inspection':artifact,'customer_count':len(package['customers']),'max_products_per_customer':max(len(c['products']) for c in package['customers']),'previously_sampled_owner':package['account'] in prior_owners,'previously_sampled_identical':package_digest(package) in prior_digests})
                counts=Counter(r.get('exception') or 'matched' for r in plan['audit_rows'])
                stats={'department':department,'week':week,'frozen_at':data['frozen_at'],'mapping_window':mapping.get('window'),'source_snapshot':marker,**checks,'archive_bytes':sum(p.get('inspection',{}).get('bytes',0) for p in packages),'validated_packages':sum(p['status']=='validated' for p in packages),'blocked_packages':sum(p['status']=='blocked' for p in packages),'previously_sampled_owners':sum(p['previously_sampled_owner'] for p in packages),'previously_sampled_identical':sum(p['previously_sampled_identical'] for p in packages),'audit_reasons':dict(counts),'all_packages_locally_checked':True,'sent':False}
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
            rows=[r for r in store.rows('cycles','customer-audit') if r['cycle_id'].startswith('ca-') and cycle.payload(r)['summary']['week']==week]
            if len(rows)!=8:raise ValueError('COMPLETE_LOCAL_AUDIT_REQUIRED')
            existing,key=batch_record(store,week)
            if existing:return {'status':'existing_sample_batch_preserved','evidence':str(live.root()/(key+'.json'))}
            data_rows=[audit_data(r) for r in rows]
            candidates=[{**p,'department':d['summary']['department'],'week':d['summary']['week']} for d in data_rows for p in d['packages'] if p['status']=='validated' and not p['previously_sampled_owner']]
            if len(candidates)<2:raise ValueError('ADDITIONAL_SAMPLE_CANDIDATES_INSUFFICIENT')
            repaired=[p for p in candidates if p.get('prior_artifact_error')]
            first=max(repaired or candidates,key=lambda p:(p['max_products_per_customer'],p['inspection']['bytes']))
            second=max((p for p in candidates if p['package_digest']!=first['package_digest']),key=lambda p:(p['customer_count'],p['inspection']['bytes']))
            selected=[first,second];notices=[]
            for index,p in enumerate(selected):
                inspect_zip(Path(p['archive']),p['package'])
                notices.append({'logical_id':'additional-customer-'+str(index),'channel':'private','role':'额外客户包抽验；'+p['department']+'；多客户/多产品；非原销售或客户触达','body':wf.customer_package_message(p['package'],p['week']),'attachments':[p['archive']]})
            result={'selected':selected,'notices':notices,'notice_digest':base.digest(notices),'purpose':'two previously unsampled packages: repaired long-cell/multi-product case and most customers','sent':False}
            with store.transaction():store.cycle(key,'customer-audit','planned',result)
            live.save(key+'.json',result)
            return {'status':'prepared_not_sent','samples':[{'department':p['department'],'customers':p['customer_count'],'max_products_per_customer':p['max_products_per_customer'],'bytes':p['inspection']['bytes']} for p in selected],'notices':2,'preview':str(live.root()/(key+'.json'))}
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
            week=current_week(store);row,key=batch_record(store,week)
            if not row:raise ValueError('ADDITIONAL_SAMPLE_PREPARATION_REQUIRED')
            if row['status']=='committed':return {'status':'already_sent_no_resend'}
            if row['status']=='unknown':raise ValueError('ADDITIONAL_SAMPLE_UNKNOWN_REVIEW_REQUIRED')
            data=cycle.payload(row)
            if base.digest(data['notices'])!=data['notice_digest']:raise ValueError('ADDITIONAL_SAMPLE_NOTICE_CHANGED')
            for p in data['selected']:
                checked=inspect_zip(Path(p['archive']),p['package'])
                if checked['sha256']!=p['inspection']['sha256']:raise ValueError('ADDITIONAL_SAMPLE_ARCHIVE_CHANGED')
            from . import workflow_delivery_review as review
            if 'prior_delivery_reviews' not in data:
                data['prior_delivery_reviews']=[review.review(store,'ca-'+p['department'].lower()+'-'+p['week'].lower(),{'notices':[notice]}) for p,notice in zip(data['selected'],data['notices'])]
                cycle.persist(store,key,'customer-audit',row['status'],data)
            if any(r['prior_matches'] for r in data['prior_delivery_reviews']):return {'status':'equivalent_prior_sample_found_no_new_send','sent':False}
            cycle.persist(store,key,'customer-audit','sending',data)
            try:receipt=cycle.dispatch('customer-extra-'+week.lower(),data['notices'],row['status'])
            except Exception as exc:
                cycle.persist(store,key,'customer-audit','failed' if str(exc)=='DELIVERY_COMPONENT_FAILED' else 'unknown',data);raise
            data['receipt']=receipt;data['sent']=True;cycle.persist(store,key,'customer-audit','committed',data)
            live.save('customer-sample-send-result.json',receipt);return receipt
    finally:store.close()
def compact_and_summarize():
    store=live.Store();packages=[];departments=[]
    try:
        with live.lock(store,'customer-audit'):
            week=current_week(store)
            rows=store.rows('cycles','customer-audit')
            for row in rows:
                if not row['cycle_id'].startswith('ca-'):continue
                data=audit_data(row)
                if data['summary']['week']!=week:continue
                if cycle.payload(row).get('kind')!='customer_audit_pointer':persist_audit(store,row['cycle_id'],data)
                packages.extend(data['packages']);departments.append(data['summary'])
            sample_row,_=batch_record(store,week);sample=cycle.payload(sample_row) if sample_row else {}
            added=sample.get('selected',[]) if sample.get('sent') else []
            result={'status':'local_package_coverage','departments':departments,'department_owner_packages':len(packages),'validated_packages':sum(p['status']=='validated' for p in packages),'blocked_packages':sum(p['status']=='blocked' for p in packages),'expected_customer_cards':sum(d['customer_cards'] for d in departments),'decoded_pngs':sum(p.get('inspection',{}).get('png_count',0) for p in packages),'archive_bytes':sum(p.get('inspection',{}).get('bytes',0) for p in packages),'historical_sampled_owner_packages':sum(p['previously_sampled_owner'] for p in packages),'historical_same_content_packages':sum(p['previously_sampled_identical'] for p in packages),'additional_sent_packages':len(added),'logical_package_delivery_coverage':sum(p['previously_sampled_owner'] for p in packages)+len(added),'current_content_delivery_coverage':sum(p['previously_sampled_identical'] for p in packages)+len(added),'actual_customer_delivery_claimed':False,'source_requeried':False,'large_audit_payloads':'immutable private JSON with digest pointers in test ledger'}
            live.save('customer-package-coverage.json',result);return result
    finally:store.close()
