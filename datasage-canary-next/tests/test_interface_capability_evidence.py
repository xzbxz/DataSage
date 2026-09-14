"""Model-visible capability facts must survive projection and match rejection fields."""
import json
import unittest
from unittest.mock import patch
from test_remediation_remaining_cases import plugin, metric
import test_slow_baseline_net_outbound as weekly


class InterfaceCapabilityTests(unittest.TestCase):
    def test_net_grouping_and_forbidden_time_bucket_survive_model_catalog(self):
        for code in ('registered_slow_monthly_net_outbound', 'registered_slow_pool_baseline_net_outbound'):
            for selector in ({'metric': code}, {'view': 'expert_index'}):
                payload=json.loads(plugin.contracts.datasage_catalog({'requests':[{'domain':'inventory',**selector}]}))
                compact=plugin.wire.compact_catalog_payload(payload)['results'][0]
                item=compact['metric'] if 'metric' in compact else next(m for m in compact['metrics'] if m['code']==code)
                self.assertEqual(['unit'],item['grouping']['required'])
                self.assertEqual([],item['allowed_time_buckets'])
                self.assertEqual([],item['ordering']['fields'])

    def test_builder_bucket_capability_keeps_existing_month_gate(self):
        gate=plugin.capability_contract.analytical_time_buckets
        self.assertIsNone(gate({}))
        for kind in ('target_completion','allocated_amount','pattern_matching'):
            self.assertEqual(['month'],gate({'query_kind':kind}))
        self.assertEqual(['month'],gate({'query_kind':'fabric_source','fabric_side':'delivery'}))
        self.assertEqual([],gate({'query_kind':'fabric_source','fabric_side':'inventory'}))
        self.assertEqual([],gate({'query_kind':'monthly_slow_pool'}))
        payload=json.loads(plugin.contracts.datasage_catalog({'requests':[{'domain':'inventory','metric':'fabric_inventory_source_summary'}]}))
        item=plugin.wire.compact_catalog_payload(payload)['results'][0]['metric']
        self.assertEqual([],item['allowed_time_buckets'])

    def test_rejected_fields_survive_public_wire_without_sql(self):
        h=weekly.BaselineNetTests();h.setUp();self.addCleanup(h.doCleanups)
        for code, period in [('registered_slow_monthly_net_outbound', {'month':'2026-09'}), ('registered_slow_pool_baseline_net_outbound', {'month':None,'baseline_week':'2026-W37'})]:
          for field,kwargs in [('time_bucket',{'time_bucket':'month'}),('dimensions',{'dimensions':['salesperson']}),('order_by',{'order_by':{'field':'metric_value','direction':'desc'}})]:
            with self.subTest(metric=code,field=field):
                before=len(h.sql_trace)
                result=h.query(metric(code,'inventory',**period,**kwargs))
                compact=plugin.wire.compact_query_payload(result)
                error=compact['results'][0]['error']
                self.assertEqual(field,error['path'])
                self.assertIn('hint',error)
                self.assertEqual(before,len(h.sql_trace))
                self.assertNotIn('DDP',error['hint'])

    def test_time_evidence_stays_per_request(self):
        rows=[{'request_id':'stock','status':'success','rows':[], 'applied_time_range':{'monthly_read_at':'2026-09-14T10:00:01','monthly_is_current':1}},
              {'request_id':'flow','status':'success','rows':[], 'applied_time_range':{'monthly_read_at':'2026-09-14T10:00:04','monthly_is_current':1}}]
        result=plugin.wire.compact_query_payload({'status':'success','results':rows})
        self.assertEqual([r['applied_time_range'] for r in rows],[r['applied_time_range'] for r in result['results']])
