"""Formal DSO windows through real SQL/public projection on synthetic SQLite."""
import copy
from decimal import Decimal
import unittest
from unittest.mock import patch
import test_remediation_remaining_cases as public


class DsoWindowTests(unittest.TestCase):
    def setUp(self):
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def seed(self, start, months, *, missing_snapshot=None, null_debt=False,
             null_delivery=False, delivery_value=100, missing_delivery=None):
        self.h.conn.execute('DELETE FROM vk_dw.customer_debt_bymonth_dw')
        self.h.conn.execute('DELETE FROM vk_dwd.sale_bill_goods_detail_dwd')
        year, month = map(int, start[:7].split('-'))
        ordinal = year * 12 + month - 1
        periods = [f'{v//12:04d}-{v%12+1:02d}' for v in range(ordinal-1, ordinal+months)]
        debts = [40, *([100] * (months-1)), 200]
        self.h.insert('vk_dw.customer_debt_bymonth_dw', 'debt_amount_rmb,bill_date,is_inner_cus',
            [(None if null_debt and i==0 else value, period, 'n')
             for i,(period,value) in enumerate(zip(periods, debts)) if i != missing_snapshot])
        self.h.delivery([(None if null_delivery and i==0 else delivery_value, 6, 'n', period+'-15', 'A')
                         for i,period in enumerate(periods[1:]) if i != missing_delivery])
        return periods

    def query(self, **kwargs):
        return self.h.query(public.metric('formal_receivable_turnover_days', 'receivable',
                                         month=None, **kwargs))

    def assert_verified(self, response, start, end, months, days, average):
        r=self.h.result(response)
        self.assertEqual('rows', r['data_state'], r)
        f=r['rows'][0]['facts']; a=f['calculation_attestation']
        self.assertEqual('verified', a['status'])
        self.assertEqual(start, r['applied_time_range']['start'])
        self.assertEqual(end, r['applied_time_range']['end'])
        self.assertAlmostEqual(float(average), f['average_net_debt_rmb'], places=7)
        self.assertAlmostEqual(float(average * days / (months*100)), f['metric_value'], places=7)
        self.assertEqual(months+1, a['component_values']['snapshot_month_count'])
        self.assertEqual(months, float(a['component_values']['effective_month_count']))
        self.assertEqual(days, f['period_natural_days'])
        self.assertEqual(64,len(response['source_evidence_ref']['source_ref_sha256']))
        trace=self.h.sql_trace[-1]
        self.assertIn(start,trace['params']);self.assertIn(end,trace['params'])
        coverage=next(d['text'] for d in response['disclosures']
                      if d['disclosure_id']=='receivable.formal-receivable-turnover.coverage')
        for fact in (start,end,f'{months}个完整自然月',f'{months+1}个月末',f'{days}个自然日'):
            self.assertIn(fact,coverage)

    def test_default_twelve_months_and_explicit_single_six_thirteen(self):
        # Independent hand-specified dates/days and weighted endpoint arithmetic:
        # opening 40 / 2 + intervening 100 each + closing 200 / 2.
        scenarios=[('2025-09-01','2026-09-01',12,365,Decimal(1220)/12,{}),
                   ('2024-02-01','2024-03-01',1,29,Decimal(120),{'calendar_month':'2024-02'}),
                   ('2024-01-01','2024-07-01',6,182,Decimal(620)/6,{'time_range':{'start':'2024-01-01','end':'2024-07-01'}}),
                   ('2023-08-01','2024-09-01',13,397,Decimal(1320)/13,{'time_range':{'start':'2023-08-01','end':'2024-09-01'}})]
        for start,end,n,days,average,kwargs in scenarios:
            with self.subTest(months=n):
                periods=self.seed(start,n)
                response=self.query(**kwargs)
                self.assert_verified(response,start,end,n,days,average)
                self.assertIn(periods[0],self.h.sql_trace[-1]['params'])
                self.assertIn(periods[-1],self.h.sql_trace[-1]['params'])

    def assert_undefined(self,response):
        r=self.h.result(response)
        self.assertEqual('undefined',r['data_state'],r)
        self.assertTrue(all(row['facts'].get('metric_value') is None for row in r['rows']))
        self.assertTrue(all(row['facts']['calculation_attestation']['status']=='undefined' for row in r['rows']))
        self.assertNotIn('receivable.formal-receivable-turnover.formula', r.get('disclosure_refs',[]))

    def test_missing_opening_middle_closing_stays_undefined(self):
        for changes in ({'missing_snapshot':0},{'missing_snapshot':3},{'missing_snapshot':6}):
            with self.subTest(changes=changes):
                self.seed('2024-01-01',6,**changes)
                self.assert_undefined(self.query(time_range={'start':'2024-01-01','end':'2024-07-01'}))

    def test_zero_balance_months_and_sparse_delivery_do_not_shorten_window(self):
        self.seed('2025-09-01',12)
        self.h.conn.execute("UPDATE vk_dw.customer_debt_bymonth_dw SET debt_amount_rmb=0 WHERE bill_date<'2026-01'")
        self.h.conn.execute("DELETE FROM vk_dwd.sale_bill_goods_detail_dwd WHERE delivery_time<'2026-01-01'")
        response=self.query();r=self.h.result(response);f=r['rows'][0]['facts']
        self.assertEqual('verified',f['calculation_attestation']['status'])
        # Seven Jan-Jul balances of 100, closing August 200/2, full 12 months.
        self.assertAlmostEqual(800/12, f['average_net_debt_rmb'])
        self.assertAlmostEqual((800/12)/800*365, f['metric_value'])
        self.assertEqual(8,f['effective_month_count'])
        self.assertEqual('2025-09-01',r['applied_time_range']['start'])
        # A stored zero flow is also valid; it is not a missing monetary input.
        self.h.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=0 WHERE delivery_time='2026-01-15'")
        f=self.h.result(self.query())['rows'][0]['facts']
        self.assertEqual('verified',f['calculation_attestation']['status'])
        self.assertEqual(7,f['effective_month_count'])
        self.assertAlmostEqual((800/12)/700*365,f['metric_value'])
        # Removing a stored zero balance is a missing snapshot, not implicit zero.
        self.h.conn.execute("DELETE FROM vk_dw.customer_debt_bymonth_dw WHERE bill_date='2025-10'")
        self.assert_undefined(self.query())

    def test_null_inputs_zero_and_negative_denominator_stay_undefined(self):
        for changes in ({'null_debt':True},{'null_delivery':True},{'delivery_value':0},{'delivery_value':-100}):
            with self.subTest(changes=changes):
                self.seed('2024-02-01',1,**changes)
                self.assert_undefined(self.query(calendar_month='2024-02'))

    def test_counts_are_checked_against_dates_not_trusted_from_SQL(self):
        self.seed('2024-02-01',1)
        execute=self.h.execute
        for field,value in [('snapshot_month_count',13),('effective_month_count',12),('period_natural_days',365)]:
            with self.subTest(field=field):
                def corrupted(*a,**kw):
                    rows,truncated,source=execute(*a,**kw)
                    rows=copy.deepcopy(rows)
                    for row in rows:row[field]=value
                    return rows,truncated,source
                with patch.object(public.plugin.tools,'_execute_with_source',side_effect=corrupted):
                    self.assert_undefined(self.query(calendar_month='2024-02'))

    def test_resealed_components_cannot_override_actual_window(self):
        self.seed('2024-02-01',1)
        response=self.query(calendar_month='2024-02');r=self.h.result(response)
        facts=r['rows'][0]['facts'];components=facts['calculation_attestation']['component_values']
        validate=public.plugin.tools._formal_dso_attested_components_are_valid
        self.assertTrue(validate(components,facts=facts,applied_time_range=r['applied_time_range']))
        for period in ({'start':'2024-01-01','end':'2024-07-01'}, {'start':'2024-03-01','end':'2024-04-01'}):
            self.assertFalse(validate(components,facts=facts,applied_time_range=period))
        for field,value in [('snapshot_month_count',13),('effective_month_count',12)]:
            changed={**components,field:value};changed_facts={**facts,field:value}
            self.assertFalse(validate(changed,facts=changed_facts,applied_time_range=r['applied_time_range']))

    def test_signed_and_offset_balances_keep_formula_values_and_interpretation(self):
        # One completed 30-day month, sales 1000. Negative is not abs/zero/undefined.
        for opening, closing, expected in [(100,100,3),(-100,-100,-3),(0,0,0),(100,-100,0)]:
            with self.subTest(opening=opening,closing=closing):
                self.seed('2026-06-01',1,delivery_value=1000)
                self.h.conn.execute("UPDATE vk_dw.customer_debt_bymonth_dw SET debt_amount_rmb=? WHERE bill_date='2026-05'",(opening,))
                self.h.conn.execute("UPDATE vk_dw.customer_debt_bymonth_dw SET debt_amount_rmb=? WHERE bill_date='2026-06'",(closing,))
                response=self.query(calendar_month='2026-06');r=self.h.result(response);f=r['rows'][0]['facts']
                self.assertEqual('zero' if expected == 0 else 'rows',r['data_state'])
                self.assertEqual(expected,f['metric_value'])
                self.assertEqual((opening+closing)/2,f['average_net_debt_rmb'])
                self.assertEqual('verified',f['calculation_attestation']['status'])
                # Delivery of metric meaning, not a claim about model compliance.
                explanation=next(d['text'] for d in response['disclosures']
                    if d['disclosure_id']=='receivable.formal-receivable-turnover.formula')
                self.assertIn('不代表提前付款天数',explanation)
                self.assertIn('正负抵消',explanation)

    def test_current_future_partial_and_conflicting_months_rejected_before_SQL(self):
        for kwargs in ({'calendar_month':'2026-09'},{'calendar_month':'2027-01'},
                       {'time_range':{'start':'2024-02-02','end':'2024-03-01'}},
                       {'time_range':{'start':'2024-02-01','end':'2024-02-29'}},
                       {'calendar_month':'2024-02','time_range':{'start':'2024-02-01','end':'2024-03-01'}}):
            with self.subTest(kwargs=kwargs):
                before=len(self.h.sql_trace);response=self.query(**kwargs)
                self.assertEqual('failed',response['status'])
                self.assertFalse(any(r.get('rows') for r in response.get('results',[])))
                self.assertEqual(before,len(self.h.sql_trace))

if __name__=='__main__':unittest.main()
