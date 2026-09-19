"""An unproven preview must not turn missing per-product sales facts into zero."""
import copy
import unittest

import test_weekly_acceptance as base


class ReportSalesUnknownTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.WeeklyAcceptanceTests('run')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def preview(self, field):
        evidence = copy.deepcopy(self.fixture.evidence)
        flow = evidence['packets']['flow']
        flow['results'][0]['rows'][0]['facts'][field] = None
        result = base.inputs.legacy_report_packet(
            evidence['packets']['pool'], flow, self.fixture.labels, 'HCM', '2026-W38'
        )
        return result

    def test_missing_rolls_remain_unknown_in_sold_by(self):
        result = self.preview('net_rolls')
        self.assertTrue(any('Unknown rolls' in str(row[11]) for row in result['detail_rows']))
        self.assertFalse(result['detail_complete'])

    def test_missing_quantity_remains_unknown_in_sold_by(self):
        result = self.preview('metric_value')
        self.assertTrue(any('Qty: Unknown M' in str(row[11]) for row in result['detail_rows']))
        self.assertFalse(result['detail_complete'])
