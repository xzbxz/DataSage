import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
E2E_ROOT = PLUGIN_ROOT / "e2e"
SPEC = importlib.util.spec_from_file_location("datasage_offline_e2e", E2E_ROOT / "runner.py")
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(RUNNER)


class OfflineE2ETests(unittest.TestCase):
    def setUp(self):
        self.cases_path = E2E_ROOT / "cases.json"
        self.replay_path = E2E_ROOT / "fixtures" / "offline_replay.json"
        self.config_path = PLUGIN_ROOT.parents[1] / "config.yaml"

    def _run_modified_replay(self, mutate):
        replay = json.loads(self.replay_path.read_text(encoding="utf-8"))
        mutate(replay)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.json"
            path.write_text(json.dumps(replay, ensure_ascii=False), encoding="utf-8")
            return RUNNER.run(self.cases_path, path, self.config_path)

    def test_release_suite_has_26_cases_and_two_multiturn_regressions(self):
        suite = json.loads(self.cases_path.read_text(encoding="utf-8"))
        self.assertEqual(26, len(suite["cases"]))
        self.assertEqual({"7_ar_dso", "18_mt_region2"}, set(suite["known_regressions"]))
        conversations = {}
        for case in suite["cases"]:
            conversations.setdefault(case["conversation_id"], []).append(case)
            oracle = case["oracle"]
            self.assertIn("allowed_metrics", oracle)
            self.assertIn("allowed_domains", oracle)
            self.assertIn("allowed_entities", oracle)
            self.assertIn("allowed_periods", oracle)
            self.assertIn("numeric_assertions", oracle)
        self.assertEqual(3, len(conversations["c17_region"]))
        self.assertEqual(2, len(conversations["c20_entity"]))

    def test_offline_replay_passes_all_three_layers(self):
        report = RUNNER.run(self.cases_path, self.replay_path, self.config_path)
        self.assertEqual(26, report["summary"]["total"])
        self.assertEqual(26, report["summary"]["process_exit"]["passed"])
        self.assertEqual(26, report["summary"]["tool_success"]["passed"])
        self.assertEqual(26, report["summary"]["business_correct"]["passed"])
        self.assertEqual(0, report["summary"]["timeouts"])

    def test_exit_zero_does_not_imply_business_correct(self):
        def mutate(replay):
            row = next(item for item in replay["cases"] if item["id"] == "7_ar_dso")
            row["semantic"]["metric"] = "debt_days"
            row["answer"] = "这个指标没有，查不出来。"

        report = self._run_modified_replay(mutate)
        row = next(item for item in report["results"] if item["id"] == "7_ar_dso")
        self.assertTrue(row["process_exit"])
        self.assertTrue(row["tool_success"])
        self.assertFalse(row["business_correct"])

    def test_dso_headline_detail_contradiction_fails_business_oracle(self):
        def mutate(replay):
            row = next(item for item in replay["cases"] if item["id"] == "7_ar_dso")
            row["numeric_facts"]["detail_dso_days"] = 38.2

        report = self._run_modified_replay(mutate)
        row = next(item for item in report["results"] if item["id"] == "7_ar_dso")
        self.assertTrue(row["process_exit"])
        self.assertTrue(row["tool_success"])
        self.assertFalse(row["business_correct"])
        self.assertTrue(any("numeric contradiction" in error for error in row["errors"]["business"]))

    def test_wecom_surface_rejects_private_or_disabled_tool(self):
        def mutate(replay):
            row = replay["cases"][0]
            row["tool_calls"].append({"name": "session_search", "status": "success"})

        report = self._run_modified_replay(mutate)
        row = report["results"][0]
        self.assertTrue(row["process_exit"])
        self.assertFalse(row["tool_success"])
        self.assertFalse(row["business_correct"])

    def test_follow_up_must_keep_session_and_replace_entity_only(self):
        def mutate(replay):
            row = next(item for item in replay["cases"] if item["id"] == "18_mt_region2")
            row["session_id"] = "new-session"
            row["semantic"]["metric"] = "receipt_target_completion"

        report = self._run_modified_replay(mutate)
        row = next(item for item in report["results"] if item["id"] == "18_mt_region2")
        self.assertFalse(row["business_correct"])
        self.assertTrue(any("session_id" in error for error in row["errors"]["business"]))

    def test_original_currency_follow_up_preserves_scope_and_session(self):
        report = RUNNER.run(self.cases_path, self.replay_path, self.config_path)
        q25 = next(item for item in report["results"] if item["id"] == "25_rcp_top20")
        q26 = next(item for item in report["results"] if item["id"] == "26_rcp_original")
        self.assertTrue(q25["business_correct"])
        self.assertTrue(q26["business_correct"])

    def test_live_mode_is_not_the_default_or_implicitly_callable(self):
        source = (E2E_ROOT / "runner.py").read_text(encoding="utf-8")
        self.assertIn('default="offline-replay"', source)
        self.assertIn("no live adapter is shipped", source)


if __name__ == "__main__":
    unittest.main()
