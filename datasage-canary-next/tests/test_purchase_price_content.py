"""Synthetic purchase-content tests; no database, network, or transport."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
import importlib
import unittest

import test_business_contracts as base


content = importlib.import_module(base.TEST_PACKAGE + ".purchase_price_content")
compat = importlib.import_module(base.TEST_PACKAGE + ".legacy_price_compat")


OBSERVED = "2026-09-19T12:34:56+08:00"


def _row(
    goods_no: str,
    *,
    old_inc="10",
    new_inc="11",
    old_exc="8",
    new_exc="9",
    goods_name="Synthetic fabric",
    color="Blue",
    supplier="Synthetic supplier",
    unit="m",
    currency="CNY",
    adjust_date="2026-09-19",
):
    return {
        "goods_no": goods_no,
        "goods_name": goods_name,
        "color_label": color,
        "supplier_no": "SUP-SYN",
        "supplier_name": supplier,
        "old_inc": old_inc,
        "new_inc": new_inc,
        "old_exc": old_exc,
        "new_exc": new_exc,
        "unit_cuur": unit,
        "currency_no": currency,
        "adjust_date": adjust_date,
    }


def _parse_final_parts(parts):
    """Rebuild (group, record block) pairs from final Markdown only."""

    groups = (
        "🔻 采购价下调",
        "🔺 采购价上调",
        "缺失状态变化（需核对）",
    )
    parsed = []
    part_group_counts = []
    for part in parts:
        counts = {}
        found_any = False
        for index, title in enumerate(groups):
            marker = f"**{title}（"
            start = part.find(marker)
            if start < 0:
                continue
            found_any = True
            count_start = start + len(marker)
            count_end = part.find("条）**", count_start)
            count = int(part[count_start:count_end])
            body_start = part.find("\n\n", count_end) + 2
            end = len(part)
            for later_title in groups[index + 1 :]:
                later = part.find(f"**{later_title}（", body_start)
                if later >= 0:
                    end = min(end, later - len("\n\n**————————————**\n\n"))
            section = part[body_start:end].rstrip()
            blocks = section.split("\n\n────────────\n\n")
            if len(blocks) != count:
                raise AssertionError((title, count, blocks))
            parsed.extend((title, block) for block in blocks)
            counts[title] = count
        if not found_any:
            raise AssertionError(f"no group heading in {part!r}")
        part_group_counts.append(counts)
    return parsed, part_group_counts


def _parse_legacy_message(text):
    """Extract legacy group/block pairs without using the candidate module."""

    groups = (
        "🔻 采购价下调",
        "🔺 采购价上调",
        "缺失状态变化（需核对）",
    )
    parsed = []
    for index, title in enumerate(groups):
        marker = f"**{title}（"
        start = text.find(marker)
        if start < 0:
            continue
        count_end = text.find("条）**", start) + len("条）**")
        body_start = text.find("\n\n", count_end) + 2
        end = len(text)
        for later_title in groups[index + 1 :]:
            later = text.find(f"**{later_title}（", body_start)
            if later >= 0:
                end = min(end, later - len("\n\n**————————————**\n\n"))
        parsed.extend((title, block) for block in text[body_start:end].rstrip().split("\n\n────────────\n\n"))
    return parsed


class PurchasePriceContentTests(unittest.TestCase):
    def test_zero_change_batch_is_explicit_and_has_no_parts(self):
        row = _row("SYN-SAME", old_inc="10", new_inc="10", old_exc="8", new_exc="8")
        result = content.build_purchase_parts([row], observed_at=OBSERVED)
        self.assertEqual((), result.parts)
        self.assertEqual("empty_no_change", result.summary["status"])
        self.assertEqual(1, result.summary["input_row_count"])
        self.assertEqual(0, result.summary["change_record_count"])
        self.assertEqual([], result.summary["part_byte_sizes"])

    def test_old_renderer_blocks_are_preserved_and_direction_uses_tax_excluded_first(self):
        rows = [
            _row("SYN-DOWN", old_inc="12", new_inc="10", old_exc="10", new_exc="9"),
            _row("SYN-UP", old_inc="20", new_inc="21", old_exc="18", new_exc="19"),
            # Opposite directions: old logic gives the tax-excluded side
            # priority, so the complete record belongs to the down group.
            _row("SYN-OPPOSITE", old_inc="10", new_inc="12", old_exc="9", new_exc="8"),
            # Only one tax side changed; the unchanged side must not appear.
            _row("SYN-INC-ONLY", old_inc="5", new_inc="6", old_exc="4", new_exc="4"),
        ]
        result = content.build_purchase_parts(rows, observed_at=OBSERVED)
        self.assertEqual(1, len(result.parts))
        text = result.parts[0]
        self.assertLess(text.index("🔻 采购价下调"), text.index("🔺 采购价上调"))
        self.assertIn("批总数：4条", text)
        self.assertIn("观察时点：2026-09-19 12:34:56+08:00", text)
        self.assertIn("货号：SYN-OPPOSITE（Synthetic fabric）", text)
        self.assertIn("含税价：10.00 → 12.00（m；CNY）", text)
        self.assertIn("不含税价：9.00 → 8.00（m；CNY）", text)
        inc_only = text[text.index("货号：SYN-INC-ONLY") :]
        self.assertIn("含税价：5.00 → 6.00", inc_only)
        self.assertNotIn("不含税价：4.00 → 4.00", inc_only)
        self.assertNotIn("attachment", text.lower())

        old_pairs = _parse_legacy_message(compat.purchase_message(rows))
        new_pairs, _ = _parse_final_parts(result.parts)
        self.assertEqual(Counter(old_pairs), Counter(new_pairs))

    def test_long_utf8_parts_repeat_metadata_and_reconstruct_complete_records(self):
        rows = [
            _row(
                f"SYN-{index:03d}",
                goods_name="中文面料" * 12,
                color="สีแดง[]*",
                supplier="供应商\n特殊*[]",
                old_inc=Decimal("10.001"),
                new_inc=Decimal("10.002"),
                old_exc=Decimal("9.111"),
                new_exc=Decimal("9.222"),
            )
            for index in range(12)
        ]
        result = content.build_purchase_parts(rows, observed_at=OBSERVED, max_bytes=900, disclosure="合成范围：仅群文本")
        self.assertGreater(len(result.parts), 1)
        for index, part in enumerate(result.parts, 1):
            self.assertLessEqual(len(part.encode("utf-8")), 900)
            self.assertIn("观察时点：2026-09-19 12:34:56+08:00", part)
            self.assertIn("批总数：12条", part)
            self.assertIn(f"Part {index}/{len(result.parts)}", part)
            self.assertIn("披露：合成范围：仅群文本", part)
            self.assertIn("供应商\\n特殊\\*\\[\\]", part)

        old_pairs = _parse_legacy_message(compat.purchase_message(rows))
        new_pairs, part_counts = _parse_final_parts(result.parts)
        # Normalized ordinary fields must be exactly the legacy blocks; escaped
        # special fields are separately checked above.
        self.assertEqual(len(old_pairs), len(new_pairs))
        self.assertEqual(
            Counter(pair[0] for pair in old_pairs),
            Counter(pair[0] for pair in new_pairs),
        )
        self.assertEqual(len(rows), result.summary["rendered_record_count"])
        self.assertEqual(
            sorted(result.summary["rendered_record_multiset"]),
            result.summary["rendered_record_multiset"],
        )
        self.assertEqual(
            len(result.summary["rendered_record_multiset"]),
            result.summary["rendered_record_count"],
        )
        self.assertEqual(
            sum(sum(counts.values()) for counts in part_counts),
            result.summary["rendered_record_count"],
        )

    def test_final_reconstruction_keeps_all_down_groups_before_up_groups_across_parts(self):
        # Input is intentionally interleaved.  Long fields force one or two
        # records per part, so checking each final heading catches a part
        # boundary that would otherwise put an up record before a later down.
        rows = [
            _row("SYN-UP-1", old_inc="1", new_inc="2", old_exc="1", new_exc="2", goods_name="长" * 15),
            _row("SYN-DOWN-1", old_inc="2", new_inc="1", old_exc="2", new_exc="1", goods_name="长" * 15),
            _row("SYN-UP-2", old_inc="3", new_inc="4", old_exc="3", new_exc="4", goods_name="长" * 15),
            _row("SYN-DOWN-2", old_inc="4", new_inc="3", old_exc="4", new_exc="3", goods_name="长" * 15),
        ]
        result = content.build_purchase_parts(rows, observed_at=OBSERVED, max_bytes=650)
        self.assertGreater(len(result.parts), 1)
        headings = []
        for part in result.parts:
            for line in part.splitlines():
                if line.startswith("**🔻 采购价下调（"):
                    headings.append("down")
                elif line.startswith("**🔺 采购价上调（"):
                    headings.append("up")
        self.assertEqual(["down", "up"], headings)
        pairs, _ = _parse_final_parts(result.parts)
        self.assertEqual(4, len(pairs))
        self.assertEqual(["🔻 采购价下调", "🔻 采购价下调", "🔺 采购价上调", "🔺 采购价上调"], [pair[0] for pair in pairs])

    def test_group_detection_escapes_supplier_pseudo_heading_and_date_only_is_invalid(self):
        pseudo = _row(
            "SYN-PSEUDO-UP",
            supplier="供应商\n**🔻 采购价下调（1条）**",
            old_inc="1",
            new_inc="2",
            old_exc="1",
            new_exc="2",
        )
        result = content.build_purchase_parts([pseudo], observed_at=OBSERVED)
        self.assertIn("🔺 采购价上调（1条）", result.parts[0])
        self.assertNotIn("**🔻 采购价下调（1条）**", result.parts[0])
        for bad_clock in ("2026-09-19", date(2026, 9, 19)):
            with self.subTest(bad_clock=bad_clock), self.assertRaisesRegex(content.PurchaseContentError, "OBSERVED_CLOCK"):
                content.build_purchase_parts([pseudo], observed_at=bad_clock)

    def test_duplicate_records_remain_duplicates_in_final_reconstruction_and_summary(self):
        row = _row("SYN-DUP", old_inc="1", new_inc="2", old_exc="1", new_exc="1")
        result = content.build_purchase_parts([row, dict(row)], observed_at=OBSERVED)
        pairs, _ = _parse_final_parts(result.parts)
        self.assertEqual(2, len(pairs))
        self.assertEqual(2, result.summary["rendered_record_count"])
        self.assertEqual(2, len(result.summary["rendered_record_multiset"]))
        self.assertEqual(result.summary["rendered_record_multiset"], sorted(result.summary["rendered_record_multiset"]))

    def test_bad_and_mixed_unchanged_input_is_rejected_explicitly(self):
        changed = _row("SYN-CHANGED")
        unchanged = _row("SYN-UNCHANGED", old_inc="1", new_inc="1", old_exc="2", new_exc="2")
        with self.assertRaisesRegex(content.PurchaseContentError, "MIXED"):
            content.build_purchase_parts([changed, unchanged], observed_at=OBSERVED)
        with self.assertRaisesRegex(content.PurchaseContentError, "GOODS_NO"):
            content.build_purchase_parts([{"old_inc": 1, "new_inc": 2}], observed_at=OBSERVED)
        with self.assertRaisesRegex(content.PurchaseContentError, "INVALID"):
            content.build_purchase_parts([_row("SYN-BAD", old_inc="not-a-number")], observed_at=OBSERVED)
        with self.assertRaisesRegex(content.PurchaseContentError, "OBSERVED"):
            content.build_purchase_parts([changed], observed_at="not-a-time")

    def test_single_record_is_rejected_without_truncation_and_budget_ceiling_is_strict(self):
        too_long = _row("SYN-LONG", goods_name="长" * 3000)
        with self.assertRaisesRegex(content.PurchaseContentError, "SINGLE_CHANGE"):
            content.build_purchase_parts([too_long], observed_at=OBSERVED)
        with self.assertRaisesRegex(content.PurchaseContentError, "BYTE_BUDGET"):
            content.build_purchase_parts([], observed_at=OBSERVED, max_bytes=4097)


if __name__ == "__main__":
    unittest.main()
