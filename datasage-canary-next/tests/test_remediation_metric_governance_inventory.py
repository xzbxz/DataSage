from __future__ import annotations

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "plugins" / "datasage-query" / "contracts"
GOVERNANCE = CONTRACTS / "metric-governance.yaml"


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: yaml.Loader, node: yaml.Node, deep: bool = False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _mapping,
)


def _load(path: Path):
    text = path.read_text(encoding="utf-8")
    if path == GOVERNANCE:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    return yaml.safe_load(text)


class MetricGovernanceInventoryTests(unittest.TestCase):
    def test_governance_inventory_exactly_matches_all_semantic_metrics(self):
        governance = _load(GOVERNANCE)
        self.assertEqual(
            {"schema", "version", "review_policy", "metrics"},
            set(governance),
        )
        self.assertEqual("datasage-metric-governance/v1", governance["schema"])
        self.assertEqual(1, governance["version"])
        expected_counts = {
            "customer_risk": 5,
            "delivery": 35,
            "inventory": 14,
            "receipt": 12,
            "receivable": 47,
            "target": 8,
        }
        expected_metrics = {}
        pending = {}
        for path in sorted(CONTRACTS.glob("*-semantics.yaml")):
            domain = path.name.removesuffix("-semantics.yaml")
            semantics = _load(path)
            expected_metrics[domain] = set(semantics["metrics"])
            for code, definition in semantics["metrics"].items():
                availability = definition.get("availability")
                if isinstance(availability, dict):
                    pending[(domain, code)] = availability
        self.assertEqual(expected_counts, {k: len(v) for k, v in expected_metrics.items()})
        self.assertEqual(121, sum(expected_counts.values()))
        self.assertEqual(set(expected_metrics), set(governance["metrics"]))
        for domain, codes in expected_metrics.items():
            self.assertEqual(codes, set(governance["metrics"][domain]))

        record_keys = {
            "owner_role",
            "lifecycle",
            "reviewed_at",
            "review_interval_days",
            "validation_gate",
        }
        owner_count = 0
        for domain, metrics in governance["metrics"].items():
            for code, record in metrics.items():
                self.assertEqual(record_keys, set(record), f"{domain}.{code}")
                self.assertEqual("active", record["lifecycle"])
                self.assertIsNone(record["reviewed_at"])
                self.assertIsNone(record["review_interval_days"])
                self.assertNotIn("derived_status", record)
                availability = pending.get((domain, code))
                if availability is None:
                    self.assertIsNone(record["owner_role"])
                    self.assertIsNone(record["validation_gate"])
                else:
                    owner_count += 1
                    self.assertEqual(availability["owner"], record["owner_role"])
                    self.assertEqual(
                        availability["activation_gate"],
                        record["validation_gate"],
                    )
        self.assertEqual(13, len(pending))
        self.assertEqual(13, owner_count)

    def test_review_policy_is_explicit_and_never_claims_completed_review(self):
        policy = _load(GOVERNANCE)["review_policy"]
        self.assertEqual(
            {
                "timezone": "Asia/Shanghai",
                "missing_review": "release_blocker",
                "overdue_review": "release_blocker",
                "missing_owner": "release_blocker",
                "missing_interval": "release_blocker",
                "runtime_review_failure": "allow",
                "deprecated_runtime": "allow_with_warning",
                "retired_runtime": "deny",
            },
            policy,
        )


if __name__ == "__main__":
    unittest.main()
