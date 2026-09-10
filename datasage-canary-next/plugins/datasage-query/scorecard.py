"""Candidate business lenses for a broad operating-performance question.

The scorecard is intentionally outside the capability compiler.  It publishes
possible evidence lenses and metric identities; Hermes chooses the material
subset, ordering, interpretation, and conclusions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


SCORECARD_VERSION = "datasage-performance-scorecard/v3"


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
        "lens": "collections",
        "business_question": "How did period registered net collections change? This is not cash flow or liquidity.",
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
        "status": "candidate_only",
        "limitation": (
            "Published profit values are source-report records, not proof of accounting close. "
            "Customer-month and order-lifetime ledgers must remain distinct; missing expenses stay unknown."
        ),
        "candidates": (
            {"domain": "profit", "metric": "customer_month_gross_profit"},
            {"domain": "profit", "metric": "customer_month_gross_margin"},
        ),
    },
)


def performance_scorecard_manifest() -> dict[str, Any]:
    """Return non-executing candidate lenses with explicit ownership bounds."""

    return {
        "version": SCORECARD_VERSION,
        "kind": "operating_performance_candidate_lenses",
        "candidate_lenses": deepcopy(SCORECARD_LENSES),
        "evidence_boundaries": [
            "This is operating evidence, not complete company health or profitability.",
            (
                "Candidate lenses are not evidence until successfully queried. "
                "An unqueried, failed, or unavailable material lens remains "
                "unassessed and limits any umbrella conclusion."
            ),
            "Period flows and current or latest snapshots have different time scopes.",
        ],
    }
