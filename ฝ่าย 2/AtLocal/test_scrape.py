"""Offline parser and previous-output comparison checks for the At Local scraper."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scrape

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_json(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class CleaningTests(unittest.TestCase):
    def test_clean_text_and_html_text(self) -> None:
        self.assertEqual(scrape.clean_text("\ufeff  บ้าน\u00a0 วัด  "), "บ้าน วัด")
        self.assertIsNone(scrape.clean_text(" - "))
        self.assertEqual(scrape.html_text("<p>บ้าน <strong>วัด</strong></p>"), "บ้าน วัด")

    def test_map_json_array_parser_reports_bad_input(self) -> None:
        warnings: list[str] = []
        self.assertEqual(scrape.parse_json_list("[bad", warnings, "tags"), [])
        self.assertEqual(warnings, ["Malformed JSON in tags"])

    def test_16_area_value_split(self) -> None:
        self.assertEqual(scrape.split_area_value("กาญจนบุรี;3", "province"), ("กาญจนบุรี", "3"))
        with self.assertRaises(ValueError):
            scrape.split_area_value("กาญจนบุรี", "province")


class ApiParserTests(unittest.TestCase):
    def test_articles_include_filter_metadata(self) -> None:
        payload = load_json("articles_sample.json")
        with patch.object(scrape, "fetch_json", return_value=payload):
            records, metadata = scrape.collect_articles(scrape.create_session("test"), 0, 0, lambda _: None)
        self.assertEqual(records[0]["article_id"], "3")
        self.assertEqual(records[0]["summary_text"], "เนื้อหาตัวอย่าง")
        self.assertEqual(metadata["provinces"][0]["name"], "สระบุรี")

    def test_map_products_parse_nested_lists(self) -> None:
        payload = {"status": "Success", "data": [{"id": 634, "item_name": "บริการ", "unit_price": 30000, "picture_list": '["a.jpg"]', "tags": '["#tag"]', "shop_name": "ร้าน", "map": "", "item_type_id": 4, "item_type_name": "ความเชื่อ", "items_sale_channel_id": 1, "item_sale_channel": "ออนไลน์", "item_status_id": 2, "item_status": "จัดส่ง", "province_th": "ชลบุรี", "district_th": "พนัสนิคม", "exclusive": 0}]}
        with patch.object(scrape, "fetch_json", return_value=payload):
            records, _ = scrape.collect_product_map(scrape.create_session("test"), 0, 0, lambda _: None)
        self.assertEqual(records[0]["map_product_id"], 634)
        self.assertEqual(records[0]["tags"], ["#tag"])
        self.assertTrue(records[0]["image_urls"][0].endswith("/a.jpg"))

    def test_map_product_with_missing_name_is_preserved_with_warning(self) -> None:
        payload = {"status": "Success", "data": [{"id": 99, "item_name": "", "unit_price": 0, "picture_list": "[]", "tags": "[]", "shop_name": "ร้าน", "item_type_id": None, "item_type_name": "", "items_sale_channel_id": None, "item_sale_channel": "", "item_status_id": None, "item_status": "", "province_th": "A", "district_th": "B", "exclusive": 0}]}
        with patch.object(scrape, "fetch_json", return_value=payload):
            records, _ = scrape.collect_product_map(scrape.create_session("test"), 0, 0, lambda _: None)
        with patch.dict(scrape.SPECS, {"product_map": scrape.DatasetSpec("product_map", "x.json", (), 1)}):
            scrape.require_records("product_map", records)
        self.assertIsNone(records[0]["name"])
        self.assertIn("Missing item_name in source", records[0]["warnings"])

    def test_areas_join_parallel_arrays(self) -> None:
        payload = load_json("areas_16_sample.json")
        with patch.object(scrape, "fetch_json", return_value=payload):
            records, _ = scrape.collect_areas_16(scrape.create_session("test"), 0, 0, lambda _: None)
        self.assertEqual(records[0]["area_id"], "3")
        self.assertEqual(records[0]["image_filenames"], ["l657.jpg"])

    def test_areas_reject_inconsistent_ids(self) -> None:
        payload = {"status": "Success", "data": {"province": ["A;1"], "district": ["B;2"], "name": ["N;1"], "description": ["D;1"], "picture_list": ['[];1']}}
        with patch.object(scrape, "fetch_json", return_value=payload):
            with self.assertRaises(ValueError):
                scrape.collect_areas_16(scrape.create_session("test"), 0, 0, lambda _: None)


class HtmlParserTests(unittest.TestCase):
    def test_marketplace_rsc_parser_and_normalization(self) -> None:
        page = (FIXTURES / "marketplace_rsc_sample.html").read_text(encoding="utf-8")
        parsed = scrape.extract_marketplace_products(page)
        self.assertEqual(len(parsed), 1)
        record = scrape.marketplace_record(parsed[0])
        self.assertEqual(record["marketplace_product_id"], "1")
        self.assertEqual(record["price_thb"], 30000)
        self.assertTrue(record["product_url"].endswith("/1/บริการแสดงเอ็งกอ"))

    def test_festival_discovery_excludes_registration_page(self) -> None:
        page = '<a href="/festival/lampang/event1">One</a><a href="/festival/x/photo_regis">Skip</a>'
        self.assertEqual(scrape.festival_urls_from_activity(page), ["https://atlocalthailand.com/festival/lampang/event1"])

    def test_festival_record_uses_highlight_when_page_has_no_h1(self) -> None:
        record = scrape.festival_record("https://atlocalthailand.com/festival/a/event", {"id": "11", "title": "งาน", "imageUrl": "/cover.jpg"}, "<html><body><main><p>เนื้อหา</p></main></body></html>")
        self.assertEqual(record["title"], "งาน")
        self.assertEqual(record["description_text"], "เนื้อหา")
        self.assertTrue(record["cover_image_url"].endswith("/cover.jpg"))


class CompareWithPreviousTests(unittest.TestCase):
    def record(self, key: str, title: str = "A") -> dict[str, object]:
        return {"record_type": "article", "article_id": key, "title": title}

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "articles.json"
        path.write_text(json.dumps({"data": records}), encoding="utf-8")
        return path

    def test_counts_new_changed_unchanged_and_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(
                directory,
                [self.record("1"), self.record("2"), self.record("3"), self.record("4"), self.record("5")],
            )
            counts = scrape.compare_with_previous(
                "articles",
                [self.record("1"), self.record("2", "B"), self.record("3"), self.record("4"), self.record("6")],
                path,
                False,
            )
        self.assertEqual(counts, {"new": 1, "changed": 1, "unchanged": 3, "retired": 1})

    def test_first_run_without_file_counts_everything_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            counts = scrape.compare_with_previous(
                "articles", [self.record("1"), self.record("2")], Path(directory) / "missing.json", False
            )
        self.assertEqual(counts, {"new": 2, "changed": 0, "unchanged": 0, "retired": 0})

    def test_removal_guard_trips_above_threshold_and_override_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, [self.record(str(index)) for index in range(4)])
            with self.assertRaises(ValueError):
                scrape.compare_with_previous("articles", [self.record("0")], path, False)
            counts = scrape.compare_with_previous("articles", [self.record("0")], path, True)
        self.assertEqual(counts["retired"], 3)

    def test_removal_at_threshold_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, [self.record(str(index)) for index in range(4)])
            counts = scrape.compare_with_previous(
                "articles", [self.record(str(index)) for index in range(3)], path, False
            )
        self.assertEqual(counts["retired"], 1)

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "articles.json"
            bad_contents = ("{not json", json.dumps([]), json.dumps({"data": {}}), json.dumps({"data": [{"title": "no id"}]}), json.dumps({"data": [self.record("1"), self.record("1")]}))
            for contents in bad_contents:
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    scrape.compare_with_previous("articles", [self.record("1")], path, True)

    def test_duplicate_record_rejected(self) -> None:
        with self.assertRaises(ValueError):
            scrape.require_records("articles", [self.record("1"), self.record("1", "B")])


if __name__ == "__main__":
    unittest.main()
