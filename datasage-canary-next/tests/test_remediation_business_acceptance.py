"""Sanity-check hand-authored business fixtures, not model answer accuracy."""
from decimal import Decimal
import json
from pathlib import Path
import unittest


class BusinessAcceptanceFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = json.loads((Path(__file__).parent / 'fixtures/business_acceptance_cases.json').read_text(encoding='utf-8'))
        cls.cases = {item['id']: item for item in cls.suite['cases']}

    def test_reference_arithmetic_is_independently_recomputed(self):
        number = lambda value: Decimal(str(value))
        for case in self.cases.values():
            for operation, keys, output in case['arithmetic_checks']:
                with self.subTest(case=case['id'], output=output):
                    values = [case['source_fixture'][key] for key in keys]
                    if operation == 'sum': actual = sum(map(number, values[0]))
                    elif operation == 'positive_sum': actual = sum(number(value) for value in values[0] if value > 0)
                    elif operation == 'net_sum': actual = sum(map(number, values[0])) - sum(map(number, values[1]))
                    elif operation == 'difference': actual = number(values[0]) - number(values[1])
                    elif operation == 'ratio': actual = number(values[0]) / number(values[1])
                    else: self.fail(f'unsupported fixture check: {operation}')
                    self.assertEqual(number(case['expected_facts'][output]), actual)

    def test_missing_rate_and_target_reference_values(self):
        case = self.cases['B05']
        rows = case['source_fixture']['receipt_rows']
        known = [row['amount'] * row['exchange_rate'] for row in rows if row['amount'] is not None and row['exchange_rate'] is not None]
        self.assertEqual(sum(known), case['expected_facts']['known_partial_rmb'])
        self.assertIsNone(case['expected_facts']['complete_rmb_total'])
        self.assertEqual(len(rows) - len(known), case['expected_facts']['missing_rows'])
        case = self.cases['B10']
        source, expected = case['source_fixture'], case['expected_facts']
        actual = sum(source['allocated_delivery_rmb']) - sum(source['allocated_returns_rmb'])
        self.assertEqual(Decimal(str(expected['completion_rate'])), Decimal(actual) / source['target_rmb'])
        self.assertEqual(expected['shortfall_rmb'], source['target_rmb'] - actual)

    def test_department_contributions_reconcile_without_causal_claims(self):
        case = self.cases['B14']
        source, expected = case['source_fixture'], case['expected_facts']
        deltas = {row['department']: row['current'] - row['prior'] for row in source['department_rows']}
        total = source['current_total'] - source['prior_total']
        self.assertEqual(total, sum(deltas.values()))
        for department, value in deltas.items():
            self.assertEqual(expected[department + '_delta'], value)
            self.assertEqual(Decimal(str(expected[department + '_contribution'])), Decimal(value) / total)
        self.assertTrue(case['forbidden_answer_properties'])

    def test_coverage_and_unmeasured_status_are_explicit(self):
        self.assertEqual(18, len(self.cases))
        self.assertEqual(18, len(self.suite['cases']))
        expected_case_domains = ('delivery', 'receipt', 'receivable', 'inventory', 'target')
        self.assertEqual(len(expected_case_domains), len(set(expected_case_domains)))
        self.assertEqual(set(expected_case_domains),
                         {domain for case in self.cases.values() for domain in case['domains']})
        self.assertEqual('hand_authored_independent_of_plugin_contracts', self.suite['expected_result_origin'])
        self.assertTrue(self.suite['not_a_model_accuracy_result'])
        self.assertEqual(7, len(self.suite['evaluation_axes']))
        for case in self.cases.values():
            self.assertEqual('not_run', case['execution_status'])
            self.assertEqual('pending', case['independent_business_review_status'])
            self.assertTrue(case['turns'])
            self.assertTrue(case['required_answer_properties'])
            self.assertTrue(case['forbidden_answer_properties'])


if __name__ == '__main__': unittest.main()
