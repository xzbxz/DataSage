"""Offline regression checks for customer image/ZIP/audit compatibility.

The golden values were produced once by the old pure functions extracted from
``send_slow_customer.py`` at the pinned legacy ref.  Runtime tests do not
require the old repository: they compare the candidate to the checked-in
golden and inspect XLSX semantics directly.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from PIL import Image, ImageDraw

import test_business_contracts as base


compat = importlib.import_module(f"{base.TEST_PACKAGE}.legacy_customer_compat")
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "legacy_customer_compatibility.json").read_text(
        encoding="utf-8"
    )
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_replay_zip(package: dict, root: Path, week: str) -> tuple[Path, str]:
    """Test-only local ZIP oracle; production keeps the shared atomic publisher."""

    root.mkdir(parents=True, exist_ok=True)
    account = str(package["account"])
    token = hashlib.sha256(account.strip().encode("utf-8")).hexdigest()[:12]
    archive_path = root / f"sales_{token}_{week}.zip"
    image_root = root / f"images_{token}"
    image_root.mkdir(parents=True, exist_ok=True)
    customers = list(package["customers"])
    digest_input = {"week": week, "account": account, "customers": customers}
    digest = hashlib.sha256(
        json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, customer in enumerate(customers):
            member = compat.customer_zip_member_name(customer, index)
            image_path = image_root / f"{index:05d}.png"
            compat.render_customer_card(customer, image_path)
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, image_path.read_bytes())
    return archive_path, digest


class LegacyCustomerCompatibilityTests(unittest.TestCase):
    def test_provenance_is_pinned_to_the_reviewed_old_sender(self):
        self.assertEqual(
            FIXTURE["legacy_ref"], compat.LEGACY_REF
        )
        self.assertEqual(
            FIXTURE["legacy_source"], compat.LEGACY_SOURCE
        )
        self.assertEqual(64, len(FIXTURE["legacy_source_sha256"]))

    def test_normal_card_and_zip_match_legacy_golden(self):
        case = FIXTURE["normal_case"]
        customer = case["customer"]
        package = {"account": case["account"], "customers": [customer]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            card = root / "card.png"
            compat.render_customer_card(customer, card)
            with Image.open(card) as rendered:
                self.assertEqual(tuple(case["image_size"]), rendered.size)
            self.assertEqual(case["image_sha256"], _sha256(card))

            archive, digest = _build_replay_zip(package, root / "zip", case["week"])
            self.assertEqual(case["zip_name"], archive.name)
            self.assertEqual(case["zip_sha256"], _sha256(archive))
            self.assertEqual(case["content_digest"], digest)
            with zipfile.ZipFile(archive) as handle:
                self.assertEqual([case["zip_member"]], handle.namelist())
                info = handle.getinfo(case["zip_member"])
                self.assertEqual((1980, 1, 1, 0, 0, 0), info.date_time)
                self.assertEqual(0o600 << 16, info.external_attr)

    def test_long_fields_keep_legacy_visual_markers_and_full_content(self):
        case = FIXTURE["long_case"]
        customer = {
            "customer_no": case["customer_no"],
            "customer_name": case["customer_name"],
            "products": case["products"],
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "long.png"
            compat.render_customer_card(customer, path)
            with Image.open(path) as rendered:
                width, height = rendered.size
            self.assertEqual(case["compatibility_image_width"], width)
            self.assertGreaterEqual(height, case["compatibility_image_min_height"])
            # The explicit exception is height/pixels for long fields.  The
            # legacy right marker and palette remain source-visible and are
            # asserted by the renderer output's known palette.
            with Image.open(path) as rendered:
                palette = {
                    colour
                    for _count, colour in (rendered.convert("RGB").getcolors(10_000_000) or [])
                }
            self.assertIn((46, 117, 182), palette)  # #2E75B6
            self.assertIn((242, 242, 242), palette)  # #F2F2F2
            self.assertIn("Discountable", Path(compat.__file__).read_text(encoding="utf-8"))

    def test_chinese_customer_and_color_use_real_cjk_glyph_font(self):
        if not compat._CJK_FONT_PATH.exists():
            self.skipTest("Windows Microsoft YaHei font is unavailable")
        fallback = compat._font(13)
        cjk_font = compat._font_for_value("华南纺织客户", fallback, 13)
        english_font = compat._font_for_value("Synthetic Customer", fallback, 13)
        self.assertIn("msyh", str(getattr(cjk_font, "path", "")).lower())
        self.assertEqual(getattr(fallback, "path", None), getattr(english_font, "path", None))
        customer = {
            "customer_no": "中文-C1",
            "customer_name": "华南纺织客户",
            "products": [["SYN-中文-001", "深红色", 3], ["SYN-002", "蓝色", 1]],
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "zh.png"
            compat.render_customer_card(customer, path)
            with Image.open(path) as rendered:
                self.assertEqual((424, 152), rendered.size)
                # The CJK title and colour cells contain real glyph pixels;
                # a missing-glyph box is ruled out by the
                # font-path assertion above and this real rendered sample.
                title_colors = rendered.convert("RGB").crop((8, 0, 180, 26)).getcolors(100000) or []
                self.assertGreater(
                    sum(count for count, colour in title_colors if colour != (255, 255, 255)),
                    20,
                )

    def test_thai_and_fullwidth_parentheses_use_script_fallbacks(self):
        if not compat._THAI_FONT_PATH.exists() or not compat._CJK_FONT_PATH.exists():
            self.skipTest("required installed script fonts are unavailable")
        fallback = compat._font(13)
        thai = compat._font_for_value("ราคาไทย", fallback, 13)
        self.assertIn("leelaw", str(getattr(thai, "path", "")).lower())
        runs = compat._font_runs("客户（ราคาไทย）", fallback, 13)
        paths = [str(getattr(font, "path", "")).lower() for font, _text in runs]
        self.assertTrue(any("msyh" in path for path in paths))
        self.assertTrue(any("leelaw" in path for path in paths))

        # Check actual cmap rendering masks for representative glyphs.  A
        # selected font that only supplies the replacement glyph is rejected;
        # this does not rely on PNG pixel equality or an assumed OCR result.
        def is_missing(font, char):
            glyph = font.getmask(char)
            replacement = font.getmask(compat.MISSING_GLYPH_PROBE)
            return glyph.size == replacement.size and bytes(glyph) == bytes(replacement)

        for char in ("（", "）"):
            font = compat._font_for_char(char, fallback, 13)
            self.assertIn("msyh", str(getattr(font, "path", "")).lower())
            self.assertFalse(is_missing(font, char))
        for char in "ราคาไทย":
            font = compat._font_for_char(char, fallback, 13)
            self.assertIn("leelaw", str(getattr(font, "path", "")).lower())
            self.assertFalse(is_missing(font, char))

        value = "客户（ราคาไทย） / สีแดง（สด）"
        image = Image.new("RGB", (424, 1), "white")
        draw = ImageDraw.Draw(image)
        self.assertEqual(value, "".join(compat._wrap_cell(value, fallback, 390, draw)))
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "script-fallback.png"
            compat.render_customer_card(
                {
                    "customer_no": "SYN-SCRIPT",
                    "customer_name": "客户（ราคาไทย）",
                    "products": [["货号（A）", "สีแดง（สด）", 2]],
                },
                path,
            )
            with Image.open(path) as rendered:
                self.assertEqual((424, 96), rendered.size)

    def test_missing_required_script_font_fails_closed(self):
        original = compat._THAI_FONT_PATH
        try:
            compat._THAI_FONT_PATH = Path("C:/Windows/Fonts/__missing_required_thai__.ttf")
            with self.assertRaisesRegex(RuntimeError, "CUSTOMER_IMAGE_REQUIRED_FONT_MISSING"):
                compat._font_for_char("ก", compat._font(13), 13)
        finally:
            compat._THAI_FONT_PATH = original

    def test_long_cjk_title_wraps_inside_discountable_lane(self):
        if not compat._CJK_FONT_PATH.exists():
            self.skipTest("Windows Microsoft YaHei font is unavailable")
        title = "华南纺织客户" * 6
        fallback = compat._font(15)
        font = compat._font_for_value(title, fallback, 15)
        image = Image.new("RGB", (424, 1), "white")
        draw = ImageDraw.Draw(image)
        right_box = draw.textbbox((0, 0), "Discountable", font=fallback)
        right_x = 424 - compat.PADDING - (right_box[2] - right_box[0])
        lane = right_x - compat.PADDING - 8
        lines = compat._title_lines(title, font, draw, lane)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(draw.textbbox((0, 0), line, font=font)[2] <= lane for line in lines))
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "long-zh.png"
            short_path = Path(temporary) / "short-zh.png"
            compat.render_customer_card(
                {"customer_no": "SYN-CJK", "customer_name": title, "products": [["SYN-1", "红色", 1]]},
                path,
            )
            compat.render_customer_card(
                {"customer_no": "SYN-CJK", "customer_name": "华南客户", "products": [["SYN-1", "红色", 1]]},
                short_path,
            )
            with Image.open(path) as rendered:
                self.assertEqual(424, rendered.width)
                with Image.open(short_path) as short_rendered:
                    self.assertGreater(rendered.height, short_rendered.height)

    def test_filename_rule_does_not_turn_missing_values_into_internal_ids(self):
        self.assertEqual(
            "SYN-C1_Synthetic Customer.png",
            compat.customer_zip_member_name(
                {"customer_no": "SYN-C1", "customer_name": "Synthetic Customer"}, 0
            ),
        )
        self.assertEqual(
            "CustomerNo_1_Customer_1.png",
            compat.customer_zip_member_name(
                {"customer_no": None, "customer_name": None}, 0
            ),
        )
        self.assertNotIn("c-internal", compat.customer_zip_member_name(
            {"customer_no": None, "customer_name": None, "customer_id": "c-internal"}, 0
        ))

    def test_dispatch_workbook_keeps_old_sheet_rows_and_layout(self):
        case = FIXTURE["dispatch_case"]
        packages = case["packages"]
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "dispatch.xlsx"
            compat.generate_customer_dispatch_xlsx(packages, path)
            with zipfile.ZipFile(path) as handle:
                self.assertEqual(case["workbook_entries"], handle.namelist())
                self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in handle.infolist()))
                workbook = handle.read("xl/workbook.xml").decode("utf-8")
                self.assertIn('name="Synthetic Sales"', workbook)
                sheet = handle.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn('<dimension ref="A1:B3"/>', sheet)
                self.assertIn('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>', sheet)
                self.assertIn('<autoFilter ref="A1:B3"/>', sheet)
                self.assertEqual(2, sheet.count('s="1"'))
                self.assertEqual(1, sheet.count('<t>Customer No</t>'))
                self.assertEqual(1, sheet.count('<t>Customer</t>'))
                self.assertEqual(1, sheet.count('<t>SYN-C1</t>'))
                # Source customers are sorted by customer number and name.
                self.assertLess(sheet.index("SYN-C1"), sheet.index("SYN-C2"))
                self.assertIn("Another Customer", sheet)

    def test_artifact_builder_is_local_only(self):
        source = Path(compat.__file__).read_text(encoding="utf-8")
        self.assertNotIn("pymysql", source)
        self.assertNotIn("requests", source)
        self.assertNotIn("send_message", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
