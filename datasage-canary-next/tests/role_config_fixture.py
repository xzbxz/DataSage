"""Shared full-shape synthetic role configuration for offline tests only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REGIONS = (
    "HCM",
    "HN",
    "BKK",
    "IDK",
    "HCM-HT",
    "HN-HT",
    "IDK-HT",
    "BKK-HT",
)


def _sales_departments(region: str) -> list[str]:
    return [
        {
            "HCM": "HCM Sales",
            "HN": "HN Sales",
            "BKK": "BKK Sales",
            "IDK": "IDK Sales",
            "HCM-HT": "HT-HCM",
            "HN-HT": "HT-HN",
            "IDK-HT": "HT-IDK",
            "BKK-HT": "HT-BKK",
        }[region]
    ]


def public_document() -> dict[str, Any]:
    return {
        "version": 1,
        "regions": {
            region: {
                "departments": [region],
                "task_sales_departments": _sales_departments(region),
            }
            for region in REGIONS
        },
    }


def legacy_document() -> dict[str, Any]:
    public = public_document()
    return {
        "reference": "synthetic-legacy-reference",
        "approval": "review_only_not_send_authorization",
        "regions": {
            region: {
                "executors": [
                    {
                        "account": "synthetic-exec-" + region.lower().replace("-", "_"),
                        "name": "Synthetic Executor " + region,
                    }
                ],
                "managers": ["synthetic-manager-" + region.lower().replace("-", "_")],
                "dynamic_sales_departments": public["regions"][region]["task_sales_departments"],
            }
            for region in REGIONS
        },
        "price_manager_fixed": ["synthetic-fixed-manager"],
        "purchase_target": "synthetic explicit review target",
    }


def write_public(profile: Path) -> Path:
    path = profile / "plugins/datasage-query/contracts/legacy-workflows.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(public_document(), indent=2), encoding="utf-8")
    return path


def write_legacy(profile: Path) -> Path:
    path = profile / "legacy-recipient-reference.json"
    path.write_text(json.dumps(legacy_document(), indent=2), encoding="utf-8")
    return path
