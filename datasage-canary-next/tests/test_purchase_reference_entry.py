"""Review-only purchase initialization through the trusted local CLI."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import importlib,json,unittest
import test_business_contracts as base
local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
ops=importlib.import_module(base.TEST_PACKAGE+'.operations')
io=importlib.import_module(base.TEST_PACKAGE+'.workflow_io')
transport=importlib.import_module(base.TEST_PACKAGE+'.wecom_app_transport')

class PurchaseReferenceEntryTests(unittest.TestCase):
    def test_robot_target_selects_existing_http_transport_family(self):
        binding={'target_map':{'synthetic-group':{'platform':'wecom_webhook','target_kind':'robot_group','webhook_ref':'purchase_price','target_ref':'a'*64}}}
        with patch.object(transport,'ProductionWebhookTransport') as factory:
            self.assertIs(factory.return_value,io.make_transport(Path('synthetic-profile'),'purchase_price',binding))
            factory.assert_called_once_with('purchase_price',Path('synthetic-profile'))
        for job in ('sales_price','slow_task'):
            with self.assertRaises(io.IOErrorBoundary):io.make_transport(Path('synthetic-profile'),job,binding)
        binding['target_map']['other']={'platform':'wecom_app_http'}
        with self.assertRaises(io.IOErrorBoundary):io.make_transport(Path('synthetic-profile'),'purchase_price',binding)

    def test_purchase_plan_preserves_tax_prices_without_initializing_either_side(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);source=root/'synthetic-reference.json'
            row={'id':1,'goods_no':'SYN-P','goods_name':'Synthetic Product','color_label':'Red','supplier_no':'SYN-S','supplier_name':'Synthetic Supplier','tax_inclue_price':'110.001','tax_exclue_price':'100.0001','currency_no':'USD','unit_cuur':'m','snapshot_at':'2026-09-19 09:00:00'}
            source.write_text(json.dumps({'rows':[row],'provenance':{'source':'synthetic_operator_json','observed_at':'2026-09-19 09:00:00','history_unknown':True}}),encoding='utf-8')
            with patch('hermes_constants.get_hermes_home',return_value=root),patch.object(local,'_assert_local_context'),patch.object(base.tools,'_execute_with_source',side_effect=AssertionError('no database query in initialization review')):
                status=local.main(root,['--purchase-reference-plan',str(source)])
            self.assertEqual(0,status)
            files=list((root/'report_runs/legacy_execution').glob('purchase-reference-initialization-preview-*.json'))
            self.assertEqual(1,len(files));plan=json.loads(files[0].read_text())
            self.assertEqual(row,plan['rows'][0]);self.assertTrue(plan['review_required'])
            self.assertFalse((root/'report_runs/legacy_execution/purchase_reference/head.json').exists())
            self.assertFalse((root/'report_runs/legacy_execution/sales_reference/head.json').exists())

    def test_two_sides_or_execution_cannot_mix_with_initialization_review(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            for extra in (['--sales-reference-plan','other.json'],['--legacy-run','purchase_price'],['--accept-snapshot','a'*64]):
                with patch('hermes_constants.get_hermes_home',return_value=root),patch.object(local,'_assert_local_context'):
                    self.assertEqual(2,local.main(root,['--purchase-reference-plan','missing.json',*extra]))

    def test_purchase_generic_observation_cannot_advance_profile_reference(self):
        operation={'kind':'purchase_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000,'reference_source':'profile_local'}
        with patch.object(local,'_assert_local_context'),patch.object(base.tools,'_execute_with_source',side_effect=AssertionError('no bypass query')):
            with self.assertRaisesRegex(ops.OperationError,'REQUIRES_WORKFLOW_ENTRY'):
                ops.execute(Path('synthetic-profile'),'synthetic',operation)

if __name__=='__main__':unittest.main()
