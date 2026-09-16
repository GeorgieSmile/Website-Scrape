"""Offline tests for the PMUA Apptech scraper.

All tests run against saved fixtures and synthetic data; no network access.
Run from the repository root:

    ../.venv/bin/python -m unittest discover -s "PMUA Apptech" -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrape  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class CleaningTests(unittest.TestCase):
    def test_clean_text_normalizes_unicode_and_space(self) -> None:
        self.assertEqual(scrape.clean_text("\u00a0\u00a0สวัสดี   ครับ"), "สวัสดี ครับ")

    def test_optional_text_placeholders(self) -> None:
        self.assertIsNone(scrape.optional_text("-"))
        self.assertIsNone(scrape.optional_text("ไม่มี"))
        self.assertIsNone(scrape.optional_text(""))
        self.assertEqual(scrape.optional_text("ข้อมูล"), "ข้อมูล")

    def test_to_int(self) -> None:
        self.assertEqual(scrape.to_int("1,172"), 1172)
        self.assertIsNone(scrape.to_int("-"))
        self.assertIsNone(scrape.to_int(None))

    def test_parse_thb_amount(self) -> None:
        self.assertEqual(scrape.parse_thb_amount("60,000.00 ฿"), 60000)
        self.assertEqual(scrape.parse_thb_amount("45,000.00 ฿"), 45000)
        self.assertIsNone(scrape.parse_thb_amount("สอบถาม"))
        self.assertIsNone(scrape.parse_thb_amount(None))

    def test_semantic_hash_ignores_volatile_fields(self) -> None:
        base = {"record_id": "1", "title": "x", "scraped_at": "a"}
        other = {"record_id": "1", "title": "x", "scraped_at": "b"}
        self.assertEqual(scrape.semantic_hash(base), scrape.semantic_hash(other))


class ProductParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.record = scrape.parse_product_detail(
            load_fixture("product_4648.html"), {"product_id": "4648"}
        )

    def test_title(self) -> None:
        self.assertEqual(
            self.record["title"],
            "เครื่องลอกเปลือกกัญชง ขับเคลื่อนด้วยพลังงานแสงอาทิตย์",
        )
        self.assertEqual(self.record["record_id"], "4648")
        self.assertIn("/product/show/4648", self.record["detail_url"])

    def test_trl(self) -> None:
        self.assertIsNotNone(self.record["trl"])
        self.assertEqual(self.record["trl"]["level"], 6)
        self.assertEqual(self.record["trl"]["label_raw"], "ต้นแบบ")
        self.assertIsNotNone(self.record["trl"]["explanation_raw"])

    def test_price_and_sidebar(self) -> None:
        self.assertEqual(self.record["price_raw"], "60,000.00 ฿")
        self.assertEqual(self.record["price_thb"], 60000)
        self.assertIn("20 ต้น/นาที", self.record["production_capacity_raw"] or "")
        self.assertEqual(self.record["technology_type"], "เทคโนโลยี / นวัตกรรมพร้อมใช้")
        self.assertEqual(self.record["category"], "เครื่องมือ / เครื่องจักร")

    def test_researcher_and_university_links(self) -> None:
        self.assertEqual(self.record["researcher"]["agent_id"], "1829")
        self.assertEqual(self.record["researcher"]["name_raw"], "นพพร พัชรประกิติ")
        self.assertEqual(self.record["university"]["university_id"], "48")

    def test_funding_history(self) -> None:
        self.assertEqual(len(self.record["funding_history"]), 1)
        entry = self.record["funding_history"][0]
        self.assertEqual(entry["year_raw"], "2566")
        self.assertIn("มูลนิธิโครงการหลวง", entry["source_raw"] or "")

    def test_target_groups_outcomes_impacts(self) -> None:
        groups = self.record["target_groups"]
        self.assertTrue(any(g["group_raw"] == "ภาคประชาชน/สังคม" for g in groups))
        self.assertEqual(self.record["outcomes"], ["เส้นใยกัญชง"])

    def test_area_codes_extracted(self) -> None:
        areas = self.record["areas"]
        self.assertIsNotNone(areas)
        province_ids = {area["province_id"] for area in areas}
        self.assertEqual(province_ids, {"50", "57"})

    def test_description_sections(self) -> None:
        labels = [section["label"] for section in self.record["description_sections"]]
        self.assertIn("ความรู้ / เทคโนโลยี", labels)
        self.assertIn("จุดเด่น", labels)

    def test_ip_block(self) -> None:
        ip = self.record["ip"]
        self.assertIsNotNone(ip)
        types = [entry["value_raw"] for entry in ip if entry["label_raw"] == "ประเภท"]
        self.assertIn("สิทธิบัตร (Patent)", types)

    def test_second_fixture_variants(self) -> None:
        record = scrape.parse_product_detail(
            load_fixture("product_3754.html"), {"product_id": "3754"}
        )
        self.assertEqual(record["trl"]["level"], 9)
        self.assertEqual(record["price_thb"], 25000)
        self.assertEqual(record["location_latlng_raw"], "17.177551,103.837750")
        labels = [section["label"] for section in record["description_sections"]]
        self.assertIn("เงื่อนไขการใช้งาน", labels)

        record = scrape.parse_product_detail(
            load_fixture("product_9285.html"), {"product_id": "9285"}
        )
        self.assertEqual(record["price_thb"], 5)


class AreaResolverTests(unittest.TestCase):
    def test_resolve_areas_with_prefilled_dictionaries(self) -> None:
        resolver = scrape.AreaNameResolver(None, scrape.no_progress)  # type: ignore[arg-type]
        resolver._provinces = {"50": "เชียงใหม่"}
        resolver._districts = {"5007": "อำเภอสันทราย"}
        resolver._tambons = {"500708": "ตำบลริมใต้"}
        areas = resolver.resolve_areas(["50"], ["5007"], ["500708"])
        self.assertEqual(len(areas), 1)
        self.assertEqual(areas[0]["province"], "เชียงใหม่")
        self.assertEqual(areas[0]["districts"], ["อำเภอสันทราย"])
        self.assertEqual(areas[0]["tambons"], ["ตำบลริมใต้"])

    def test_parse_product_without_resolver_keeps_codes(self) -> None:
        record = scrape.parse_product_detail(
            load_fixture("product_4648.html"), {"product_id": "4648"}, None
        )
        self.assertTrue(all(area["province"] is None for area in record["areas"]))
        self.assertTrue(
            any("areas" in warning for warning in record["validation_warnings"])
        )


class StationParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.record = scrape.parse_station_detail(
            load_fixture("station_325.html"), {"station_id": "325"}
        )

    def test_identity_and_project_info(self) -> None:
        self.assertEqual(self.record["record_id"], "325")
        self.assertIn("โครงการจัดตั้งและพัฒนาศูนย์", self.record["title"])
        self.assertEqual(self.record["director_raw"], "ผศ.ดร.นพพร พัชรประกิติ")
        self.assertEqual(
            self.record["organization_raw"], "มหาวิทยาลัยเทคโนโลยีราชมงคลล้านนา"
        )
        self.assertEqual(self.record["start_year_raw"], "2569")

    def test_contact_panel(self) -> None:
        self.assertIn("เชียงใหม่", self.record["contact"].get("ที่ตั้ง", ""))
        self.assertIn("โทรศัพท์", self.record["contact"])

    def test_technology_mapping(self) -> None:
        self.assertIn("8777", self.record["product_ids"])
        self.assertIn("8779", self.record["product_ids"])
        self.assertGreaterEqual(len(self.record["product_ids"]), 7)


class ResearcherParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = scrape.parse_researchers_page(
            load_fixture("researchers_page1.html")
        )

    def test_card_count(self) -> None:
        self.assertEqual(len(self.records), 12)

    def test_first_card_fields(self) -> None:
        first = self.records[0]
        self.assertEqual(first["record_type"], "researcher")
        self.assertTrue(first["record_id"].isdigit())
        self.assertTrue(first["title"])
        self.assertTrue(
            first["profile_url"].endswith(f"product/agent/{first['record_id']}")
        )
        self.assertIsNotNone(first["expertise_raw"])
        self.assertIsInstance(first["innovation_count"], int)


class UniversityParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = scrape.parse_universities_page(
            load_fixture("universities_page1.html")
        )

    def test_card_count(self) -> None:
        self.assertEqual(len(self.records), 12)

    def test_stats_parsed(self) -> None:
        stats = self.records[0]["stats"]
        self.assertIsNotNone(stats)
        self.assertIn("นักวิจัย", stats)
        self.assertIn("นวัตกรรม", stats)


class DashboardExtractionTests(unittest.TestCase):
    def test_family_economic_metrics_are_parsed_from_cards(self) -> None:
        metrics = scrape.extract_family_economic_metrics(
            load_fixture("dashboard_family_all.html")
        )
        self.assertEqual(metrics["cost_reduction"]["thb"], 160951488)
        self.assertEqual(metrics["income_increase"]["thb"], 836895050)
        self.assertEqual(metrics["net_income_increase"]["thb"], 997846539)
        self.assertEqual(metrics["net_formula"]["component_sum_thb"], 997846538)
        self.assertEqual(metrics["net_formula"]["difference_thb"], 1)
        self.assertFalse(metrics["net_formula"]["matches_displayed_net"])

    def test_family_economic_metrics_fail_when_a_card_is_missing(self) -> None:
        with self.assertRaises(ValueError):
            scrape.extract_family_economic_metrics(
                '<div class="eco-card bg-red"><div class="small">'
                "มูลค่าต้นทุนที่ลดลงรวม</div><div class=\"h3\">1 ฿</div></div>"
            )

    def test_innovators_blob_sums_to_anchor(self) -> None:
        data = scrape.extract_inline_json(
            load_fixture("dashboard_innovators.html"), "provData"
        )
        self.assertEqual(len(data), 76)
        total = sum(entry.get("total_inno", 0) for entry in data.values())
        self.assertEqual(total, 12063)
        levels = {
            level: sum(entry.get("levels", {}).get(level, 0) for entry in data.values())
            for level in ("1", "2", "3", "4")
        }
        self.assertEqual(sum(levels.values()), 12063)

    def test_family_blob_sums_to_anchor(self) -> None:
        data = scrape.extract_inline_json(
            load_fixture("dashboard_family_all.html"), "provData"
        )
        self.assertEqual(len(data), 75)
        self.assertEqual(
            sum(entry.get("total_hh", 0) for entry in data.values()), 22945
        )
        self.assertEqual(
            sum(entry.get("total_members", 0) for entry in data.values()), 17295
        )

    def test_map_blob_matches_product_catalog_size(self) -> None:
        data = scrape.extract_inline_json(
            load_fixture("dashboard_innovation_map.html"), "provinceData"
        )
        unique_ids = {item["prod_id"] for items in data.values() for item in items}
        self.assertEqual(len(unique_ids), 1172)
        self.assertEqual(sum(len(items) for items in data.values()), 1506)

    def test_missing_variable_fails_loudly(self) -> None:
        with self.assertRaises(ValueError):
            scrape.extract_inline_json("<html></html>", "provData")


class SearchDiscoveryGuardTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, html: str):
            self.text = html

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        def __init__(self, pages: list[str]):
            self.pages = pages
            self.calls = 0

        def get(self, url: str, timeout: int = 45):  # noqa: ANN001
            page = self.pages[min(self.calls, len(self.pages) - 1)]
            self.calls += 1
            return SearchDiscoveryGuardTests.FakeResponse(page)

    PAGE_ONE = (
        '<div class="text-muted font-weight-bold">พบข้อมูล '
        '<span class="text-primary">1,172</span> รายการ</div>'
        '<a href="https://pmua-apptech.com/product/show/101" class="prod-title">A</a>'
        '<a href="https://pmua-apptech.com/product/show/102" class="prod-title">B</a>'
    )

    def test_parse_search_total(self) -> None:
        self.assertEqual(scrape.parse_search_total(self.PAGE_ONE), 1172)
        self.assertIsNone(scrape.parse_search_total("<html></html>"))

    def test_stuck_pagination_raises(self) -> None:
        session: Any = self.FakeSession([self.PAGE_ONE])
        with self.assertRaises(ValueError):
            scrape.discover_products(session, 0, scrape.no_progress)

    def test_limit_stops_early(self) -> None:
        page_two = (
            '<div class="text-muted font-weight-bold">พบข้อมูล '
            '<span class="text-primary">1,172</span> รายการ</div>'
            '<a href="https://pmua-apptech.com/product/show/103" class="prod-title">C</a>'
        )
        session: Any = self.FakeSession([self.PAGE_ONE, page_two, page_two])
        rows, reported = scrape.discover_products(
            session, 0, scrape.no_progress, limit=3
        )
        self.assertEqual(reported, 1172)
        self.assertEqual([row["product_id"] for row in rows], ["101", "102", "103"])


class StateTests(unittest.TestCase):
    def _sync_once(self, connection, run_id: str, title: str = "same") -> dict:
        record = scrape.build_record(
            "station",
            "900",
            title,
            {"detail_url": "https://example.com"},
            [],
        )
        return scrape.sync_records(connection, "station", 1, [record], run_id, "now")

    def test_rerun_creates_no_new_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = scrape.open_database(Path(directory) / "state.sqlite3")
            counts = self._sync_once(connection, "run-1")
            self.assertEqual(counts["new"], 1)
            counts = self._sync_once(connection, "run-2")
            self.assertEqual(counts["unchanged"], 1)
            versions = connection.execute(
                "SELECT COUNT(*) FROM record_versions"
            ).fetchone()[0]
            self.assertEqual(versions, 1)
            connection.close()

    def test_changed_content_creates_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = scrape.open_database(Path(directory) / "state.sqlite3")
            self._sync_once(connection, "run-1", "old title")
            counts = self._sync_once(connection, "run-2", "new title")
            self.assertEqual(counts["updated"], 1)
            versions = connection.execute(
                "SELECT change_type FROM record_versions ORDER BY version_no"
            ).fetchall()
            self.assertEqual(
                [row["change_type"] for row in versions], ["created", "updated"]
            )
            connection.close()

    def test_duplicate_ids_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = scrape.open_database(Path(directory) / "state.sqlite3")
            record_a = scrape.build_record(
                "station", "901", "A", {"detail_url": "u"}, []
            )
            record_b = scrape.build_record(
                "station", "901", "B", {"detail_url": "v"}, []
            )
            with self.assertRaises(ValueError):
                scrape.sync_records(
                    connection, "station", 1, [record_a, record_b], "r", "now"
                )
            connection.close()

    def test_minimum_records_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = scrape.open_database(Path(directory) / "state.sqlite3")
            with self.assertRaises(ValueError):
                scrape.sync_records(connection, "product", 1000, [], "r", "now")
            connection.close()


class CountDriftTests(unittest.TestCase):
    def test_no_warning_near_anchor(self) -> None:
        warnings: list[str] = []
        scrape.warn_count_drift("product", 1172, warnings)
        self.assertEqual(warnings, [])

    def test_warning_on_large_drift(self) -> None:
        warnings: list[str] = []
        scrape.warn_count_drift("product", 800, warnings)
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
