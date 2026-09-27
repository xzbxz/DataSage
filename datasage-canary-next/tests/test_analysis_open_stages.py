"""Execute independent row-versus-group and credit-grain counterexamples."""
from datetime import date, timedelta
import unittest

import test_analysis_slices_sql as harness


class OpenReceivableStageTests(unittest.TestCase):
    def setUp(self):
        self.db = harness.AnalysisSliceContractTests._sqlite()
        self.addCleanup(self.db.close)
        self.datasets, self.semantics = harness.contracts.execution_contracts('receivable')
        for customer, amount, overdue in [('A', 6000, 5), ('A', 12000, 40), ('B', 4000, 5), ('B', 8000, 40)]:
            day = (date(2026, 9, 2) - timedelta(days=30 + overdue)).isoformat()
            self.db.execute('INSERT INTO vk_dwd.receivable_bill_detail_dwd VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (customer, customer, customer, 'D', 'ORG', 'CNY', amount, 1, day, 'C', 'n'))
        self.db.executemany('INSERT INTO vk_dwd.customer_credit_dwd VALUES (?,?,?,?)',
                            [('A', 'ORG', 'CNY', 30), ('B', 'ORG', 'CNY', 30)])

    def query(self, row=(), group=()):
        request = {'metric': 'open_receivable_amount', 'domain': 'receivable', 'dimensions': ['customer'],
                   'currency_basis': 'rmb', 'analysis': {'row_filters': list(row), 'group_filters': list(group)}}
        sql, params, scope = harness.analysis_queries.build_open_receivable_analysis_query(
            request, self.semantics['metrics'][request['metric']], self.datasets, self.semantics, 20)
        return [dict(row) for row in self.db.execute(sql.replace('%s', '?').replace('<=>', 'IS'), params)]

    @staticmethod
    def condition(field, op, value):
        return {'field': field, 'op': op, 'value': str(value)}

    def test_customer_total_exists_and_filtered_subtotal_are_different(self):
        amount = [self.condition('metric_value', 'gte', 10000), self.condition('metric_value', 'lte', 50000)]
        total = self.query(group=[*amount, self.condition('any_overdue_days', 'gt', 30)])
        self.assertEqual({'A': 18000, 'B': 12000}, {r['customer_id']: r['metric_value'] for r in total})
        late = self.query(row=[self.condition('overdue_days', 'gt', 30)], group=amount)
        self.assertEqual({'A': 12000}, {r['customer_id']: r['metric_value'] for r in late})
        self.assertEqual((2, 1, 0, 1), tuple(late[0]['analysis_' + k + '_count'] for k in ('population', 'match', 'unknown', 'excluded')))

    def test_row_and_exists_conditions_cannot_be_satisfied_by_different_items(self):
        rows = self.query(row=[self.condition('overdue_days', 'gt', 30)],
                          group=[self.condition('any_overdue_days', 'lt', 10)])
        self.assertEqual(1, len(rows))
        self.assertEqual('coverage_only', rows[0]['analysis_match_state'])
        self.assertEqual(2, rows[0]['analysis_excluded_count'])
        self.assertEqual(0, rows[0]['__matched_row_count'])

    def test_duplicate_credit_does_not_multiply_amount_and_conflict_is_unknown(self):
        self.db.execute('INSERT INTO vk_dwd.customer_credit_dwd VALUES (?,?,?,?)', ('A', 'ORG', 'CNY', 30))
        group = [self.condition('any_overdue_days', 'gt', 30)]
        rows = self.query(group=group)
        self.assertEqual(18000, next(r for r in rows if r['customer_id'] == 'A')['metric_value'])
        self.db.execute('INSERT INTO vk_dwd.customer_credit_dwd VALUES (?,?,?,?)', ('A', 'ORG', 'CNY', 60))
        unknown = next(r for r in self.query(group=group) if r['customer_id'] == 'A')
        self.assertEqual(18000, unknown['metric_value'])
        self.assertEqual('unknown', unknown['analysis_match_state'])
        self.assertEqual(2, unknown['analysis_overdue_unknown_count'])

    def test_all_excluded_and_empty_parent_keep_different_coverage(self):
        condition = [self.condition('metric_value', 'gt', 99999)]
        excluded = self.query(group=condition)[0]
        self.assertEqual((2, 2), (excluded['analysis_population_count'], excluded['analysis_excluded_count']))
        self.db.execute('DELETE FROM vk_dwd.receivable_bill_detail_dwd')
        empty = self.query(group=condition)[0]
        self.assertEqual((0, 0), (empty['analysis_population_count'], empty['analysis_excluded_count']))
        self.assertEqual('2026-09-02', empty['__as_of_date'])


if __name__ == '__main__':
    unittest.main()
