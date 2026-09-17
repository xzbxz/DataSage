import unittest,importlib,copy
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib,json
import test_business_contracts as base
a=importlib.import_module(base.TEST_PACKAGE+'.workflow_customer_audit')
def sample():
    baseline=[{'whse_dept':'HCM','goods_no':'G1','attr_val':'R','total_piece':'1.4'},{'whse_dept':'HCM','goods_no':'G1','attr_val':'R','total_piece':'1.1'},{'whse_dept':'HCM','goods_no':'G2','attr_val':'B','total_piece':'4'},{'whse_dept':'HCM','goods_no':'G3','attr_val':'Z','total_piece':'7'}]
    mapping={'productsByCustomer':{'1':[{'goods_no':'G1','whse_dept':'HCM'},{'goods_no':'G2','whse_dept':'HCM'}],'2':[{'goods_no':'G1','whse_dept':'HCM'}]},'customerInfo':{'1':{'customer_no':'SYN-C1','name':'Synthetic One','sales':'S'},'2':{'customer_no':'SYN-C2','name':'Synthetic Two','sales':'S'}},'wecomBySales':{'S':'synthetic-account'},'employeeBySales':{'S':{'wecom_account':'synthetic-account','region':'HCM'}}}
    return baseline,mapping,a.wf.contact_plan(baseline,mapping)
class CustomerAuditTests(unittest.TestCase):
    def test_source_grain_rolls_and_ownership_reconcile(self):
        b,m,p=sample();result=a.verify_plan(b,m,p)
        self.assertEqual(result['customer_cards'],2);self.assertEqual(result['display_product_rows'],3)
        self.assertEqual(p['sales_packages'][0]['customers'][0]['products'][0],['G1','R',3])
        self.assertEqual(sum(r.get('exception')=='No Matched Customer' for r in p['audit_rows']),1)
    def test_wrong_owner_and_changed_quantity_fail(self):
        b,m,p=sample();p['sales_packages'][0]['account']='other'
        with self.assertRaisesRegex(ValueError,'OWNER'):a.verify_plan(b,m,p)
        b,m,p=sample();p['sales_packages'][0]['customers'][0]['products'][0][2]=99
        with self.assertRaisesRegex(ValueError,'PRODUCTS'):a.verify_plan(b,m,p)
    def test_zip_has_one_decodable_png_per_customer(self):
        _,_,plan=sample();package=plan['sales_packages'][0]
        with TemporaryDirectory() as d:
            path=a.wf.customer_zip(package,Path(d),'2026-W38');proof=a.inspect_zip(path,package)
            self.assertEqual(proof['png_count'],2);self.assertTrue(proof['all_pngs_decoded'])
    def test_wrong_zip_membership_is_rejected(self):
        _,_,plan=sample();package=plan['sales_packages'][0]
        with TemporaryDirectory() as d:
            path=a.wf.customer_zip(package,Path(d),'2026-W38')
            changed=copy.deepcopy(package);changed['customers'].pop()
            with self.assertRaisesRegex(ValueError,'MEMBERSHIP'):a.inspect_zip(path,changed)
    def test_real_size_limit_rejected_before_opening_archive(self):
        _,_,plan=sample()
        fake=SimpleNamespace(stat=lambda:SimpleNamespace(st_size=a.wf.policy()['customer_artifacts']['max_zip_bytes']+1))
        with self.assertRaisesRegex(ValueError,'SIZE'):a.inspect_zip(fake,plan['sales_packages'][0])
    def test_filename_collision_does_not_drop_a_customer(self):
        _,_,plan=sample();package=plan['sales_packages'][0]
        package['customers'][1]['customer_no']=package['customers'][0]['customer_no'];package['customers'][1]['customer_name']=package['customers'][0]['customer_name']
        with TemporaryDirectory() as d:
            with self.assertRaisesRegex(a.wf.WorkflowError,'COLLISION'):a.wf.customer_zip(package,Path(d),'2026-W38')
    def test_long_cell_wraps_without_losing_characters(self):
        from PIL import ImageFont,Image
        font=ImageFont.load_default();text='LONG-COLOR-DETAIL-'*12
        lines=a.wf.card_lines(text,font,80)
        self.assertGreater(len(lines),1);self.assertEqual(''.join(lines),text)
        customer={'customer_name':'Synthetic','products':[['SYN-LONG',text,7]]}
        with TemporaryDirectory() as d:
            path=Path(d)/'card.png';a.wf.card_png(customer,path)
            with Image.open(path) as image:self.assertGreater(image.height,100)
    def test_manifest_pointer_integrity_and_week_isolation(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'audit.json';path.write_text('{"packages":[]}',encoding='utf-8')
            row={'payload':{'kind':'customer_audit_pointer','artifact_path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
            with patch.object(a.live,'root',return_value=Path(d)):
                self.assertEqual(a.audit_data(row),{'packages':[]})
                path.write_text('{}')
                with self.assertRaisesRegex(ValueError,'CHANGED'):a.audit_data(row)
        class Store:
            def rows(self,*args):return [{'cycle_id':'customer-sample-batch-v1','payload':{'selected':[{'week':'2026-W38'}]}}]
        self.assertIsNotNone(a.batch_record(Store(),'2026-W38')[0])
        self.assertEqual(a.batch_record(Store(),'2026-W39'),(None,'customer-sample-2026-w39-v1'))

if __name__=='__main__':unittest.main()
