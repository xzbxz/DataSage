"""Independent public regressions for direction, rank, numeric evidence and discovery."""
import unittest,json
from decimal import Decimal as D
from unittest.mock import patch
import test_remediation_remaining_cases as public
import test_c01_c02_public as target_cases
import test_remediation_overdue_coverage as overdue_cases
import test_profit_contract as profit_cases
metric=public.metric

class AnalysisEvidenceTests(unittest.TestCase):
    def fixture(self):
        h=public.RemainingCaseTests();h.setUp();self.addCleanup(h.doCleanups);return h
    def change(self):
        h=self.fixture()
        for table in ['sale_bill_goods_detail_dwd','delivery_return_detail_dwd']:
            h.conn.execute('ALTER TABLE vk_dwd.'+table+' ADD COLUMN customer_id TEXT')
            h.conn.execute('ALTER TABLE vk_dwd.'+table+' ADD COLUMN customer_name TEXT')
        rows=[]
        for i in range(32):
            delta=-10 if i<22 else 5 if i<30 else 0
            for month,value in [('07',100),('08',100+delta)]:rows.append((value,6,'n','2026-'+month+'-10',str(i),'Customer '+str(i)))
        h.insert('vk_dwd.sale_bill_goods_detail_dwd','delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_id,customer_name',rows)
        return h
    def query_change(self,h,direction=None):
        op={'dimension':'customer'}
        if direction is not None:op['direction']=direction
        return h.query(metric('delivery_amount','delivery',complete_change_decomposition=op))
    def test_directions_keep_same_population_and_reconcile(self):
        h=self.change()
        for direction in ['decrease','increase','absolute']:
            r=h.result(self.query_change(h,direction));rec=r['change_reconciliation']
            self.assertEqual('reconciled',rec['status']);self.assertEqual(D(-180),D(rec['overall_delta']))
            deltas=[D(x['facts']['delta_value']) for x in r['rows']]
            self.assertEqual(D(5) if direction=='increase' else D(-10),deltas[0])
            self.assertEqual(32,rec['full_partition_row_count']);self.assertTrue(r['truncated'])
    def test_full_distribution_does_not_describe_tail_as_all_decliners(self):
        h=self.change();r=h.result(self.query_change(h,'increase'))
        dist=r['change_reconciliation']['change_distribution']
        self.assertEqual((8,22,2,0),(dist['positive_count'],dist['negative_count'],dist['zero_count'],dist['unknown_count']))
        self.assertEqual(D(40),D(dist['positive_delta_sum']));self.assertEqual(D(-220),D(dist['negative_delta_sum']))
        self.assertEqual('full_partition_groups',dist['scope'])
    def test_absolute_sort_can_prioritize_a_larger_positive_change(self):
        h=self.change();h.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=130 WHERE customer_id='22' AND delivery_time='2026-08-10'")
        absolute=h.result(self.query_change(h,'absolute'))
        decrease=h.result(self.query_change(h,'decrease'))
        self.assertEqual(D(30),D(absolute['rows'][0]['facts']['delta_value']))
        self.assertEqual(D(-10),D(decrease['rows'][0]['facts']['delta_value']))
        self.assertEqual(D(-155),D(absolute['change_reconciliation']['overall_delta']))
    def test_unknown_values_withhold_full_distribution(self):
        h=self.change();h.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=NULL WHERE customer_id='0' AND delivery_time='2026-08-10'")
        r=h.result(self.query_change(h,'decrease'))
        self.assertNotEqual('reconciled',r['change_reconciliation']['status'])
        self.assertNotIn('change_distribution',r['change_reconciliation'])
    def test_invalid_direction_fails_before_sql(self):
        h=self.change();r=self.query_change(h,'DROP TABLE');self.assertEqual('failed',r['status']);self.assertFalse(h.sql_trace)
    def test_malformed_distribution_is_not_promoted_to_a_full_population_fact(self):
        h=self.change();original=public.plugin.tools._seal_change_reconciliations
        def corrupt(results):
            original(results)
            for r in results:
                proof=r.get('change_reconciliation')
                if isinstance(proof,dict) and 'change_distribution' in proof:
                    proof['change_distribution']['negative_count']=0
                    public.plugin.evidence.seal_reconciliation(proof)
        with patch.object(public.plugin.tools,'_seal_change_reconciliations',corrupt):r=self.query_change(h,'decrease')
        self.assertEqual('failed',r['status']);self.assertFalse(r['results'][0]['rows'])
    def test_full_returned_zero_net_partition_still_distinguishes_signs(self):
        h=self.change();h.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=270 WHERE customer_id='0' AND delivery_time='2026-08-10'")
        r=h.result(h.query(metric('delivery_amount','delivery',dimensions=['customer'],comparison={'kind':'previous_period'},limit=100)))
        dist=r['numeric_evidence']['change_distribution']
        self.assertEqual((9,21,2),(dist['positive_count'],dist['negative_count'],dist['zero_count']))
        self.assertEqual(0,D(dist['positive_delta_sum'])+D(dist['negative_delta_sum']))
    def target(self,targets=(100,200),actuals=(99,1)):
        case=target_cases.PublicInterfaceRegressions();h=case.target(targets=targets,actuals=actuals);self.addCleanup(case.doCleanups);return h
    def rank_query(self,h,field='gap_amount_rmb'):
        return h.query(metric('delivery_target_completion','target',attribution_mode='transaction_detail',dimensions=['department'],limit=1,order_by={'field':field,'direction':'desc'}))
    def test_truncated_target_top_gap_is_a_proven_rank_not_a_full_partition(self):
        h=self.target();r=h.result(self.rank_query(h));proof=r['ranking_evidence']
        self.assertTrue(r['truncated']);self.assertEqual(199,r['rows'][0]['facts']['gap_amount_rmb'])
        self.assertEqual('gap_amount_rmb',proof['field']);self.assertEqual('desc',proof['direction'])
        self.assertTrue(proof['top_value_proven']);self.assertEqual(2,proof['population_count'])
        self.assertFalse(proof['all_rows_returned'])
    def test_completion_sort_does_not_authorize_largest_gap(self):
        h=self.target();r=h.result(self.rank_query(h,'completion_rate'))
        self.assertEqual(1,r['rows'][0]['facts']['gap_amount_rmb']);self.assertEqual('completion_rate',r['ranking_evidence']['field'])
    def test_ties_and_unknown_ranking_values_have_explicit_boundaries(self):
        h=self.target(targets=(100,200),actuals=(50,150));r=h.result(self.rank_query(h))
        self.assertTrue(r['ranking_evidence']['top_value_proven']);self.assertFalse(r['ranking_evidence']['unique_top']);self.assertFalse(r['ranking_evidence']['top_ties_complete'])
        h.conn.execute("UPDATE vk_dwd.delivery_target_detail_dwd SET detail_target_rmb=NULL WHERE customer_dept='Beta'")
        r=h.result(self.rank_query(h));self.assertFalse(r['ranking_evidence']['top_value_proven']);self.assertEqual(1,r['ranking_evidence']['unknown_value_count'])
    def monthly(self):
        h=self.target(targets=(),actuals=())
        # Independent synthetic series; no production or customer facts are hardcoded.
        values=[90,80,20,70,10,60,50,30]
        for i,value in enumerate(values,1):
            h.insert('vk_dwd.delivery_target_detail_dwd','detail_target_rmb,is_inner_cus,year_month,customer_dept',[(100,'n',f'2026-{i:02d}','Alpha')])
            h.delivery([(value,6,'n',f'2026-{i:02d}-10','Alpha')])
        return h
    def month_query(self,h,**extra):
        return h.query(metric('delivery_target_completion','target',month=None,time_range={'start':'2026-01-01','end':'2026-09-01'},time_bucket='month',attribution_mode='transaction_detail',**extra))
    def test_month_ranks_and_selected_period_sums_are_checked(self):
        h=self.monthly();r=h.result(self.month_query(h,period_summary={'field':'gap_amount_rmb','periods':['2026-03','2026-05','2026-08']}))
        evidence=r['numeric_evidence'];ranks={x['period']:x['rank'] for x in evidence['period_ranking']['ascending']}
        self.assertEqual(3,ranks['2026-08'])
        s=evidence['period_summary'];self.assertEqual(D(240),D(s['selected_sum']));self.assertEqual(D(390),D(s['window_sum']));self.assertAlmostEqual(240/390,float(s['ratio']))
        self.assertEqual('人民币元',s['sum_unit']);self.assertEqual('比例',s['ratio_unit']);self.assertEqual('selected_period_sum / queried_window_sum',s['basis'])
    def test_gap_ratio_is_not_target_change_contribution(self):
        h=self.target();r=h.result(h.query(metric('delivery_target_completion','target',attribution_mode='transaction_detail')))
        proof=r['numeric_evidence']['ratios'][0]
        self.assertEqual('gap_amount_rmb',proof['numerator_field']);self.assertEqual('target_amount_rmb',proof['denominator_field'])
        self.assertFalse(proof['causal_or_structural_contribution_authorized'])
    def test_two_coverage_denominators_and_record_grain_are_distinct(self):
        case=overdue_cases.OverdueCoverageTests();case.setUp();self.addCleanup(case.doCleanups)
        case.row('due');case.row('not_due',bill='2026-09-01');case.row('unknown',days=None)
        _,r,_=case.query();ratios={x['field']:x for x in r['numeric_evidence']['coverage_ratios']}
        self.assertEqual((2,3),(ratios['eligibility_coverage_rate']['numerator'],ratios['eligibility_coverage_rate']['denominator']))
        self.assertEqual((1,2),(ratios['value_coverage_rate']['numerator'],ratios['value_coverage_rate']['denominator']))
        self.assertEqual('source_records_not_distinct_documents',ratios['value_coverage_rate']['count_grain'])
    def test_zero_period_sum_denominator_retains_signed_components(self):
        h=self.monthly();h.conn.execute('UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=100')
        h.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=80 WHERE delivery_time='2026-01-10'")
        h.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=120 WHERE delivery_time='2026-02-10'")
        r=h.result(self.month_query(h,period_summary={'field':'gap_amount_rmb','periods':['2026-01']}))
        check=r['numeric_evidence']['period_summary'];self.assertEqual(D(20),D(check['selected_sum']))
        self.assertEqual(D(0),D(check['window_sum']));self.assertIsNone(check['ratio']);self.assertEqual('zero_denominator',check['ratio_state'])
    def test_stock_snapshots_are_not_summed_across_months(self):
        h=self.fixture();r=h.query(metric('debt_balance_trend','receivable',month=None,time_range={'start':'2026-01-01','end':'2026-03-01'},time_bucket='month',period_summary={'field':'metric_value','periods':['2026-01']}))
        self.assertEqual('failed',r['status']);self.assertFalse(h.sql_trace)
    def test_month_stored_profit_series_keeps_period_arithmetic(self):
        h=profit_cases.ProfitContractTests();h.setUp();self.addCleanup(h.doCleanups)
        h.customer_rows([('1','2026-07','1','A',100,10,0),('2','2026-08','1','A',100,30,0)])
        r=h.result(h.query(metric('customer_month_gross_profit','profit',month=None,time_range={'start':'2026-07-01','end':'2026-09-01'},time_bucket='month',period_summary={'field':'metric_value','periods':['2026-07']})))
        check=r['numeric_evidence']['period_summary']
        self.assertEqual(D(10),D(check['selected_sum']));self.assertEqual(D(40),D(check['window_sum']));self.assertEqual(D('.25'),D(check['ratio']))
    def test_invalid_or_outside_months_fail_before_querying(self):
        h=self.monthly()
        for periods in [['2026-13'],['2025-01'],['2026-03','2026-03']]:
            before=len(h.sql_trace);r=self.month_query(h,period_summary={'field':'gap_amount_rmb','periods':periods})
            self.assertEqual('failed',r['status']);self.assertEqual(before,len(h.sql_trace))
    def test_truncated_months_do_not_produce_complete_series_ranks_or_sums(self):
        h=self.monthly();r=h.result(self.month_query(h,limit=2,period_summary={'field':'gap_amount_rmb','periods':['2026-01']}))
        self.assertNotIn('period_ranking',r['numeric_evidence']);self.assertEqual('unavailable',r['numeric_evidence']['period_summary']['status'])
    def test_invalid_claims_cannot_produce_numeric_checks(self):
        h=self.monthly();original=public.plugin.tools._seal_claim_ids
        def corrupt(results):
            original(results)
            for r in results:
                for claim in r.get('claim_ledger',[]):claim['claim_seal']='broken'
        with patch.object(public.plugin.tools,'_seal_claim_ids',corrupt):r=self.month_query(h,period_summary={'field':'gap_amount_rmb','periods':['2026-03']})
        self.assertEqual('failed',r['status']);self.assertNotIn('numeric_evidence',r['results'][0])
    def test_missing_month_and_nonadditive_period_summary_are_not_filled(self):
        h=self.monthly();q={'field':'gap_amount_rmb','periods':['2026-03','2026-05']}
        h.conn.execute("DELETE FROM vk_dwd.delivery_target_detail_dwd WHERE year_month='2026-02'")
        h.conn.execute("DELETE FROM vk_dwd.sale_bill_goods_detail_dwd WHERE delivery_time='2026-02-10'")
        r=h.result(self.month_query(h,period_summary=q));self.assertEqual('unavailable',r['numeric_evidence']['period_summary']['status'])
        before=len(h.sql_trace);r=self.month_query(h,period_summary={'field':'completion_rate','periods':['2026-03']})
        self.assertEqual('failed',r['status']);self.assertEqual(before,len(h.sql_trace))
    def test_expert_index_reduces_repeated_long_text_but_keeps_exact_detail(self):
        h=self.fixture();domains=['delivery','receivable','receipt','inventory','target']
        r=h.invoke('datasage_catalog',{'requests':[{'domain':d,'view':'expert_index'} for d in domains]})
        expanded=h.invoke('datasage_catalog',{'requests':[{'domain':d} for d in domains]})
        self.assertEqual('success',r['status']);self.assertLess(len(json.dumps(r,ensure_ascii=False)),len(json.dumps(expanded,ensure_ascii=False))*.8)
        for index,original in zip(r['results'],expanded['results']):
            expected={m['code']:m for m in original['metrics']}
            self.assertEqual(set(expected),{m['code'] for m in index['metrics']})
            for raw in index['metrics']:
                m={**index.get('metric_defaults',{}),**raw}
                self.assertEqual(expected[m['code']].get('allowed_dimensions',[]),index['allowed_dimension_sets'][m['allowed_dimension_set']])
                for key in ['unit','time_policy','comparison_kinds','operation_summary','allowed_attribution_modes']:
                    self.assertEqual(expected[m['code']].get(key),m.get(key))
        detail=h.invoke('datasage_catalog',{'requests':[{'domain':'target','metric':'delivery_target_completion'}]})
        self.assertIn('gap_amount_rmb',json.dumps(detail));self.assertIn('business_definition',detail['results'][0]['metric'])

if __name__=='__main__':unittest.main()
