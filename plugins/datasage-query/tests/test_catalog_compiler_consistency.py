from __future__ import annotations

import importlib
import itertools
import json
import os
import sys
import types
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
PACKAGE = "catalog_compiler_consistency_test_package"
os.environ["HERMES_HOME"] = str(REPO_ROOT)


def _load_package_module(name: str):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


contracts = _load_package_module("contracts")
tools = _load_package_module("tools")


EXPECTED_NON_PROMISED_PAIRS = {
    ("customer_risk", "delivery_receipt_comparison", "customer", "department"),
    ("customer_risk", "delivery_receipt_comparison", "customer", "organization"),
    ("customer_risk", "delivery_receipt_comparison", "department", "organization"),
    ("customer_risk", "formal_receivable_turnover_days", "customer", "department"),
    ("customer_risk", "formal_receivable_turnover_days", "customer", "organization"),
    ("customer_risk", "formal_receivable_turnover_days", "customer", "salesperson"),
    ("customer_risk", "formal_receivable_turnover_days", "department", "organization"),
    ("customer_risk", "formal_receivable_turnover_days", "department", "salesperson"),
    ("customer_risk", "formal_receivable_turnover_days", "organization", "salesperson"),
}


def _metric_dimensions(planner: dict, metric: dict) -> list[str]:
    direct = metric.get("allowed_dimensions")
    if isinstance(direct, list):
        return [str(value) for value in direct]
    dimension_sets = planner.get("allowed_dimension_sets") or {}
    return [str(value) for value in dimension_sets.get(metric.get("allowed_dimension_set"), [])]


def _offline_request(domain: str, metric_code: str, metric: dict, pair: tuple[str, str]) -> dict:
    request = {
        "request_id": "catalog-matrix",
        "domain": domain,
        "mode": "metric",
        "purpose": "offline catalog/compiler consistency",
        "metric": metric_code,
        "dimensions": list(pair),
    }
    if metric.get("time_policy") == "required":
        request["time_range"] = {
            "start": "2026-05-01",
            "end": "2026-07-01",
        }
    if domain == "target":
        request["attribution_mode"] = metric.get("required_attribution_mode") or (
            metric.get("allowed_attribution_modes") or ["transaction_detail"]
        )[0]
    currency_policy = metric.get("currency_policy")
    if "currency" not in pair and (currency_policy == "group_or_filter" or (
        isinstance(currency_policy, dict)
        and currency_policy.get("mode") == "original_currency"
        and currency_policy.get("require_filter_or_group") is True
    )):
        request.setdefault("metric_filters", {})["currency"] = "CNY"
    unit_policy = metric.get("unit_policy")
    if "unit" not in pair and (unit_policy == "group_or_filter" or (
        isinstance(unit_policy, dict)
        and unit_policy.get("mode") == "group_or_filter"
    )):
        request.setdefault("metric_filters", {})["unit"] = "EA"
    return request


class CatalogCompilerConsistencyTests(unittest.TestCase):
    def test_every_catalog_promised_dimension_pair_compiles_offline(self) -> None:
        candidate_count = 0
        promised_count = 0
        non_promised: set[tuple[str, str, str, str]] = set()
        compile_failures: list[tuple[str, str, tuple[str, str], str]] = []

        for domain in contracts._DOMAIN_FOLDERS:
            planner = contracts._domain_contract(domain, "planner")["planner"]
            datasets, semantics = tools._contracts(domain)
            for projected_metric in planner["metrics"]:
                metric_code = projected_metric["code"]
                dimensions = _metric_dimensions(planner, projected_metric)
                maximum = projected_metric["max_group_dimensions"]
                metric = semantics["metrics"][metric_code]
                for pair in itertools.combinations(dimensions, 2):
                    candidate_count += 1
                    if len(pair) > maximum:
                        non_promised.add((domain, metric_code, *pair))
                        continue
                    promised_count += 1
                    request = _offline_request(domain, metric_code, metric, pair)
                    try:
                        tools._build_metric_query(request, datasets, semantics, 100)
                    except tools.QueryFailure as exc:
                        compile_failures.append((domain, metric_code, pair, exc.code))

        self.assertEqual(candidate_count, 24_382)
        self.assertEqual(promised_count, 24_373)
        self.assertEqual(non_promised, EXPECTED_NON_PROMISED_PAIRS)
        self.assertEqual(compile_failures, [])

    def test_catalog_guidance_is_a_bounded_projection_of_planner_authority(self) -> None:
        for domain, folder in contracts._DOMAIN_FOLDERS.items():
            planner = contracts._domain_contract(domain, "planner")["planner"]
            summary = contracts._catalog_summary(domain, planner)
            projected = summary["planning_guidance"]

            self.assertEqual(
                projected["version"],
                contracts._CATALOG_PLANNING_GUIDANCE_VERSION,
            )
            for key in contracts._CATALOG_PLANNING_GUIDANCE_KEYS:
                if key in planner["guidance"]:
                    self.assertEqual(projected[key], planner["guidance"][key])
            self.assertLessEqual(
                len(json.dumps(projected, ensure_ascii=False)),
                contracts._MAX_CATALOG_PLANNING_GUIDANCE_JSON_CHARS,
            )

            metric_code = planner["metrics"][0]["code"]
            detail = contracts._catalog_metric_detail(domain, metric_code, planner)
            self.assertEqual(detail["planning_guidance"], projected)

    def test_future_target_skill_uses_executor_state_name(self) -> None:
        skill = (
            REPO_ROOT
            / "skills"
            / "datasage"
            / "datasage-query-patterns"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn('target_data_state: "not_set_for_future"', skill)
        self.assertNotIn('target_data_state: "incomplete"', skill)


if __name__ == "__main__":
    unittest.main()
