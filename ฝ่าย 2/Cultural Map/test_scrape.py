"""Offline checks for the Cultural Map parser and previous-output safeguards."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import scrape


class CleaningTests(unittest.TestCase):
    def test_text_normalization_and_placeholders(self) -> None:
        self.assertEqual(scrape.clean_text("\ufeff  บ้าน\u00a0 วัด  "), "บ้าน วัด")
        self.assertIsNone(scrape.optional_text(" - "))

    def test_semantic_hash_ignores_view_counter(self) -> None:
        before = {"title": "A", "data": {"view_count": 1}}
        after = {"title": "A", "data": {"view_count": 99}}
        self.assertEqual(scrape.semantic_hash(before), scrape.semantic_hash(after))

    def test_coordinate_validation_keeps_warning(self) -> None:
        warnings: list[str] = []
        self.assertIsNone(scrape.parse_coordinate("181", warnings, "longitude"))
        self.assertIn("Out-of-range coordinate in longitude: 181", warnings)

    def test_source_date_accepts_buddhist_era_slash_dates(self) -> None:
        warnings: list[str] = []
        self.assertEqual(scrape.normalize_source_date("6/12/2564.", "DOR", warnings), "2021-12-06")
        self.assertEqual(warnings, [])


class ProgressTests(unittest.TestCase):
    def test_detail_progress_reports_first_interval_and_final_fetch(self) -> None:
        messages: list[str] = []
        for index in range(1, 13):
            scrape.report_detail_progress(messages.append, "Products", index, 12, f"PD-{index}", 5)
        self.assertEqual(
            messages,
            [
                "Products: 1/12 — fetching PD-1",
                "Products: 5/12 — fetching PD-5",
                "Products: 10/12 — fetching PD-10",
                "Products: 12/12 — fetching PD-12",
            ],
        )

    def test_quiet_reporter_does_not_write_messages(self) -> None:
        self.assertIs(scrape.create_progress_reporter(True), scrape.no_progress)


class DetailParserTests(unittest.TestCase):
    def test_map_inspiration_schema_uses_names_and_drops_raw_source_fields(self) -> None:
        source = {
            "CId": "36",
            "CulCodeNew": "AA-74000-00001",
            "CulCode": "AA-74000-001-ANC",
            "CulTName": "เรือโบราณ",
            "CulEName": "Ancient ship",
            "CulTypeId": "T",
            "CatId": "AA",
            "CatIdOther": ",AA7",
            "CulDistrict": "740113",
            "CulProvince": "59",
            "CulAmphure": "814",
            "CulPostalCode": "74000",
            "CulLocationLa": "13.5337461",
            "CulLocationLo": "100.3804955",
            "CulHistory": "ประวัติ",
            "FundType": "",
            "UniCode": "19500",
            "UserNameRecord": "ผู้บันทึก",
            "UserName": "recorder@example.org",
            "DOR": "2021-08-29",
            "InsDate": "2021-09-12 14:08:45",
            "EditDate": "0000-00-00 00:00:00",
            "CulStatusRisk": "5",
            "count_view": "1,234",
        }
        with patch.object(scrape, "fetch_json", return_value=[source]):
            record = scrape.collect_map_inspiration(scrape.create_session("test"), 0)[0]
        data = record["data"]
        administrative = data["location"]["administrative"]
        self.assertNotIn("source_fields", data)
        self.assertEqual(administrative["province"]["name_th"], "สมุทรสาคร")
        self.assertEqual(administrative["amphure"]["name_th"], "เมืองสมุทรสาคร")
        self.assertEqual(administrative["tambon"]["name_th"], "พันท้ายนรสิงห์")
        self.assertIsNone(data["funding"]["project_funded"])
        self.assertEqual(data["funding"]["status"], "not_indicated")
        self.assertEqual(data["dates"]["inserted"], "2021-09-12T14:08:45")
        self.assertEqual(data["metrics"]["view_count"], 1234)

    def test_map_inspiration_fallback_resolves_only_supported_location_levels(self) -> None:
        source = {"CId": "470", "CulTName": "บ้านจงดีขนมไทย", "CulProvince": "70"}
        with patch.object(scrape, "fetch_json", return_value=[source]):
            record = scrape.collect_map_inspiration(scrape.create_session("test"), 0)[0]
        administrative = record["data"]["location"]["administrative"]
        self.assertEqual(administrative["province"]["name_th"], "สงขลา")
        self.assertIsNone(administrative["amphure"])
        self.assertIsNone(administrative["tambon"])
        self.assertIn(
            "Missing standard Tambon code; Province resolved from verified local-ID crosswalk",
            record["validation_warnings"],
        )

    def test_product_parser_extracts_published_fields_without_following_links(self) -> None:
        html = """
        <section class="culturalproductinfo">
          <h2 class="mycontainer2header">ผ้าทอ</h2>
          <a href="P-2">เครื่องแต่งกาย</a><a href="CD-12">บ้านทอผ้า</a>
          ราคา : 250 บาท รายละเอียด : ทอมือ ช่องทางการจำหน่าย : ตลาดชุมชน
          เลขที่ : 10 ถนนตัวอย่าง เข้าชม : 1,234 ครั้ง
          <a href="https://example.org/shop">ร้านค้า</a>
          <a data-fancybox="gallery" href="/images/a.jpg" data-caption="ด้านหน้า"></a>
        </section>
        """
        item = {
            "external_id": "PD-9",
            "source_url": "https://www.culturalmapthailand.info/PD-9",
            "discovered_from": ["https://www.culturalmapthailand.info/P-2"],
        }
        record = scrape.parse_product_detail(html, item)
        self.assertEqual(record["title"], "ผ้าทอ")
        self.assertEqual(record["data"]["related_cultural_record"], "CD-12")
        self.assertEqual(record["data"]["view_count"], 1234)
        self.assertEqual(record["data"]["external_links"][0]["url"], "https://example.org/shop")
        self.assertEqual(record["data"]["gallery_images"][0]["url"], "https://www.culturalmapthailand.info/images/a.jpg")

    def test_activity_and_recreation_parsers_accept_known_public_structure(self) -> None:
        activity = scrape.parse_activity_detail(
            "<h2 class='mycontainer2header smallheaderfontsize'>งานบุญ</h2>"
            "<div class='wrapword'>วันจัดงาน 1 มกราคม 2569 รายละเอียดกิจกรรม</div>",
            {"external_id": "G-1", "source_url": "https://www.culturalmapthailand.info/G-1", "discovered_from": []},
        )
        recreation = scrape.parse_recreation_detail(
            "<main class='container-xxl'><h4 class='display-9'>งานสร้างสรรค์</h4>"
            "<h5>ประเภท : งานฝีมือ</h5> Re-creation : แบบใหม่ รายละเอียด : รายละเอียดงาน "
            "ชื่อทีม : ทีมเอ รายชื่อ : 1. ก ข เข้าชม : 25 ครั้ง</main>",
            {"external_id": "REDetail-1", "source_url": "https://www.culturalmapthailand.info/REDetail-1", "discovered_from": []},
        )
        self.assertEqual(activity["data"]["date_text"], "วันจัดงาน 1 มกราคม 2569")
        self.assertEqual(recreation["data"]["recreation_category"], "งานฝีมือ")
        self.assertEqual(recreation["data"]["view_count"], 25)
        self.assertNotIn("external_links", recreation["data"])

    def test_team_parser_assigns_group_and_image(self) -> None:
        html = """
        <h2 class="display-6">คณะทำงาน</h2>
        <div class="team-item"><img class="img-thumbnail img-fluid" src="/team/a.jpg"><h6 class="mt-2">Alice</h6></div>
        <div class="team-item"><img class="img-thumbnail img-fluid" src="/team/b.jpg"><h6 class="mt-2">Bob</h6></div>
        """
        with patch.object(scrape, "fetch_text", return_value=html):
            records = scrape.collect_team(scrape.create_session("test"), 0)
        self.assertEqual([record["title"] for record in records], ["Alice", "Bob"])
        self.assertEqual(records[0]["data"]["group"], "คณะทำงาน")
        self.assertTrue(records[1]["data"]["profile_image_url"].endswith("/team/b.jpg"))


class FetchOrderedTests(unittest.TestCase):
    def test_results_come_back_in_input_order(self) -> None:
        def fetch(number: int) -> int:
            time.sleep(0.01 * (5 - number))  # Later items finish first.
            return number * 10

        results = list(scrape.fetch_ordered(fetch, [0, 1, 2, 3, 4], 4))
        self.assertEqual(results, [0, 10, 20, 30, 40])

    def test_first_error_propagates_and_cancels_pending_work(self) -> None:
        calls: list[int] = []

        def fetch(number: int) -> int:
            calls.append(number)
            time.sleep(0.02)
            if number == 1:
                raise RuntimeError("boom")
            return number

        with self.assertRaisesRegex(RuntimeError, "boom"):
            list(scrape.fetch_ordered(fetch, list(range(50)), 1))
        self.assertLess(len(calls), 50)


class CompareWithPreviousTests(unittest.TestCase):
    def record(self, external_id: str, value: str = "same") -> dict[str, object]:
        return scrape.build_record(
            external_id,
            f"Title {external_id}",
            f"https://www.culturalmapthailand.info/{external_id}",
            [],
            {"value": value},
            [],
        )

    def source(self, minimum_records: int = 1) -> scrape.SourceDefinition:
        return scrape.SourceDefinition("test", "test.json", 1, (), minimum_records, lambda _s, _d, _l, _r, _p, _w, _u: [])

    def records(self, count: int, value: str = "same") -> list[dict[str, object]]:
        return [self.record(f"X-{number}", value) for number in range(1, count + 1)]

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "test.json"
        path.write_text(json.dumps({"data": {"records": records}}), encoding="utf-8")
        return path

    def test_counts_new_updated_unchanged_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(4))
            current = self.records(2) + [self.record("X-3", "changed"), self.record("X-9")]
            counts = scrape.compare_with_previous(self.source(), current, path)
        self.assertEqual(counts, {"new": 1, "updated": 1, "unchanged": 2, "removed": 1})

    def test_first_run_without_file_counts_everything_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            counts = scrape.compare_with_previous(self.source(), self.records(3), Path(directory) / "missing.json")
        self.assertEqual(counts, {"new": 3, "updated": 0, "unchanged": 0, "removed": 0})

    def test_view_count_change_is_unchanged(self) -> None:
        base = self.record("X-1")
        bumped = self.record("X-1")
        bumped_data = bumped["data"]
        assert isinstance(bumped_data, dict)
        bumped_data["view_count"] = 999
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, [base])
            counts = scrape.compare_with_previous(self.source(), [bumped], path)
        self.assertEqual(counts["unchanged"], 1)

    def test_large_removal_is_rejected_and_override_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(4))
            with self.assertRaisesRegex(ValueError, "25% safety limit"):
                scrape.compare_with_previous(self.source(), self.records(1), path)
            counts = scrape.compare_with_previous(self.source(), self.records(1), path, allow_large_removal=True)
        self.assertEqual(counts["removed"], 3)

    def test_removal_at_threshold_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(4))
            counts = scrape.compare_with_previous(self.source(), self.records(3), path)
        self.assertEqual(counts["removed"], 1)

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.json"
            bad_contents = (
                "{not json",
                json.dumps([]),
                json.dumps({"data": []}),
                json.dumps({"data": {"records": {}}}),
                json.dumps({"data": {"records": [{"title": "no id"}]}}),
                json.dumps({"data": {"records": self.records(1) * 2}}),
            )
            for contents in bad_contents:
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    scrape.compare_with_previous(self.source(), self.records(1), path, allow_large_removal=True)

    def test_record_floor_and_duplicate_ids_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaisesRegex(ValueError, "expected at least"):
                scrape.compare_with_previous(self.source(minimum_records=5), self.records(2), missing)
            with self.assertRaisesRegex(ValueError, "duplicate external IDs"):
                scrape.compare_with_previous(self.source(), self.records(1) * 2, missing)


if __name__ == "__main__":
    unittest.main()
