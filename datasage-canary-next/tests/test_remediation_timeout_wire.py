"""A deadline must preserve completed facts without bypassing public projection."""
import importlib
import json
import unittest
from unittest.mock import patch
import test_remediation_overdue_coverage as coverage
import test_remediation_remaining_cases as public


class TimeoutWireTests(unittest.TestCase):
    def test_both_deadline_exits_keep_public_shape_and_completed_facts(self):
        t=coverage.OverdueCoverageTests();t.setUp();self.addCleanup(t.doCleanups)
        t.row('SYNTHETIC_KNOWN')
        captured=[];original=public.plugin.tools._run_one
        def capture(*args,**kwargs):
            result=original(*args,**kwargs);captured.append(result);return result
        with patch.object(public.plugin.tools,'_run_one',side_effect=capture):
            response,_,_=t.query()
        request=t.h.calls[-1]['args']['requests'][0]
        intermediate=public.plugin.tools._model_wire_result(captured[0],request=request)
        self.assertTrue(intermediate['claim_ledger'])
        raw={**response,'results':[intermediate]}
        raw.pop('model_wire_version',None)  # producer payload, before the compact v3 boundary
        wire=importlib.import_module(f'{public.plugin.__name__}.wire')
        compact=wire.enforce_tool_result_budget
        for stage in ('producer','compaction'):
            with self.subTest(stage=stage):
                now=[0.0]
                def handler(args):
                    payload=dict(raw)
                    if stage=='producer':
                        payload.update(status='partial',error={'code':'BATCH_DEADLINE_EXCEEDED'})
                        now[0]=61.0
                    return json.dumps(payload)
                def render(*args):
                    result=compact(*args)
                    if stage=='compaction':now[0]=61.0
                    return result
                with patch.object(wire.time,'monotonic',side_effect=lambda:now[0]),patch.object(wire,'enforce_tool_result_budget',side_effect=render):
                    result=json.loads(wire.bounded_json_handler('datasage_query',handler)({}))
                self.assertEqual('partial',result['status'])
                self.assertEqual('BATCH_DEADLINE_EXCEEDED',result['error']['code'])
                row=result['results'][0]
                self.assertNotIn('claim_ledger',row)
                self.assertNotIn('disclosure_ledger_seal',row)
                self.assertNotIn('scope_fingerprint',row)
                self.assertEqual(200,row['rows'][0]['facts']['metric_value'])
                self.assertEqual(1,row['rows'][0]['facts']['scope_row_count'])


if __name__=='__main__':unittest.main()
