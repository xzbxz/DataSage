"""Candidate business lenses for a broad operating-performance question.

The scorecard is intentionally outside the capability compiler.  It publishes
possible evidence lenses and metric identities; Hermes chooses the material
subset, ordering, interpretation, and conclusions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


SCORECARD_VERSION = "datasage-performance-scorecard/v2"


SCORECARD_LENSES: tuple[dict[str, Any], ...] = (
    {
        "lens": "growth",
        "business_question": "How did period operating output change?",
        "time_semantics": "period_flow",
        "candidates": (
            {"domain": "delivery", "metric": "delivery_amount"},
        ),
    },
    {
        "lens": "cash",
        "business_question": "How did period cash collection change?",
        "time_semantics": "period_flow",
        "candidates": (
            {"domain": "receipt", "metric": "net_receipt_amount"},
        ),
    },
    {
        "lens": "target",
        "business_question": "How did governed actuals compare with targets?",
        "time_semantics": "period_flow_with_governed_target",
        "candidates": (
            {"domain": "target", "metric": "delivery_target_completion"},
            {"domain": "target", "metric": "receipt_target_completion"},
        ),
    },
    {
        "lens": "inventory_turnover",
        "business_question": "What does inventory efficiency and exposure show?",
        "time_semantics": "period_metric_and_separate_snapshot",
        "candidates": (
            {"domain": "inventory", "metric": "inventory_turnover_days"},
            {"domain": "inventory", "metric": "current_inventory_amount_rmb"},
        ),
    },
    {
        "lens": "risk",
        "business_question": "What does receivable trend and overdue exposure show?",
        "time_semantics": "period_metric_and_separate_snapshot",
        "candidates": (
            {"domain": "receivable", "metric": "debt_balance_trend"},
            {"domain": "receivable", "metric": "overdue_receivable_amount"},
        ),
    },
    {
        "lens": "profitability",
        "status": "not_available",
        "limitation": (
            "No governed profit, gross-margin, or expense metric is published "
            "in this toolset."
        ),
        "candidates": (),
    },
)


# Internal entitlement compatibility inventory only.  This is not a bundle,
# ordering contract, or requirement that Hermes query every listed metric.
SCORECARD_METRICS: tuple[dict[str, str], ...] = tuple(
    {"domain": candidate["domain"], "metric": candidate["metric"]}
    for lens in SCORECARD_LENSES
    for candidate in lens["candidates"]
)


def performance_scorecard_manifest() -> dict[str, Any]:
    """Return non-executing candidate lenses with explicit ownership bounds."""

    return {
        "version": SCORECARD_VERSION,
        "kind": "operating_performance_candidate_lenses",
        "selection_owner": "Hermes",
        "ordering_owner": "Hermes",
        "interpretation_owner": "Hermes",
        "candidate_lenses": deepcopy(SCORECARD_LENSES),
        "evidence_boundaries": [
            "This is operating evidence, not complete company health or profitability.",
            "Period flows and current or latest snapshots have different time scopes.",
            "Normative judgments require a returned governed benchmark.",
            "Cross-metric comparisons require compatible populations, periods, units, and scope.",
        ],
    }
