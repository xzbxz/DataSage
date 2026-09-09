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



class QueryHandoffTests(unittest.TestCase):
    def setUp(self):
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.delivery([(100, 6, 'n', '2026-08-15', 'A'),
                         (50, 6, 'n', '2026-07-15', 'A')])

    def request(self, rid='good', **kwargs):
        return public.metric('delivery_amount', 'delivery', request_id=rid, **kwargs)

    def handoff(self, args, after=60.001):
        # Stage-based clock: advance only after the real producer has returned
        # and the real outer capacity lease has been released, not on clock-call N.
        tools = public.plugin.tools
        now = [0.0]
        completed = []
        producer = tools._datasage_query_with_slot
        release = tools._release_query_slot
        sql_before = len(self.h.sql_trace)
        def finish(*a, **kw):
            raw = producer(*a, **kw)
            completed.append(json.loads(raw))
            return raw
        def released():
            release()
            if completed:
                now[0] = after
        with patch.object(tools.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(tools, '_datasage_query_with_slot', side_effect=finish), \
             patch.object(tools, '_release_query_slot', side_effect=released):
            result = self.h.invoke('datasage_query', args)
        self.assertTrue(completed)
        self.assertEqual(0, tools._ACTIVE_QUERY_CALLS)
        self.assertIsNone(tools.db_executor.current_deadline())
        # The registered public handler is used; readiness/SQL transport comes
        # from the shared in-memory SQLite fixture with network tripwires.
        return result, completed[0], len(self.h.sql_trace) - sql_before

    def test_registered_handoff_keeps_completed_facts_and_normal_reply(self):
        args = {'requests': [self.request()]}
        normal, normal_raw, normal_sql = self.handoff(args, 59.999)
        expired, expired_raw, expired_sql = self.handoff(args)
        self.assertEqual('success', normal['status'])
        self.assertEqual('partial', expired['status'])
        self.assertEqual('BATCH_DEADLINE_EXCEEDED', expired['error']['code'])
        self.assertEqual(normal['results'], expired['results'])
        self.assertEqual(normal['source_evidence_ref'], expired['source_evidence_ref'])
        self.assertEqual(normal['disclosures'], expired['disclosures'])
        self.assertEqual(normal['evidence_bundle'], expired['evidence_bundle'])
        self.assertEqual(normal_sql, expired_sql)
        self.assertGreater(expired_sql, 0)
        self.assertNotIn('BATCH_DEADLINE_EXCEEDED', json.dumps(expired_raw))
        self.assertEqual(100, expired['results'][0]['rows'][0]['facts']['metric_value'])
        for private in ('claim_ledger', 'scope_fingerprint', 'disclosure_ledger_seal'):
            self.assertNotIn(private, expired['results'][0])

    def test_handoff_preserves_failed_calculation_dependency(self):
        args = {'requests': [self.request(), {**self.request('bad'), 'metric':'unknown'}],
                'calculations':[{'calculation_id':'delta','operation':'difference',
                    'left_request_id':'good','right_request_id':'bad'}]}
        result, raw, count = self.handoff(args)
        self.assertEqual('partial', result['status'])
        self.assertEqual(100, self.h.result(result, 'good')['rows'][0]['facts']['metric_value'])
        self.assertEqual([], self.h.result(result, 'bad')['rows'])
        self.assertEqual('failed', result['calculations'][0]['status'])
        self.assertIsNone(result['calculations'][0]['value'])
        self.assertEqual('CALCULATION_SOURCE_UNAVAILABLE', result['calculations'][0]['error']['code'])

    def test_handoff_keeps_valid_reconciliation_without_authorizing_failed_one(self):
        good = self.request('complete', comparison={'kind':'previous_period'},
                            complete_change_decomposition={'dimension':'department'})
        bad = self.request('bad', comparison={'kind':'previous_period'},
                           complete_change_decomposition={'dimension':'not_a_dimension'})
        result, _, _ = self.handoff({'requests':[bad, good]})
        self.assertEqual([], self.h.result(result, 'bad')['rows'])
        self.assertEqual(50, float(self.h.result(result, 'complete')['change_reconciliation']['overall_delta']))
        self.assertFalse(any('causal' in relation for r in result['results']
                             for row in r['rows'] for relation in row['allowed_relations']))

    def test_invalid_unverified_producer_evidence_stays_empty_at_handoff(self):
        import copy
        tools = public.plugin.tools
        project = tools._model_wire_result
        def invalid(result, **kwargs):
            result = copy.deepcopy(result)
            result['disclosure_ledger_seal'] = 'invalid'
            result['private_sql'] = 'SYNTHETIC_PRIVATE_SQL'
            return project(result, **kwargs)
        with patch.object(tools, '_model_wire_result', side_effect=invalid):
            result, _, _ = self.handoff({'requests':[self.request()]})
        self.assertTrue(result['results'])
        self.assertTrue(all(r.get('error', {}).get('code') == 'EVIDENCE_INTEGRITY_INVALID' for r in result['results']))
        self.assertFalse(any(r['rows'] for r in result['results']))
        self.assertNotIn('SYNTHETIC_PRIVATE_SQL', json.dumps(result))
        self.assertFalse(any(r.get('change_reconciliation') for r in result['results']))

    def test_malformed_json_cannot_become_timeout_facts(self):
        wire = importlib.import_module(f'{public.plugin.__name__}.wire')
        now = [0.0]
        def malformed(args):
            now[0] = 60.001
            return 'not JSON SYNTHETIC_PRIVATE_SQL'
        with patch.object(wire.time, 'monotonic', side_effect=lambda: now[0]):
            result = json.loads(wire.bounded_json_handler('datasage_query', malformed)({}))
        self.assertEqual('failed', result['status'])
        self.assertEqual([], result['results'])
        self.assertNotIn('SYNTHETIC_PRIVATE_SQL', json.dumps(result))

if __name__=='__main__':unittest.main()
