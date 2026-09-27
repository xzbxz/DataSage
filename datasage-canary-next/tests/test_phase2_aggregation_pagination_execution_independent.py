"""Guarded public-API Phase-2 pagination probe.

This is the executable companion to the reviewed Phase-2 protocol. It runs in
the guarded source-copy runner. It imports the promoted
Phase-1 fixture modules as module aliases, never their old test classes into
this module namespace, and calls the candidate's public ``datasage_query``
handler with ``detail={id,limit,cursor}``.  It does not add document-detail
goldens or run while authored here.
"""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import json
import unittest

import test_phase1_ar_v2_execution_independent as _ar_v2
import test_phase1_inventory_v3_execution_independent as _inventory_v3
import test_phase1_target_execution_independent as _target_v1


def _result(payload: dict, request_id: str | None = None) -> dict:
    if payload.get("status") not in {"success", "partial"}:
        raise AssertionError(f"public detail call failed: {payload!r}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise AssertionError(f"detail results missing: {payload!r}")
    if request_id is not None:
        for item in results:
            if isinstance(item, dict) and item.get("request_id") == request_id:
                return item
    if len(results) == 1 and isinstance(results[0], dict):
        return results[0]
    raise AssertionError(f"detail request result not found: {payload!r}")


def _detail(result: dict) -> dict:
    value = result.get("detail")
    if isinstance(value, dict):
        return value
    details = result.get("details")
    if isinstance(details, dict):
        return details
    raise AssertionError(f"detail envelope missing: {result!r}")


def _page_rows(result: dict) -> list[dict]:
    rows = result.get("rows")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    claims = result.get("claim_ledger")
    if isinstance(claims, list):
        return [row for row in claims if isinstance(row, dict)]
    raise AssertionError(f"public page rows/claims missing: {result!r}")


def _error_code(payload: dict) -> str | None:
    def walk(value: object) -> str | None:
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return error["code"]
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None
    return walk(payload)


def _recursive_values(value: object, key: str) -> list[object]:
    found: list[object] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(_recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_recursive_values(child, key))
    return found


def _numeric(result: dict, *keys: str) -> int | None:
    for key in keys:
        for value in _recursive_values(result, key):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _analysis_count(result: dict, name: str) -> int | None:
    """Read Phase1 analysis population counts, not detail membership counts."""

    for context in _recursive_values(result, "analysis_context"):
        if isinstance(context, dict):
            counts = context.get("counts")
            if isinstance(counts, dict):
                value = counts.get(name)
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
    claims = result.get("claim_ledger")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            facts = claim.get("facts")
            if not isinstance(facts, dict):
                continue
            for key in (name, f"analysis_{name}_count"):
                if key in facts:
                    try:
                        return int(facts[key])
                    except (TypeError, ValueError):
                        pass
    return None


def _row_facts(row: dict) -> dict:
    facts = row.get("facts")
    return facts if isinstance(facts, dict) else row


def _first(row: dict, *keys: str) -> object:
    facts = _row_facts(row)
    for key in keys:
        if key in facts:
            return facts[key]
        if key in row:
            return row[key]
    return None


def _with_detail(request: dict, detail_id: str, page_size: int, cursor: str | None = None) -> dict:
    result = copy.deepcopy(request)
    # detail owns pagination; top-level summary limit must not be carried over.
    result.pop("limit", None)
    detail = {"id": detail_id, "limit": page_size}
    if cursor is not None:
        detail["cursor"] = cursor
    result["detail"] = detail
    return result


def _assert_request_preserved(test: unittest.TestCase, base: dict, detailed: dict) -> None:
    test.assertNotIn("limit", detailed)
    for key in ("analysis", "dimensions", "currency_basis", "metric_filters", "time_range", "calendar_month", "attribution_mode"):
        if key in base:
            test.assertEqual(base[key], detailed.get(key), key)


def _new_fixture(test: unittest.TestCase, module: object, class_name: str):
    fixture = getattr(module, class_name)()
    fixture.setUp()
    test.addCleanup(fixture.doCleanups)
    return fixture


def _set_environment_cap(test: unittest.TestCase, harness: object, cap: int) -> None:
    from unittest.mock import patch

    settings = _target_v1._remaining.plugin.tools.settings
    original = settings.get

    def bounded(name: str, default=None):
        if name == "max_rows":
            return cap
        return original(name, default)

    patcher = patch.object(settings, "get", side_effect=bounded)
    patcher.start()
    test.addCleanup(patcher.stop)


def _clean_target_fixture(test: unittest.TestCase):
    fixture = _new_fixture(test, _target_v1, "TargetPhase1StandaloneTests")
    harness = fixture.harness
    harness.conn.execute("DELETE FROM vk_dwd.delivery_target_detail_dwd")
    harness.conn.execute("DELETE FROM vk_dwd.sale_bill_goods_detail_dwd")
    target_rows = [
        (1, "2026-09", "Alpha", "Alpha", "Alpha", "Alpha", "ORG", "CNY", 100.0, 100.0, "n"),
        (2, "2026-09", "Beta", "Beta", "Beta", "Beta", "ORG", "CNY", 100.0, 100.0, "n"),
    ]
    actual_rows = [
        (50.0, 6, "n", "2026-09-01", "Alpha"),
        (50.0, 6, "n", "2026-09-01", "Beta"),
    ]
    harness.insert(
        "vk_dwd.delivery_target_detail_dwd",
        "detail_id,year_month,customer_id,customer_no,customer_name,customer_dept,org_name,currency_no,detail_target_rmb,detail_target_amount,is_inner_cus",
        target_rows,
    )
    harness.insert(
        "vk_dwd.sale_bill_goods_detail_dwd",
        "delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_dept",
        actual_rows,
    )
    return fixture


def _actor_invoke(harness: object, args: dict, user_id: str) -> dict:
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(platform="wecom", source="wecom", user_id=user_id, chat_id="OFFLINE-ONLY", chat_type="dm")
    try:
        return json.loads(harness.ctx.handlers["datasage_query"](args))
    finally:
        clear_session_vars(tokens)


class Phase2PublicPaginationExecutionTests(unittest.TestCase):
    def test_target_tie_pages_full_vs_page_subtotals_and_last_page_state(self) -> None:
        fixture = _clean_target_fixture(self)
        harness = fixture.harness
        base = fixture.request(limit=2)
        self.assertEqual(["department"], base["dimensions"])
        first_request = _with_detail(base, "target_department_groups", 1)
        _assert_request_preserved(self, base, first_request)
        first = _result(harness.invoke("datasage_query", {"requests": [first_request]}), "target-standalone")
        detail1 = _detail(first)
        self.assertTrue(detail1["has_more"], detail1)
        self.assertIsNotNone(detail1.get("next_cursor"), detail1)
        rows1 = _page_rows(first)
        self.assertEqual(1, len(rows1))
        row1 = rows1[0]
        self.assertEqual(1, _numeric(row1, "query_rank", "rank"))
        self.assertEqual(2, _numeric(row1, "rank_tie_count", "tie_count") or _numeric(detail1, "rank_tie_count", "tie_count"))
        cursor = detail1["next_cursor"]

        second_request = _with_detail(base, "target_department_groups", 1, cursor)
        second = _result(harness.invoke("datasage_query", {"requests": [second_request]}), "target-standalone")
        detail2 = _detail(second)
        self.assertFalse(detail2["has_more"], detail2)
        self.assertTrue(detail2.get("truncated", second.get("truncated", False)), detail2)
        rows2 = _page_rows(second)
        self.assertEqual(1, len(rows2))
        row2 = rows2[0]
        self.assertEqual(1, _numeric(row2, "query_rank", "rank"))
        self.assertEqual(2, _numeric(row2, "rank_tie_count", "tie_count") or _numeric(detail2, "rank_tie_count", "tie_count"))
        reconciliation = detail2.get("reconciliation", {})
        full = reconciliation.get("full", {}).get("full_subtotals", {})
        page = reconciliation.get("page", {}).get("matched_subtotals", {})
        self.assertEqual(Decimal("200"), Decimal(str(full.get("target_amount_rmb"))))
        self.assertEqual(Decimal("100"), Decimal(str(full.get("actual_amount_rmb"))))
        self.assertEqual(Decimal("100"), Decimal(str(full.get("gap_amount_rmb"))))
        self.assertEqual(Decimal("100"), Decimal(str(page.get("target_amount_rmb"))))
        self.assertEqual(Decimal("50"), Decimal(str(page.get("actual_amount_rmb"))))
        self.assertEqual(Decimal("50"), Decimal(str(page.get("gap_amount_rmb"))))
        source_reconciliation = _recursive_values(second, "source_reconciliation")
        self.assertTrue(source_reconciliation, second)
        self.assertTrue(any(isinstance(item, dict) and item.get("status") == "reconciled" for item in source_reconciliation), source_reconciliation)

    def test_changed_analysis_threshold_rejects_old_cursor(self) -> None:
        fixture = _clean_target_fixture(self)
        harness = fixture.harness
        base = fixture.request(limit=2)
        first = _result(harness.invoke("datasage_query", {"requests": [_with_detail(base, "target_department_groups", 1)]}), "target-standalone")
        cursor = _detail(first)["next_cursor"]
        changed = copy.deepcopy(base)
        changed["analysis"]["group_filters"][0]["value"] = "0.7"
        payload = harness.invoke("datasage_query", {"requests": [_with_detail(changed, "target_department_groups", 1, cursor)]})
        self.assertEqual("failed", payload.get("status"), payload)
        self.assertIn(_error_code(payload), {"CURSOR_SCOPE_CHANGED", "CURSOR_STALE"})

    def test_inventory_cursor_revalidates_current_observation_after_clock_change(self) -> None:
        fixture = _new_fixture(self, _inventory_v3, "InventoryV3ExecutionTests")
        fixture.load_extension_fixture()
        harness = fixture.harness
        base = fixture.request(analysis=True, limit=1)
        self.assertEqual("HCM", base["metric_filters"]["warehouse_department"])
        first = _result(harness.query(_with_detail(base, "inventory_product_groups", 1)), "v3")
        detail1 = _detail(first)
        self.assertIsNotNone(detail1.get("next_cursor"), detail1)
        harness.conn.create_function("NOW", -1, lambda *args: "2026-09-11T13:00:00")
        harness.conn.create_function("UTC_TIMESTAMP", -1, lambda *args: "2026-09-11T05:00:00")
        second = _result(harness.query(_with_detail(base, "inventory_product_groups", 1, detail1["next_cursor"])), "v3")
        detail2 = _detail(second)
        self.assertEqual("revalidated_current_observation", detail2.get("cursor_consistency"))
        self.assertEqual("not_guaranteed_old_snapshot", detail2.get("snapshot_continuity"))
        unknown = _analysis_count(second, "unknown")
        self.assertIsNotNone(unknown, detail2)
        self.assertGreaterEqual(unknown, 1)
        gross_values: list[int] = []
        for key in ("gross_rolls", "known_gross_rolls", "analysis_gross_rolls", "analysis_known_gross_rolls"):
            for value in _recursive_values(detail2, key) + _recursive_values(second, key):
                try:
                    gross_values.append(Decimal(str(value)))
                except (TypeError, ValueError, InvalidOperation):
                    pass
        self.assertTrue(any(value >= 8 for value in gross_values), detail2)

    def test_ar_latest_month_change_rejects_cursor_and_unknown_is_not_matched(self) -> None:
        fixture = _new_fixture(self, _ar_v2, "ARV2ExecutionTests")
        harness = fixture.harness
        base = fixture.request(limit=1)
        first = _result(harness.invoke("datasage_query", {"requests": [_with_detail(base, "receivable_customer_groups", 1)]}), "arv2")
        detail1 = _detail(first)
        matched = _analysis_count(first, "match")
        self.assertIsNotNone(matched, detail1)
        self.assertEqual(0, matched)
        self.assertIsNotNone(detail1.get("next_cursor"), detail1)
        harness.conn.execute("UPDATE vk_dw.customer_debt_bymonth_dw SET bill_date='2026-09' WHERE bill_date='2026-08'")
        payload = harness.invoke("datasage_query", {"requests": [_with_detail(base, "receivable_customer_groups", 1, detail1["next_cursor"])]})
        self.assertEqual("failed", payload.get("status"), payload)
        self.assertIn(_error_code(payload), {"CURSOR_STALE", "CURSOR_SCOPE_CHANGED"})

    def test_authorized_actor_change_rejects_old_cursor(self) -> None:
        fixture = _clean_target_fixture(self)
        harness = fixture.harness
        base = fixture.request(limit=2)
        first = _result(harness.invoke("datasage_query", {"requests": [_with_detail(base, "target_department_groups", 1)]}), "target-standalone")
        cursor = _detail(first)["next_cursor"]
        payload = _actor_invoke(harness, {"requests": [_with_detail(base, "target_department_groups", 1, cursor)]}, "OFFLINE-OTHER-AUTHORIZED")
        self.assertEqual("failed", payload.get("status"), payload)
        self.assertIn(_error_code(payload), {"CURSOR_STALE", "CURSOR_SCOPE_CHANGED", "CURSOR_AUTH_CHANGED", "CURSOR_CONTEXT_CHANGED"})

    def test_detail_cap_rejects_and_ordinary_request_without_detail_survives(self) -> None:
        fixture = _clean_target_fixture(self)
        harness = fixture.harness
        base = fixture.request(limit=2)
        over_cap = harness.invoke("datasage_query", {"requests": [_with_detail(base, "target_department_groups", 51)]})
        self.assertEqual("failed", over_cap.get("status"), over_cap)
        self.assertIn(_error_code(over_cap), {"DETAIL_PAGE_SIZE_INVALID", "DETAIL_INVALID", "INVALID_INPUT"})
        _set_environment_cap(self, harness, 1)
        collection_cap = harness.invoke("datasage_query", {"requests": [_with_detail(base, "target_department_groups", 1)]})
        self.assertEqual("failed", collection_cap.get("status"), collection_cap)
        self.assertEqual("DETAIL_SCOPE_TOO_LARGE", _error_code(collection_cap))
        ordinary = harness.invoke("datasage_query", {"requests": [base]})
        ordinary_result = _result(ordinary, "target-standalone")
        self.assertNotIn("detail", ordinary_result)

    def test_all_excluded_target_has_empty_rows_and_none_full_matched_totals(self) -> None:
        fixture = _clean_target_fixture(self)
        harness = fixture.harness
        harness.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=90")
        base = fixture.request(limit=2)
        payload = harness.invoke("datasage_query", {"requests": [_with_detail(base, "target_department_groups", 1)]})
        result = _result(payload, "target-standalone")
        detail = _detail(result)
        self.assertEqual([], _page_rows(result))
        self.assertFalse(detail.get("has_more"), detail)
        full = detail.get("reconciliation", {}).get("full", {}).get("full_subtotals", {})
        for field in ("target_amount_rmb", "actual_amount_rmb", "gap_amount_rmb"):
            self.assertIsNone(full.get(field), (field, full))
        matched = _numeric(result, "analysis_match_count", "matched_total", "match_count")
        if matched is not None:
            self.assertEqual(0, matched)

    def test_same_scope_source_mutation_rejects_cursor(self) -> None:
        fixture = _clean_target_fixture(self)
        harness = fixture.harness
        base = fixture.request(limit=2)
        first = _result(harness.invoke("datasage_query", {"requests": [_with_detail(base, "target_department_groups", 1)]}), "target-standalone")
        cursor = _detail(first)["next_cursor"]
        harness.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=51 WHERE customer_dept='Alpha'")
        payload = harness.invoke("datasage_query", {"requests": [_with_detail(base, "target_department_groups", 1, cursor)]})
        self.assertEqual("failed", payload.get("status"), payload)
        self.assertIn(_error_code(payload), {"CURSOR_STALE", "CURSOR_SCOPE_CHANGED"})


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Phase2PublicPaginationExecutionTests)
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
