"""Public branch isolation through registered handlers and synthetic SQLite only."""
import itertools
from decimal import Decimal
import unittest
from unittest.mock import patch
import test_remediation_remaining_cases as public

class PublicBranchIsolationTests(unittest.TestCase):
    def setUp(self):
        self.h=public.RemainingCaseTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.delivery([(100,6,'n','2026-08-15','A'),(50,6,'n','2026-07-15','A')])
    def request(self,rid='good',**kw):
        return public.metric('delivery_amount','delivery',request_id=rid,**kw)
    def good(self,payload,rid='good'):
        r=self.h.result(payload,rid);self.assertEqual('success',r['status'],r)
        self.assertEqual(100,r['rows'][0]['facts']['metric_value'])
    def test_branch_errors_keep_valid_SQL_and_ID_order_in_all_permutations(self):
        good=self.request();unknown={**self.request('unknown'),'metric':'unregistered_metric'}
        dim=self.request('bad_dimension',dimensions=['not_a_dimension'])
        for requests in itertools.permutations((good,unknown,dim)):
            with self.subTest(ids=[r['request_id'] for r in requests]):
                response=self.h.query(*requests);self.assertEqual('partial',response['status'],response)
                self.assertEqual([r['request_id'] for r in requests],[r['request_id'] for r in response['results']])
                self.good(response)
                for rid,code in [('unknown','UNSUPPORTED_METRIC'),('bad_dimension','UNSUPPORTED_DIMENSION')]:
                    r=self.h.result(response,rid);self.assertEqual('failed',r['status']);self.assertEqual(code,r['error']['code']);self.assertEqual([],r['rows'])
    def test_global_envelope_errors_never_reach_readiness_or_SQL(self):
        cases=[{}, {'requests':'bad'}, {'requests':[self.request(),None]}, {'requests':[self.request(),self.request()]},
               {'requests':[{'domain':'delivery','metric':'delivery_amount'}]},
               {'requests':[self.request()],'calculations':[{'calculation_id':'x','operation':'difference','left_request_id':'good','right_request_id':'absent'}]}]
        with patch.object(public.health,'query_readiness_status',side_effect=AssertionError('readiness forbidden')):
            for args in cases:
                with self.subTest(args=args):
                    r=self.h.invoke('datasage_query',args);self.assertEqual('failed',r['status']);self.assertEqual([],r.get('results',[]))
        self.assertEqual([],self.h.sql_trace)
    def test_unauthorized_stops_before_metric_or_envelope_oracle(self):
        with patch.object(public.plugin.tools,'_validate_query_dispatch',side_effect=AssertionError('oracle forbidden')),patch.object(public.health,'query_readiness_status',side_effect=AssertionError('readiness forbidden')):
            for args in ({'requests':[self.request(),{**self.request('bad'),'metric':'unknown'}]},{}):
                r=self.h.invoke('datasage_query',args,bound=False);self.assertEqual('DATA_ENTITLEMENT_DENIED',r['error']['code'])
        self.assertEqual([],self.h.sql_trace)
    def test_all_branch_errors_have_IDs_and_no_readiness_or_SQL(self):
        with patch.object(public.health,'query_readiness_status',side_effect=AssertionError('readiness forbidden')):
            response=self.h.query({**self.request('u'),'metric':'unknown'},self.request('d',dimensions=['bad']))
        self.assertEqual('failed',response['status']);self.assertEqual(['u','d'],[r['request_id'] for r in response['results']]);self.assertFalse(self.h.sql_trace)
    def test_calculation_dependency_failure_does_not_erase_independent_fact(self):
        bad={**self.request('bad'),'metric':'unknown'}
        response=self.h.invoke('datasage_query',{'requests':[self.request(),bad], 'calculations':[{'calculation_id':'derived','operation':'difference','left_request_id':'good','right_request_id':'bad'}]})
        self.good(response);self.assertEqual('partial',response['status'])
        self.assertEqual('failed',response['calculations'][0]['status']);self.assertIsNone(response['calculations'][0]['value']);self.assertEqual('CALCULATION_SOURCE_UNAVAILABLE',response['calculations'][0]['error']['code'])
    def test_complete_operation_failure_and_valid_complete_operation_are_independent(self):
        bad=self.request('bad',comparison={'kind':'previous_period'},complete_change_decomposition={'dimension':'not_a_dimension'})
        complete=self.request('complete',comparison={'kind':'previous_period'},complete_change_decomposition={'dimension':'department'})
        response=self.h.query(bad,self.request(),complete);self.good(response)
        self.assertEqual(['bad','good','complete'],[r['request_id'] for r in response['results']]);self.assertEqual('failed',self.h.result(response,'bad')['status'])
        self.assertEqual(Decimal(50),Decimal(self.h.result(response,'complete')['change_reconciliation']['overall_delta']))
    def test_manual_partition_cannot_claim_reconciliation_with_failed_parent(self):
        parent={**self.request('parent',comparison={'kind':'previous_period'}),'metric':'unknown'}
        partition=self.request('partition',comparison={'kind':'previous_period'},dimensions=['department'],decomposition_of_request_id='parent')
        for reqs in ((partition,parent,self.request()),(parent,self.request(),partition)):
            response=self.h.query(*reqs);self.good(response)
            self.assertEqual([r['request_id'] for r in reqs],[r['request_id'] for r in response['results']])
            for row in self.h.result(response,'partition').get('rows',[]):self.assertNotIn('structural_contribution',row.get('allowed_relations',[]))
    def test_entity_error_and_DB_failure_preserve_valid_branches(self):
        self.h.fail_receipts=True
        response=self.h.query(self.request('entity',metric_filters={'customer':'UNREGISTERED-SYNTHETIC'}),self.request(),public.metric('actual_receipt_amount','receipt',request_id='db'))
        self.good(response);self.assertEqual('partial',response['status'])
        for rid in ('entity','db'):
            r=self.h.result(response,rid);self.assertEqual('timeout' if rid=='db' else 'failed',r['status']);self.assertTrue(r['error']['code']);self.assertEqual([],r['rows'])

if __name__=='__main__': unittest.main()
