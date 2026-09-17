import unittest,importlib
import test_business_contracts as base
m=importlib.import_module(base.TEST_PACKAGE+'.workflow_manual_boundaries')
s=m.slow
class ManualBoundaryTests(unittest.TestCase):
    def test_synthetic_period_checks_do_not_need_database_or_sender(self):
        result=m.calculate()
        self.assertTrue(result['next_week_independent_scope']);self.assertEqual(result['real_or_test_freeze_rows_written'],0)
        self.assertFalse(result['system_or_real_observation_clock_changed'])
    def test_explicit_generation_does_not_skip_unknown(self):
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):s.generation_number({'status':'unknown','payload':{'generation':2}},force=True,reason='Synthetic manual check')
    def test_generation_budget_is_closed(self):
        with self.assertRaisesRegex(ValueError,'BUDGET'):s.generation_number({'status':'committed','payload':{'generation':999}},force=True,reason='Synthetic manual check')
if __name__=='__main__':unittest.main()
