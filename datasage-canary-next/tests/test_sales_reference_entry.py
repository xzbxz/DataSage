"""Local mode admission and review-only initialization, with no source I/O."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import importlib,json,unittest
import test_business_contracts as base
ops=importlib.import_module(base.TEST_PACKAGE+'.operations')
local=importlib.import_module(base.TEST_PACKAGE+'.local_report')
workflow=importlib.import_module(base.TEST_PACKAGE+'.workflow_io')

class SalesReferenceEntryTests(unittest.TestCase):
    def binding(self,**extra):return {'kind':'sales_prices','regions':['HCM','HN','BKK','IDK'],'limit':10000,'reference_source':'profile_local',**extra}
    def test_explicit_local_price_modes_require_four_region_scope(self):
        self.assertEqual('profile_local',ops.validate_binding(self.binding())['reference_source'])
        self.assertEqual('profile_local',ops.validate_binding(self.binding(kind='purchase_prices'))['reference_source'])
        for value in (self.binding(kind='purchase_prices',regions=['HCM']),self.binding(regions=['HCM']),self.binding(reference_source='local'),self.binding(reference_source=True)):
            with self.assertRaises(ops.OperationError):ops.validate_binding(value)
        self.assertEqual('legacy_database',ops.validate_binding(self.binding(reference_source='legacy_database'))['reference_source'])

    def test_generic_observation_entry_cannot_activate_local_reference(self):
        with patch.object(local,'_assert_local_context'),patch.object(base.tools,'_execute_with_source',side_effect=AssertionError('unexpected database query')):
            with self.assertRaisesRegex(ops.OperationError,'REQUIRES_WORKFLOW_ENTRY'):
                ops.execute(Path('synthetic-profile'),'synthetic',self.binding())

    def test_preview_initialization_never_creates_head_or_queries(self):
        with TemporaryDirectory() as temp:
            profile=Path(temp)
            value={'rows':[{'id':1,'goods_id':101,'dept':'HCM','customer_grade':'A','color_label':'Red','ddp_price':'100.001','currency_no':'USD','matched_detail_id':11,'snapshot_at':'2026-09-18 10:00:00'}],
                'provenance':{'source':'synthetic_operator_file','observed_at':'2026-09-18 10:00:00','history_unknown':True}}
            source=profile/'reference-candidate.json';source.write_text(json.dumps(value),encoding='utf-8')
            with patch('hermes_constants.get_hermes_home',return_value=profile),patch.object(local,'_assert_local_context'),patch.object(base.tools,'_execute_with_source',side_effect=AssertionError('unexpected source query')):
                result=local.main(profile,['--sales-reference-plan',str(source)])
            self.assertEqual(0,result)
            self.assertFalse((profile/'report_runs/legacy_execution/sales_reference/head.json').exists())
            files=list((profile/'report_runs/legacy_execution').glob('sales-reference-initialization-preview-*.json'))
            self.assertEqual(1,len(files));plan=json.loads(files[0].read_text(encoding='utf-8'))
            self.assertTrue(plan['review_required']);self.assertEqual('100.001',plan['rows'][0]['ddp_price'])

    def test_initialization_preview_cannot_combine_execution_or_accept(self):
        with TemporaryDirectory() as temp:
            profile=Path(temp)
            with patch('hermes_constants.get_hermes_home',return_value=profile),patch.object(local,'_assert_local_context'):
                self.assertEqual(2,local.main(profile,['--sales-reference-plan','missing.json','--legacy-run','sales_price']))

if __name__=='__main__':unittest.main()
