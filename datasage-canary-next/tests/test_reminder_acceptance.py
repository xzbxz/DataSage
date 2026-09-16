import importlib,unittest,json
from unittest.mock import patch
from datetime import date
from pathlib import Path
from test_acceptance_delivery import AcceptanceDeliveryTests,a,io
import test_business_contracts as base

r=importlib.import_module(base.TEST_PACKAGE+'.reminder_acceptance')
class ReminderAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=AcceptanceDeliveryTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.profile=self.fixture.profile
        self.home=a.runtime_home(self.profile)
        (self.home/'legacy-recipient-reference.json').write_text(json.dumps({'regions':{'IDK':{'executors':[{'account':'original-a'},{'account':'original-b'}]}}}),encoding='utf-8')
        local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
        for p in (patch.object(local,'configure_runtime'),patch.object(r.report_evidence.tools,'_business_today',return_value=date(2026,9,16))):p.start();self.addCleanup(p.stop)
    def test_synthetic_sales_keep_distinct_content_and_attachments(self):
        with patch.object(r.operations,'execute',side_effect=AssertionError('no business query')):
            value=r.prepare(self.profile,'sales-personalized-synthetic')
        self.assertEqual(2,len(value['notices']));self.assertNotEqual(value['notices'][0]['attachments'],value['notices'][1]['attachments'])
        self.assertNotEqual(value['notices'][0]['logical_id'],value['notices'][1]['logical_id'])
        self.assertEqual(value,r.prepare(self.profile,'sales-personalized-synthetic'))
    def test_private_group_purchase_fixture_is_explicitly_synthetic(self):
        value=r.prepare(self.profile,'purchase-private-group-synthetic')
        self.assertEqual('synthetic',value['evidence']['origin'])
        self.assertEqual(['private','group'],[n['channel'] for n in value['notices']]);self.assertTrue(value['notices'][1]['mention_all'])
        self.assertLess(value['notices'][0]['body'].index('采购价下调'),value['notices'][0]['body'].index('采购价上调'))
    def test_first_price_observation_does_not_alert_or_accept_baseline(self):
        doc={'kind':'sales_prices','status':'success','baseline_id':None,'records':[],'events':[],
             'event_counts':{},'observed_at':'2026-09-16T10:00:00','source_rows':0}
        with patch.object(r.operations,'execute',return_value=doc) as query,patch.object(r.operations,'accept_snapshot',side_effect=AssertionError('baseline')),patch.object(a,'deliver_batch',side_effect=AssertionError('send')):
            value=r.prepare(self.profile,'sales-observation')
            self.assertEqual('no_accepted_price_reference_no_alert',value['status'])
            self.assertNotEqual(self.profile,query.call_args.args[0]);self.assertFalse(r.send(self.profile,'sales-observation')['sent'])
    def test_idk_same_body_role_aggregation_preserves_old_recipients(self):
        doc={'kind':'idk_unpriced','status':'success','observed_at':'2026-09-16T10:00:00','source_rows':1,
             'records':[{'product':'SYN','color':'Red'}]}
        with patch.object(r.operations,'execute',return_value=doc):value=r.prepare(self.profile,'idk-current')
        self.assertEqual(['original-a','original-b'],value['evidence']['original_recipients'])
        self.assertEqual(1,len(value['notices']));self.assertIn('SYN | Color Red',value['notices'][0]['body'])
    def test_changed_staged_attachment_cannot_send(self):
        value=r.prepare(self.profile,'sales-personalized-synthetic')
        Path(value['notices'][0]['attachments'][0]).write_bytes(b'changed')
        with patch.object(a,'deliver_batch',side_effect=AssertionError('send')):
            with self.assertRaisesRegex(io.IOErrorBoundary,'FILE_CHANGED'):r.send(self.profile,'sales-personalized-synthetic')
    def test_customers_cannot_precede_task_acceptance(self):
        with patch.object(r.workflow_inputs,'customer_mapping',side_effect=AssertionError('query')):
            with self.assertRaisesRegex(io.IOErrorBoundary,'TASK_STAGE'):r.prepare(self.profile,'hcm-customer-packages')
    def test_unregistered_modes_and_production_actions_are_rejected(self):
        with patch.object(r,'prepare',side_effect=AssertionError('prepare')),patch.object(r,'send',side_effect=AssertionError('send')):
            for argv in ([],['--enable'],['--send','all'],['--prepare','qc'],['--prepare','floating_ball'],['--send','hcm-weekly','--force']):self.assertEqual(2,r.main(self.profile,argv))
    def test_cycle_expiry_stops_before_business_query(self):
        with patch.object(r.report_evidence.tools,'_business_today',return_value=date(2026,9,23)),patch.object(r.operations,'execute',side_effect=AssertionError('query')):
            with self.assertRaisesRegex(io.IOErrorBoundary,'CYCLE_EXPIRED'):r.prepare(self.profile,'idk-current')
if __name__=='__main__':unittest.main()
