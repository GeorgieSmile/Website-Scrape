"""Offline checks for the Cultural Map parser and incremental state safeguards."""

from __future__ import annotations

import tempfile
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


class StateTests(unittest.TestCase):
    def record(self, external_id: str) -> dict[str, object]:
        return scrape.build_record(
            external_id,
            f"Title {external_id}",
            f"https://www.culturalmapthailand.info/{external_id}",
            [],
            {"value": external_id},
            [],
        )

    def test_large_removal_is_rejected_without_mutating_state(self) -> None:
        source = scrape.SourceDefinition("test", "test.json", 1, (), 1, lambda _s, _d, _l: [])
        with tempfile.TemporaryDirectory() as directory:
            connection = scrape.open_database(Path(directory) / "state.sqlite3")
            initial = [self.record(f"X-{number}") for number in range(1, 5)]
            scrape.sync_records(connection, source, initial, "run-1", "2026-01-01T00:00:00+00:00")
            with self.assertRaisesRegex(ValueError, "25% safety limit"):
                scrape.sync_records(connection, source, initial[:1], "run-2", "2026-01-02T00:00:00+00:00")
            self.assertEqual(len(scrape.active_records(connection, "test")), 4)
            connection.close()


if __name__ == "__main__":
    unittest.main()
