"""The removed domain loses no metric or SQL capability."""
import unittest
import test_remediation_remaining_cases as public

class DomainConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.h=public.RemainingCaseTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
    def test_domains_and_five_migrated_metrics_are_visible(self):
        from importlib import import_module
        cap=import_module(public.plugin.__name__+'.capability_contract')
        self.assertEqual({'delivery','receipt','receivable','target','inventory','profit','pattern_matching'},set(cap.DOMAIN_SOURCES))
        for code in ('average_settlement_days','maximum_settlement_days','settlement_days_distribution','formal_receivable_turnover_days','delivery_receipt_comparison'):
            domain='receipt' if code=='delivery_receipt_comparison' else 'receivable'
            response=self.h.invoke('datasage_catalog',{'requests':[{'domain':domain,'metric':code}]})
            self.assertEqual('success',response['status'],response)
            self.assertEqual(code,response['results'][0]['metric']['code'])
    def test_retired_domain_is_rejected_without_SQL(self):
        response=self.h.query(public.metric('formal_receivable_turnover_days','customer_risk',month=None))
        self.assertEqual('failed',response['status'])
        self.assertFalse(self.h.sql_trace)
        response=self.h.invoke('datasage_catalog',{'requests':[{'domain':'customer_risk'}]})
        self.assertEqual('failed',response['status'])

if __name__=='__main__':unittest.main()
