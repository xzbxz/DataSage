"""R20: one returned result backs the answer, the table and the saved artifacts.

The display layer must reuse the evidence already returned - not re-read it and not
mutate it - keep unknown distinct from zero, and carry the returned scope line so the
answer stays traceable.  The Skill's answer boundary states the same rules per answer
type, and the last test keeps that statement honest.
"""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from gateway.session_context import clear_session_vars, set_session_vars

import test_slow_progress as progress
from test_remediation_remaining_cases import plugin

report = importlib.import_module(plugin.__name__ + ".local_report")
REFERENCES = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "business-analytics"
    / "datasage"
    / "references"
)


def synthetic_bindings(**changes):
    return {
        "version": 1,
        "default_report": "synthetic",
        "reports": {
            "synthetic": {
                "department": "A",
                "baseline_week": "2026-W37",
                "max_baseline_age_days": 7,
                "views": ["flow_summary"],
                "limit": 10,
                **changes,
            }
        },
    }


class AnswerPresentationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = progress.ProgressTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h = self.fixture.h
        self.tokens = set_session_vars()
        self.addCleanup(clear_session_vars, self.tokens)

    def _report(self, **changes):
        return report.execute_report(synthetic_bindings(**changes))

    def test_rendering_is_repeatable_and_never_rewrites_the_returned_evidence(self):
        self.h.outgoing(10)
        self.h.returning(3)
        result = self._report()
        before = copy.deepcopy(result)

        first = report.render_text(result)
        second = report.render_text(result)

        self.assertEqual(first, second, "the same result must render identically")
        self.assertEqual(before, result, "rendering must not mutate the evidence")
        self.assertIn("滞销范围观察", first)

    def test_text_and_saved_artifacts_share_one_result(self):
        self.h.outgoing(10)
        self.h.returning(3)
        result = self._report()
        text = report.render_text(result)

        with tempfile.TemporaryDirectory(prefix="datasage-r20-") as temporary:
            run = report.save_artifacts(Path(temporary), result, text)
            saved_document = json.loads((run / "report.json").read_text(encoding="utf-8"))
            saved_text = (run / "report.txt").read_text(encoding="utf-8")

        self.assertEqual(text, saved_text, "the file must carry the rendered evidence")
        self.assertEqual(
            result["query"]["results"], saved_document["query"]["results"]
        )
        # Every total the text shows must be the returned one, not a re-derived value.
        for line in text.splitlines():
            if "总卷数" not in line:
                continue
            shown = line.rsplit("：", 1)[1].replace(" 卷", "").strip()
            with self.subTest(line=line):
                self.assertTrue(
                    any(
                        str(fact.get("scope_net_rolls")) == shown or
                        str(fact.get("scope_high_net_rolls")) == shown
                        for per_result in saved_document["query"]["results"]
                        for row in per_result.get("rows", [])
                        for fact in [row.get("facts", {})]
                    ),
                    "a total in the text must come from the returned result",
                )
        self.assertNotIn("未取得有效查询范围", text, "the returned scope must be shown")
        self.assertEqual(
            "local OS operator; not WeCom authorization",
            saved_document["runtime"]["identity_basis"],
        )

    def test_unknown_is_rendered_as_unknown_and_never_as_zero(self):
        self.h.outgoing(None)
        self.h.returning(4)
        text = report.render_text(self._report())
        self.assertIn("已记录净数量：未知", text)
        self.assertIn("净数量已知部分：-4", text)
        self.assertNotIn("已记录净数量：0", text)

    def test_answer_boundary_states_minimal_content_per_answer_type(self):
        boundary = " ".join(
            (REFERENCES / "answer-boundary.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("## Minimal content by answer type", boundary)
        for answer_type in (
            "**Fact lookup**",
            "**Explanation**",
            "**Diagnosis**",
            "**Management report**",
        ):
            with self.subTest(answer_type=answer_type):
                self.assertIn(answer_type, boundary)
        self.assertIn("None of them prescribes sections, a fixed order", boundary)
        self.assertIn("reuse the facts already returned in this conversation", boundary)
        self.assertIn("never render unknown, empty, pending or failed as zero", boundary)
        self.assertIn("consistent with the same returned result", boundary)


if __name__ == "__main__":
    unittest.main()
