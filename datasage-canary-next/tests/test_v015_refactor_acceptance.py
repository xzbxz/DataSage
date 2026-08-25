"""Frozen acceptance tests for the v0.15 architecture stabilization.

These tests intentionally assert architectural boundaries, not a model's exact
tool plan or final wording.  Tests that need Hermes message assembly live in the
host-compaction fixture and are not claimed as Profile-owned fixes here.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest

import jsonschema
import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_v015_refactor_acceptance"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)


def _module(name: str):
    return importlib.import_module(f"{PACKAGE}.{name}")


def _golden_scorer():
    path = PLUGIN_ROOT / "e2e" / "golden_expert_scorer.py"
    spec = importlib.util.spec_from_file_location(
        "_datasage_v015_golden_acceptance", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load golden scorer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CapabilityContractAcceptanceTests(unittest.TestCase):
    def test_capability_contract_rejects_business_reasoning_and_fixed_workflows(self):
        contract = _module("capability_contract")
        contract.assert_capability_boundary(
            {
                "metric_identity": "synthetic_metric",
                "business_definition": "A deterministic test fact.",
                "unit": "CNY",
                "time_semantics": "period_flow",
                "supported_dimensions": ["department"],
                "comparison_affordances": ["previous_period"],
                "adapter_identity": "synthetic_adapter",
                "receipt_inputs": ["metric_identity", "time_semantics"],
                "evidence_authorization": ["observation"],
            }
        )
        forbidden = (
            "triggers",
            "required_metrics",
            "call_order",
            "recipes",
            "answer_template",
            "threshold",
            "causal_explanation",
            "hypothesis",
            "recommendation",
        )
        for key in forbidden:
            with self.subTest(key=key):
                with self.assertRaises(contract.CapabilityContractError):
                    contract.assert_capability_boundary({key: "forbidden"})

    def test_public_schema_and_runtime_share_domain_field_contract(self):
        contract = _module("capability_contract")
        schemas = _module("schemas")
        validator = jsonschema.Draft7Validator(schemas.REQUEST)

        def request(domain: str, **extra):
            return {
                "request_id": f"{domain}_contract_probe",
                "domain": domain,
                "mode": "metric",
                "metric": "contract_probe_metric",
                "purpose": "offline schema/runtime equivalence probe",
                **extra,
            }

        valid = (
            request("delivery", delivery_scope="default_net"),
            request("inventory", inventory_scope="total"),
            request("target", attribution_mode="transaction_detail"),
        )
        invalid = (
            request("target"),
            request("receipt", attribution_mode="transaction_detail"),
            request("receipt", delivery_scope="default_net"),
            request("delivery", inventory_scope="total"),
        )
        for item in valid:
            with self.subTest(valid=item):
                self.assertEqual([], list(validator.iter_errors(item)))
                contract.validate_request_field_contract(item)
        for item in invalid:
            with self.subTest(invalid=item):
                self.assertTrue(list(validator.iter_errors(item)))
                with self.assertRaises(contract.CapabilityContractError):
                    contract.validate_request_field_contract(item)

        operation = request(
            "delivery",
            complete_change_decomposition={"dimension": "department"},
        )
        self.assertEqual(2, contract.physical_request_cost(operation))
        self.assertEqual(1, contract.physical_request_cost(request("delivery")))

    def test_metric_catalog_and_runtime_use_semantics_as_one_metric_source(self):
        contracts = _module("contracts")
        schemas = _module("schemas")
        tools = _module("tools")
        self.assertNotIn("enum", schemas.REQUEST["properties"]["metric"])

        for domain in schemas.DOMAINS:
            with self.subTest(domain=domain):
                _datasets, semantics = tools._contracts(domain)
                registered = {
                    code
                    for code, definition in semantics["metrics"].items()
                    if not contracts._is_unavailable(definition)
                }
                catalog = json.loads(
                    contracts.datasage_catalog(
                        {"requests": [{"domain": domain, "view": "expert_index"}]}
                    )
                )
                self.assertEqual("success", catalog["status"], catalog)
                published = {
                    item["code"] for item in catalog["results"][0]["metrics"]
                }
                self.assertEqual(registered, published)
                self.assertEqual(
                    semantics["version"],
                    catalog["results"][0]["source_versions"]["semantics"],
                )
                for metric in published:
                    tools._ensure_metric_available(semantics["metrics"][metric])


class IntelligenceBoundaryAcceptanceTests(unittest.TestCase):
    def test_golden_scorer_accepts_adaptive_supersets_and_rejects_forbidden_items(self):
        scorer = _golden_scorer()
        case = {
            "plan_constraints": {
                "domains": [],
                "metrics": [],
                "dimensions": ["department"],
                "operations": ["performance_scorecard_first"],
                "must_not_metrics": [],
                "must_not_operations": ["fixed_scorecard_bundle"],
                "time_semantics": "period_flows_with_separate_snapshot",
                "context_action": "new",
            },
            "required_conclusions": ["report_available_evidence"],
            "allowed_conclusions": ["report_available_evidence"],
            "forbidden_conclusions": ["infer_ungoverned_health"],
            "evidence_requirements": {
                "required_receipts": ["catalog", "query"],
                "minimum_successful_queries": 1,
                "allow_partial_failure": False,
                "require_untruncated": True,
                "require_reconciled_decomposition": False,
                "must_not_query": False,
                "required_error_codes": [],
            },
        }
        observed = {
            "plan": {
                "domains": ["delivery", "target", "receivable"],
                "metrics": [
                    "delivery_amount",
                    "delivery_target_completion",
                    "overdue_receivable_amount",
                ],
                "dimensions": ["department"],
                "operations": ["performance_scorecard_first", "parallel_evidence"],
                "time_semantics": "period_flows_with_separate_snapshot",
                "context_action": "new",
                "context_bindings": {},
            },
            "conclusions": ["report_available_evidence"],
            "evidence": {
                "receipts": ["catalog", "query"],
                "successful_queries": 3,
                "failed_queries": 0,
                "truncated": False,
                "reconciled": False,
                "query_attempted": True,
                "error_codes": [],
            },
        }
        self.assertEqual([], scorer._score_case(case, observed))
        forbidden = json.loads(json.dumps(observed))
        forbidden["plan"]["operations"].append("fixed_scorecard_bundle")
        self.assertTrue(
            any(
                "forbidden operations" in error
                for error in scorer._score_case(case, forbidden)
            )
        )

    def test_release_path_has_no_exact_plan_or_fixed_answer_scorer(self):
        scorer_source = (
            PLUGIN_ROOT / "e2e" / "golden_expert_scorer.py"
        ).read_text(encoding="utf-8")
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("expected exactly", scorer_source)
        self.assertNotIn("expected_final_answer_sha256", scorer_source)
        self.assertEqual(
            "required-and-forbidden-subsets/v1",
            suite["plan_constraint_semantics"],
        )
        self.assertTrue(
            all("expected_final_answer_sha256" not in case for case in suite["cases"])
        )

    def test_model_visible_references_do_not_publish_fixed_planner_recipes(self):
        references = _module("references")
        self.assertFalse(
            any(source_id.startswith("planner_") for source_id in references._SECTION_SPECS)
        )
        self.assertFalse(
            any(source_id.startswith("planner_") for source_id in references._SOURCE_PATHS)
        )


class ReleaseAndHostBoundaryAcceptanceTests(unittest.TestCase):
    def test_release_identity_is_computed_from_candidate_content(self):
        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        config = yaml.safe_load(
            (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        plugin = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        skill_text = (
            PROFILE_ROOT / "skills" / "datasage" / "datasage" / "SKILL.md"
        ).read_text(
            encoding="utf-8"
        )
        _, skill_frontmatter, _ = skill_text.split("---", 2)
        skill = yaml.safe_load(skill_frontmatter)
        self.assertEqual(str(distribution["version"]), str(plugin["version"]))
        self.assertEqual(str(distribution["version"]), str(skill["version"]))
        self.assertEqual("source" in distribution, "installed_at" in distribution)
        if "source" in distribution:
            self.assertTrue(str(distribution["source"]).strip())
            self.assertTrue(str(distribution["installed_at"]).strip())

        identity_inputs = {
            "profile_version": str(distribution["version"]),
            "hermes_requires": distribution["hermes_requires"],
            "model_provider": config["model"]["provider"],
            "model_default": config["model"]["default"],
            "reasoning_effort": config["agent"]["reasoning_effort"],
            "capability_contract_sha256": hashlib.sha256(
                (PLUGIN_ROOT / "capability_contract.py").read_bytes()
            ).hexdigest(),
            "schema_sha256": hashlib.sha256(
                (PLUGIN_ROOT / "schemas.py").read_bytes()
            ).hexdigest(),
            "golden_suite_sha256": hashlib.sha256(
                (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_bytes()
            ).hexdigest(),
        }
        release_identity = hashlib.sha256(
            json.dumps(
                identity_inputs,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertRegex(release_identity, r"^[0-9a-f]{64}$")

    def test_release_receipt_normalizes_hermes_install_metadata(self):
        builder_path = PROFILE_ROOT / "build_release_receipt.py"
        spec = importlib.util.spec_from_file_location(
            "datasage_release_receipt_builder", builder_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        installed = dict(distribution)
        installed.update(
            {
                "name": "isolated-install-name",
                "source": "C:/isolated/source",
                "installed_at": "2026-08-24T00:00:00+00:00",
            }
        )
        source = {
            key: value
            for key, value in distribution.items()
            if key not in builder.INSTALLER_MANIFEST_FIELDS
        }
        self.assertEqual(
            builder._canonical_manifest_bytes(source),
            builder._canonical_manifest_bytes(installed),
        )

    def test_compaction_fixture_is_an_explicit_host_contract(self):
        fixture = json.loads(
            (
                PROFILE_ROOT
                / "tests"
                / "fixtures"
                / "host_compaction_ordering.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("hermes_host", fixture["owner"])
        self.assertTrue(fixture["profile_must_not_patch"])
        self.assertEqual("expected_failure_before_host_fix", fixture["pre_fix_state"])
        self.assertEqual(
            fixture["latest_user_message"],
            fixture["expected_authoritative_latest_user_message"],
        )
        registration = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("pre_llm_call", registration)
        self.assertNotIn("compaction", registration.casefold())


if __name__ == "__main__":
    unittest.main()
