import unittest,importlib,json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import test_business_contracts as base
r=importlib.import_module(base.TEST_PACKAGE+'.workflow_delivery_review')
s=importlib.import_module(base.TEST_PACKAGE+'.workflow_schedule')

class ReviewTests(unittest.TestCase):
    def test_instance_prefix_does_not_disguise_same_business_body(self):
        with patch.object(r.delivery,'runtime_home'),patch.object(r.delivery,'file_snapshot',return_value=('a',b'a','x','semantic')),patch.object(r,'content_digest',return_value='semantic'):
            a={'channel':'private','body':'Weekly HN W38','attachments':['a.xlsx']}
            self.assertEqual(r.signature(a),r.signature({**a,'body':'【真实来源验收，非生产派发】\nWeekly HN W38'}))
            self.assertNotEqual(r.signature(a),r.signature({**a,'body':'Weekly HN W39'}))
            self.assertNotEqual(r.signature(a),r.signature({**a,'message_format':'markdown'}))
    def test_only_verified_accepted_receipt_can_suppress(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'progress.json';path.write_text(json.dumps({'components':{'a'*64:{'status':'provider_accepted'}}}))
            receipt={'status':'provider_accepted_not_human_read','components':1,'progress_file':str(path)}
            with patch.object(r.io,'private_root',return_value=Path(d)),patch.object(r.delivery,'runtime_home'):
                self.assertFalse(r.accepted(receipt))
                self.assertEqual('historical_provider_accepted',r.delivery.classify_receipt(receipt)['classification'])
                self.assertFalse(r.accepted({**receipt,'status':'unknown'}))
                self.assertFalse(r.accepted({**receipt,'components':2}))
    def test_all_weeklies_precede_first_monthly_in_scheduled_group(self):
        departments=['HCM','HN','IDK'];states={'HCM':4,'HN':3,'IDK':3}
        self.assertEqual(s.next_report_department(departments,states),'HN')
        states['HN']=4;self.assertEqual(s.next_report_department(departments,states),'IDK')
        states['IDK']=4;self.assertEqual(s.next_report_department(departments,states),'HCM')
        states['HCM']=5;self.assertEqual(s.next_report_department(departments,states),'HN')
    def test_missing_task_chain_does_not_start_reports(self):
        with self.assertRaisesRegex(ValueError,'TASK_CHAINS'):s.next_report_department(['HN','IDK'],{'HN':3,'IDK':2})
    def test_outer_filename_does_not_create_a_fake_business_difference(self):
        import io,zipfile
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('xl/worksheets/sheet1.xml','same cells')
        raw=stream.getvalue()
        self.assertEqual(r.content_digest('Old_Report.xlsx',raw),r.content_digest('New_Test_Report.xlsx',raw))
    def test_same_month_content_does_not_suppress_another_week(self):
        document={'evidence':{'observed_at':'2026-09-16T14:00:00'}}
        receipt={'case_id':'regional-hcm-ht-monthly-w38-v1'}
        self.assertTrue(r.same_delivery_week('ls-hcm-ht-2026-w38-g0',document,receipt,'manifest.json'))
        self.assertFalse(r.same_delivery_week('ls-hcm-ht-2026-w39-g0',document,receipt,'manifest.json'))
    def test_changed_prior_manifest_is_not_accepted_as_proof(self):
        items=[{'body':'original','attachments':[]}]
        doc={'notices':items,'notice_digest':r.base.digest(items),'files':{}}
        self.assertTrue(r.sealed(doc,items))
        altered=[{'body':'changed','attachments':[]}]
        self.assertFalse(r.sealed(doc,altered))

if __name__=='__main__':unittest.main()
