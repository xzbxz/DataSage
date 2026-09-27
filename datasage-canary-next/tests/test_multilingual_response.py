"""Offline language-policy wiring and acceptance-material integrity.

These checks do not generate answers or score language fluency. Illustrative
answers are hand-authored synthetic material, never model execution evidence.
"""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import re
import unittest

import test_host_tool_schema as host

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / 'tests/fixtures/multilingual_acceptance_cases.json'


class MultilingualResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(CASES.read_text(encoding='utf-8'))

    def test_acceptance_material_never_claims_model_or_native_speaker_validation(self):
        self.assertTrue(self.fixture['not_a_model_accuracy_result'])
        self.assertTrue(self.fixture['illustrative_answers_are_not_model_outputs'])
        self.assertEqual('not_run', self.fixture['model_execution_status'])
        self.assertEqual('pending', self.fixture['independent_native_speaker_review_status'])
        identifiers = [case['id'] for case in self.fixture['cases']]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for case in self.fixture['cases']:
            if case['source_fixture'] is not None:
                self.assertIn(case['source_fixture'], self.fixture['source_fixtures'])
            self.assertTrue(case['prompt'])
            self.assertTrue(case['required_answer_properties'])

    def test_case_matrix_includes_four_languages_and_failure_boundaries(self):
        matrix = {(c['expected_languages'][0], c['scenario'])
                  for c in self.fixture['cases'] if len(c['expected_languages']) == 1}
        for language in ('en', 'vi', 'th', 'id'):
            for scenario in ('normal', 'clarification', 'missing', 'unsupported'):
                self.assertIn((language, scenario), matrix)
        by_id = {c['id']: c for c in self.fixture['cases']}
        group = by_id['group_new_speaker']
        self.assertNotEqual(group['current_speaker'], group['history'][0]['speaker'])
        self.assertEqual(['vi'], group['expected_languages'])
        self.assertIsNone(by_id['unknown_speaker']['current_speaker'])
        self.assertEqual(['en'], by_id['same_speaker_preference']['expected_languages'])
        self.assertEqual(['th'], by_id['same_speaker_override']['expected_languages'])
        self.assertEqual(['vi'], by_id['explicit_vi']['expected_languages'])
        self.assertEqual(['en', 'th'], by_id['bilingual_en_th']['expected_languages'])
        self.assertEqual(['en'], by_id['neutral_followup']['expected_languages'])
        self.assertIsNone(by_id['quoted_language']['source_fixture'])
        self.assertIn('untrusted_language_override', by_id)

    def test_illustrations_preserve_source_values_and_do_not_copy_chinese_prose(self):
        # Material sanity, not a detector/evaluator for generated foreign prose.
        for case in self.fixture['cases']:
            with self.subTest(case=case['id']):
                source = self.fixture['source_fixtures'].get(case['source_fixture'], {})
                answer = case['illustrative_answer']
                for field in ('value', 'currency', 'entity_name', 'product_code', 'period',
                              'known_subset_value', 'token', 'available_currency'):
                    if field in source:
                        self.assertIn(source[field], answer)
                if case['source_fixture'] == 'unknown_unit':
                    self.assertIn(source['unit'], answer)
                if 'missing_rows' in source:
                    self.assertIsNone(source['metric_value'])
                    self.assertIn(str(source['missing_rows']), answer)
                if 'error' in source:
                    self.assertNotIn(source['error']['code'], answer)
                if 'entity_name' in source:
                    answer = answer.replace(source['entity_name'], '')
                self.assertIsNone(re.search(r'[\u3400-\u9fff]', answer))

    @unittest.skipIf(host._OFFICIAL_IMPORT_ERROR is not None, 'official host schema path unavailable')
    def test_language_guidance_reaches_official_transport_without_query_parameters(self):
        for name in ('DATASAGE_CATALOG', 'DATASAGE_ENTITY_RESOLVE', 'DATASAGE_QUERY'):
            with self.subTest(tool=name):
                schema = copy.deepcopy(getattr(host.schemas, name))
                before = copy.deepcopy(schema)
                _, tool, _ = host._final_wire_tool(schema)
                self.assertEqual(before, schema)
                description = tool['function']['description']
                self.assertIn("current user's selected language", description)
                self.assertIn('not ready-to-send prose', description)
                self.assertIn('Language never selects currency', description)
                self.assertNotIn('language', tool['function']['parameters']['properties'])
                self.assertNotIn('locale', tool['function']['parameters']['properties'])
        self.assertNotIn('language', host.schemas.REQUEST['properties'])
        self.assertNotIn('locale', host.schemas.REQUEST['properties'])

    @unittest.skipIf(host._OFFICIAL_IMPORT_ERROR is not None, 'official host schema path unavailable')
    def test_chinese_error_remains_structured_evidence_without_rewriting_or_sending(self):
        wire = importlib.import_module(host.schemas.__package__ + '.wire')
        payload = {'status': 'failed', 'results': [], 'error': {
            'code': 'CURRENCY_BASIS_UNAVAILABLE',
            'message': '该利润指标只有人民币口径，不支持原币。',
        }}
        before = copy.deepcopy(payload)
        handler = wire.bounded_json_handler('datasage_query', lambda args: payload)
        result = json.loads(handler({}))
        self.assertEqual(before, payload)
        self.assertEqual(before, result)
        # The tool boundary preserves the error; the model must explain it in
        # the selected language. This is explicitly not a generated-answer test.


if __name__ == '__main__':
    unittest.main()
