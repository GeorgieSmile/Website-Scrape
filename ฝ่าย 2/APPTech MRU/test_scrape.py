"""Offline checks for the APPTech MRU parser and previous-output safeguards."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import scrape

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


INNOVATION_LISTING_ROW = {
    "innovationid": "10620",
    "innovationname": "นวัตกรรมการผลิตมันสำปะหลังแบบ “ต้นทุนต่ำ–ผลผลิตสูง”",
    "userid": "12287",
    "userfullname": "ผู้ช่วยศาสตราจารย์ ดร.ไพบูลย์ หมุ่ยมาศ",
    "firstimgpath": "/store/innovation/699472714d125.jpg",
    "profileimg": "/store/user/6994694a9e309.jpg",
    "affiliationname": "มหาวิทยาลัยราชภัฏลำปาง",
}

REQUIREMENT_LISTING_ROW = {
    "requirementid": "15126",
    "requirementname": "นวัตกรรมโรงอบแห้งพริกพลังงานแสงอาทิตย์",
    "userid": "12274",
    "userfullname": "อาจารย์พนารัตน์ สังข์อินทร์",
    "firstimgpath": None,
    "profileimg": "/store/user/695cae034fab1.png",
    "affiliationname": "มหาวิทยาลัยราชภัฏศรีสะเกษ",
}


class CleaningTests(unittest.TestCase):
    def test_text_normalization_and_placeholders(self) -> None:
        self.assertEqual(
            scrape.clean_text("\ufeff  นวัตกรรม\u00a0 พร้อมใช้  "), "นวัตกรรม พร้อมใช้"
        )
        self.assertIsNone(scrape.optional_text(" - "))
        self.assertIsNone(scrape.optional_text("ไม่มี"))
        self.assertIsNone(scrape.optional_text("-ไม่มี-"))

    def test_semantic_hash_ignores_view_counter(self) -> None:
        before = {"title": "A", "data": {"view_count": 1}}
        after = {"title": "A", "data": {"view_count": 99}}
        self.assertEqual(scrape.semantic_hash(before), scrape.semantic_hash(after))

    def test_thb_amount_parses_only_unambiguous_values(self) -> None:
        self.assertEqual(scrape.parse_thb_amount("550,000 บาท"), 550000)
        self.assertIsNone(scrape.parse_thb_amount("ประมาณ 500,000"))
        self.assertIsNone(scrape.parse_thb_amount(None))

    def test_trl_srl_level(self) -> None:
        self.assertEqual(scrape.parse_trl_srl_level("TRL7"), 7)
        self.assertEqual(scrape.parse_trl_srl_level("SRL7"), 7)
        self.assertIsNone(scrape.parse_trl_srl_level("TRL0"))
        self.assertIsNone(scrape.parse_trl_srl_level("TRL10"))

    def test_multi_space_split(self) -> None:
        self.assertEqual(
            scrape.split_multi_space("ไพบูลย์ หมุ่ยมาศ  ณรงค์ คชภักดี"),
            ["ไพบูลย์ หมุ่ยมาศ", "ณรงค์ คชภักดี"],
        )
        self.assertEqual(scrape.split_multi_space(None), [])


class ProgressTests(unittest.TestCase):
    def test_detail_progress_reports_first_interval_and_final_fetch(self) -> None:
        messages: list[str] = []
        for index in range(1, 13):
            scrape.report_detail_progress(
                messages.append, "innovation", index, 12, f"ID-{index}", 5
            )
        self.assertEqual(
            messages,
            [
                "innovation: 1/12 — fetching ID-1",
                "innovation: 5/12 — fetching ID-5",
                "innovation: 10/12 — fetching ID-10",
                "innovation: 12/12 — fetching ID-12",
            ],
        )

    def test_quiet_reporter_does_not_write_messages(self) -> None:
        self.assertIs(scrape.create_progress_reporter(True), scrape.no_progress)


class InnovationParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = scrape.parse_innovation_detail(
            load_fixture("innov_10620.html"), INNOVATION_LISTING_ROW
        )
        self.data = self.record["data"]

    def test_identity_and_listing_fields(self) -> None:
        self.assertEqual(self.record["record_id"], "10620")
        self.assertEqual(self.record["record_type"], "innovation")
        self.assertEqual(
            self.record["title"], "นวัตกรรมการผลิตมันสำปะหลังแบบ “ต้นทุนต่ำ–ผลผลิตสูง”"
        )
        self.assertEqual(
            self.record["detail_url"],
            "https://38rat.nstru.ac.th/page/innovationdetail.php?innovationid=10620",
        )
        self.assertEqual(self.data["listing"]["affiliation"], "มหาวิทยาลัยราชภัฏลำปาง")
        self.assertEqual(self.data["listing"]["owner_user_id"], "12287")

    def test_sidebar_badges(self) -> None:
        self.assertEqual(self.data["innovation_type"], "พร้อมใช้")
        self.assertEqual(self.data["category"], "เกษตรและเทคโนโลยี")
        # TRL comes from the published level image, not the stale commented heading.
        self.assertEqual(self.data["trl"]["level"], 6)
        self.assertTrue(self.data["trl"]["image_url"].endswith("TRL-6.png"))
        self.assertEqual(self.data["srl"]["level"], 7)
        self.assertEqual(self.data["srl"]["label_raw"], "SRL7")

    def test_description_and_ip_sections(self) -> None:
        labels = [section["label"] for section in self.data["description_sections"]]
        self.assertEqual(labels, ["รายละเอียด", "ความรู้/เทคโนโลยี"])
        self.assertIn("น้ำหมักชีวภาพ", self.data["description_sections"][0]["text"])
        self.assertEqual(self.data["rights_holder"], "มหาวิทยาลัยราชภัฏลำปาง")
        self.assertEqual(
            self.data["ip"]["property_name"], "นวัตกรรมการผลิตมันสำปะหลังแบบต้นทุนต่ำ ผลผลิตสูง"
        )
        self.assertEqual(self.data["ip"]["type"], "อนุสิทธิบัตร")
        self.assertEqual(self.data["ip"]["request_no_raw"], "อยู่ระหว่างยืนคำขอ")
        self.assertIsNone(self.data["ip"]["patent_no_raw"])

    def test_researchers_inventors_lists(self) -> None:
        self.assertEqual(
            self.data["inventors"],
            ["ไพบูลย์ หมุ่ยมาศ", "ณรงค์ คชภักดี"],
        )
        lead = self.data["researchers"][0]
        self.assertEqual(lead["role"], "lead")
        self.assertEqual(lead["name"], "ผู้ช่วยศาสตราจารย์ ดร. ไพบูลย์ หมุ่ยมาศ")
        self.assertEqual(lead["faculty"], "วิทยาศาสตร์")
        self.assertEqual(lead["university"], "มหาวิทยาลัยราชภัฏลำปาง")
        self.assertEqual(self.data["researchers"][1]["role"], "co")
        self.assertEqual(self.data["target_groups"], ["เกษตรกรผู้ผลิตมันสำปะหลัง"])
        self.assertTrue(self.data["highlights"])

    def test_funding_roi_and_value(self) -> None:
        funding = self.data["funding"][0]
        self.assertEqual(funding["source"], "แหล่งเงินทุนภายในมหาวิทยาลัย")
        self.assertEqual(funding["details_raw"], "กองทุนมหาวิทยาลัย 2565 ววน. 2567-2567")
        self.assertEqual(funding["amount_raw"], "550,000 บาท")
        self.assertEqual(funding["amount_thb"], 550000)
        self.assertEqual(
            self.data["roi"], {"indicator_raw": "รายได้เพิ่มขึ้น", "value_raw": "20%"}
        )
        self.assertIsNone(self.data["sroi"]["indicator_raw"])
        self.assertEqual(self.data["innovation_value_raw"], "1,500 / บาท")

    def test_placeholder_video_link_is_null(self) -> None:
        self.assertIsNone(self.data["video_url"])

    def test_not_found_page_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "no title heading"):
            scrape.parse_innovation_detail(
                load_fixture("innov_notfound.html"), INNOVATION_LISTING_ROW
            )


class DescriptionWarningTests(unittest.TestCase):
    """The no-description warning must distinguish a source data gap from a
    possible page-layout change, because only the second one questions the
    parser itself."""

    def test_placeholder_content_is_a_data_gap_not_a_parser_failure(self) -> None:
        html = """
        <html><body><h1 class="card-title">หัวข้อทดสอบ</h1>
        <h5>รายละเอียด</h5><h6>-</h6>
        <h5 class="mt-3">ความรู้/เทคโนโลยี</h5><h6>-</h6>
        </body></html>
        """
        record = scrape.parse_innovation_detail(html, INNOVATION_LISTING_ROW)
        self.assertEqual(record["data"]["description_sections"], [])
        self.assertIn(
            "Innovation page published no description text "
            "(section heading present; content empty or placeholder)",
            record["validation_warnings"],
        )

    def test_missing_headings_suggest_a_layout_change(self) -> None:
        html = """
        <html><body><h1 class="card-title">หัวข้อทดสอบ</h1>
        <h5 class="mt-3">เจ้าของสิทธิ์</h5><h6>มหาวิทยาลัยทดสอบ</h6>
        </body></html>
        """
        record = scrape.parse_innovation_detail(html, INNOVATION_LISTING_ROW)
        self.assertIn(
            "Innovation page has no description section heading "
            "(possible page-layout change; none of the expected labels matched)",
            record["validation_warnings"],
        )

    def test_requirement_placeholder_content_is_a_data_gap(self) -> None:
        html = """
        <html><body><h1 class="card-title">โจทย์ทดสอบ</h1>
        <h5>รายละเอียด</h5><h6>-</h6>
        </body></html>
        """
        record = scrape.parse_requirement_detail(html, REQUIREMENT_LISTING_ROW)
        self.assertIn(
            "Requirement page published no description text "
            "(section heading present; content empty or placeholder)",
            record["validation_warnings"],
        )


class RequirementParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = scrape.parse_requirement_detail(
            load_fixture("req_15126.html"), REQUIREMENT_LISTING_ROW
        )
        self.data = self.record["data"]

    def test_identity_and_sections(self) -> None:
        self.assertEqual(self.record["record_id"], "15126")
        self.assertEqual(self.record["record_type"], "requirement")
        self.assertIn("โรงอบแห้งพริก", self.record["title"])
        self.assertIn("ตู้อบแห้งพริก", self.data["short_description"])
        self.assertIn("จังหวัดศรีสะเกษ", self.data["description"])
        self.assertEqual(self.data["hired_external_expert_raw"], "ไม่เคย")

    def test_expertise_impact_and_badges(self) -> None:
        self.assertEqual(
            self.data["required_expertise"],
            [
                "เทคโนโลยีการอาหาร",
                "พัฒนาผลิตภัณฑ์อาหาร",
                "วิทยาศาสตร์และเทคโนโลยี",
                "วิศวกรรม",
                "พลังงานและสิ่งแวดล้อม",
            ],
        )
        self.assertEqual(
            self.data["impact"],
            {"economic_raw": None, "environmental_raw": None, "social_raw": None},
        )
        self.assertEqual(self.data["type_raw"], "พร้อมใช้")
        self.assertEqual(self.data["category"], "อาหาร")
        self.assertEqual(self.data["tags"], ["ตู้อบพลังงานแสงอาทิตย์", "โซล่าเซลล์"])
        self.assertEqual(self.data["funding"][0]["source"], None)
        self.assertEqual(self.data["funding"][0]["details_raw"], None)
        self.assertIsNone(self.data["video_url"])


class CatalogClientTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def api_response(self, rows: list[dict], totaldata: int) -> dict:
        return {
            "status": True,
            "message": "OK",
            "data": {"data": rows, "totaldata": totaldata, "maxpage": 42},
        }

    def test_pagination_stops_at_totaldata_and_validates(self) -> None:
        page_one = [{"innovationid": str(number)} for number in range(1, 13)]
        page_two = [{"innovationid": "100"}]
        with patch.object(
            scrape.requests.Session,
            "post",
            side_effect=[
                self.FakeResponse(self.api_response(page_one, 13)),
                self.FakeResponse(self.api_response(page_two, 13)),
            ],
        ):
            result = scrape.fetch_catalog(
                scrape.create_session("test"),
                scrape.DATASETS["innovations"],
                0,
                scrape.no_progress,
            )
        self.assertEqual(len(result), 13)
        self.assertEqual(result[-1]["innovationid"], "100")

    def test_duplicate_ids_fail_the_catalog(self) -> None:
        rows = [{"innovationid": "1"}, {"innovationid": "1"}]
        with patch.object(scrape.requests.Session, "post") as post:
            post.return_value.json.return_value = self.api_response(rows, 2)
            post.return_value.raise_for_status = lambda: None
            with self.assertRaisesRegex(ValueError, "duplicate"):
                scrape.fetch_catalog(
                    scrape.create_session("test"),
                    scrape.DATASETS["innovations"],
                    0,
                    scrape.no_progress,
                )

    def test_status_failure_fails_the_catalog(self) -> None:
        with patch.object(scrape.requests.Session, "post") as post:
            post.return_value.json.return_value = {
                "status": False,
                "message": "action not match",
            }
            post.return_value.raise_for_status = lambda: None
            with self.assertRaisesRegex(ValueError, "status failure"):
                scrape.fetch_catalog(
                    scrape.create_session("test"),
                    scrape.DATASETS["innovations"],
                    0,
                    scrape.no_progress,
                )


class BrowserChallengeTests(unittest.TestCase):
    def response(self) -> scrape.requests.Response:
        response = scrape.requests.Response()
        response.status_code = 200
        response._content = (
            b'<html><title>One moment, please...</title>'
            b'<body>Please wait while your request is being verified...</body></html>'
        )
        response.headers["Content-Type"] = "text/html"
        return response

    def test_catalog_challenge_has_actionable_error(self) -> None:
        with (
            patch.object(scrape.requests.Session, "post", return_value=self.response()),
            self.assertRaisesRegex(RuntimeError, "browser verification required"),
        ):
            scrape.fetch_catalog(
                scrape.create_session("test"),
                scrape.DATASETS["innovations"],
                0,
                scrape.no_progress,
            )

    def test_detail_challenge_is_rejected_before_parsing(self) -> None:
        with (
            patch.object(scrape.requests.Session, "get", return_value=self.response()),
            self.assertRaisesRegex(RuntimeError, "browser verification required"),
        ):
            scrape.fetch_detail_html(
                scrape.create_session("test"),
                scrape.DATASETS["innovations"],
                INNOVATION_LISTING_ROW,
            )


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
    def record(self, dataset_id: str, record_id: str, value: str) -> dict[str, object]:
        return scrape.build_record(
            dataset_id,
            record_id,
            f"Title {record_id}",
            f"https://38rat.nstru.ac.th/page/x?id={record_id}",
            {"value": value},
            [],
        )

    def dataset(self, minimum_records: int = 1) -> scrape.DatasetDefinition:
        return scrape.DatasetDefinition(
            dataset_id="innovation",
            output_name="innovations.json",
            api_url="unused",
            api_action="unused",
            id_field="innovationid",
            detail_url_template="unused",
            detail_title_selector="unused",
            minimum_records=minimum_records,
            parse_detail=lambda html, row: {},
        )

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "innovations.json"
        path.write_text(json.dumps({"data": records}), encoding="utf-8")
        return path

    def records(self, count: int, value: str = "same") -> list[dict[str, object]]:
        return [self.record("innovation", str(number), value) for number in range(1, count + 1)]

    def test_counts_new_updated_unchanged_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(4))
            current = self.records(2) + [
                self.record("innovation", "3", "changed"),
                self.record("innovation", "9", "same"),
            ]
            counts = scrape.compare_with_previous(self.dataset(), current, path)
        self.assertEqual(counts, {"new": 1, "updated": 1, "unchanged": 2, "removed": 1})

    def test_first_run_without_file_counts_everything_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            counts = scrape.compare_with_previous(
                self.dataset(), self.records(3), Path(directory) / "missing.json"
            )
        self.assertEqual(counts, {"new": 3, "updated": 0, "unchanged": 0, "removed": 0})

    def test_view_count_change_is_unchanged(self) -> None:
        base = self.record("innovation", "1", "same")
        bumped = self.record("innovation", "1", "same")
        bumped_data = bumped["data"]
        assert isinstance(bumped_data, dict)
        bumped_data["view_count"] = 999
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, [base])
            counts = scrape.compare_with_previous(self.dataset(), [bumped], path)
        self.assertEqual(counts["unchanged"], 1)

    def test_removal_guard_trips_above_threshold_and_override_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(12))
            with self.assertRaisesRegex(ValueError, "25% safety limit"):
                scrape.compare_with_previous(self.dataset(), self.records(8), path)
            counts = scrape.compare_with_previous(
                self.dataset(), self.records(8), path, allow_large_removal=True
            )
        self.assertEqual(counts["removed"], 4)

    def test_removal_guard_ignores_small_previous_datasets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(9))
            counts = scrape.compare_with_previous(self.dataset(), self.records(1), path)
        self.assertEqual(counts["removed"], 8)

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "innovations.json"
            bad_contents = (
                "{not json",
                json.dumps([]),
                json.dumps({"data": {}}),
                json.dumps({"data": [{"title": "no id"}]}),
                json.dumps({"data": self.records(1) * 2}),
            )
            for contents in bad_contents:
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    scrape.compare_with_previous(
                        self.dataset(), self.records(1), path, allow_large_removal=True
                    )

    def test_record_floor_and_duplicate_ids_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaisesRegex(ValueError, "expected at least"):
                scrape.compare_with_previous(self.dataset(minimum_records=5), self.records(2), missing)
            with self.assertRaisesRegex(ValueError, "duplicate record IDs"):
                scrape.compare_with_previous(self.dataset(), self.records(1) * 2, missing)


if __name__ == "__main__":
    unittest.main()
