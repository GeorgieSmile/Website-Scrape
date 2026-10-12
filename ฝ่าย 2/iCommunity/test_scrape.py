"""Offline checks for the iCommunity scraper."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import scrape


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CleaningTests(unittest.TestCase):
    def test_text_and_contact_cleaning(self) -> None:
        self.assertEqual(scrape.clean_text("\ufeff  บ้าน\u00a0 วัด  "), "บ้าน วัด")
        self.assertIsNone(scrape.clean_text(" - "))
        result = scrape.redact_contacts("โทร 081-234-5678 email a@example.com Line: hello")
        self.assertNotIn("081-234-5678", result or "")
        self.assertNotIn("a@example.com", result or "")
        self.assertIn("[phone redacted]", result or "")

    def test_hash_is_key_order_independent(self) -> None:
        self.assertEqual(scrape.semantic_hash({"a": 1, "b": 2}), scrape.semantic_hash({"b": 2, "a": 1}))


class ParserTests(unittest.TestCase):
    def test_dashboard_groups_include_zero_through_eight(self) -> None:
        html = (FIXTURES / "dashboard_groups.html").read_text(encoding="utf-8")
        rows = scrape.dashboard_groups(html, "innovator", "https://example.test/dashboard")
        self.assertEqual([row["group_id"] for row in rows], [str(value) for value in range(9)])
        self.assertEqual(rows[2]["group_label_th"], "ชุมชนนวัตกรรมแห่งการเรียนรู้")

    def test_innovator_detail_scores(self) -> None:
        html = (FIXTURES / "innovator_detail.html").read_text(encoding="utf-8")
        detail = scrape.parse_innovator_detail(html, "https://example.test/profile")
        self.assertEqual(detail["collaboration_network"], {"start_level": 3, "current_level": 4})
        self.assertEqual(detail["technology_knowledge"], {"start_level": 2, "current_level": 3})
        self.assertEqual(detail["learning_process_management"], {"start_level": 1, "current_level": 4})

    def test_innovation_detail_fields(self) -> None:
        html = (FIXTURES / "innovation_detail.html").read_text(encoding="utf-8")
        detail = scrape.parse_innovation_detail(html, "https://example.test/innovation")
        self.assertEqual(detail["innovation_type"], "กระบวนการ (Process)")
        self.assertEqual(detail["technology_readiness"], {"start_level": 3, "current_level": 4})
        self.assertEqual(detail["social_readiness"], {"start_level": 2, "current_level": 3})
        self.assertEqual(detail["usage_cost_thb_raw"], "0.00 บาท")
        self.assertEqual(detail["short_description_text"], "เทคโนโลยีทดสอบ สำหรับชุมชน")

    def test_paginated_collector_reconciles_headers(self) -> None:
        calls: list[str] = []

        def fake_fetch_json(_session, url, **_kwargs):
            calls.append(url)
            page = 2 if "page=2" in url else 1
            return ([{"id": page}], {"X-WP-TotalPages": "2", "X-WP-Total": "2"})

        with patch.object(scrape, "fetch_json", side_effect=fake_fetch_json):
            data = scrape.collect_paginated(scrape.create_session("test"), "/wp/v2/posts", 1, 0, lambda _: None)
        self.assertEqual(data, [{"id": 1}, {"id": 2}])
        self.assertEqual(len(calls), 2)


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


class ConcurrentDetailTests(unittest.TestCase):
    def test_detail_failures_become_null_detail_with_warning_in_token_order(self) -> None:
        rows = [
            {"innovatorID": str(number), "innovatorName": f"N{number}", "innovatorOutstandingID": f"tok{number}"}
            for number in range(1, 6)
        ]
        good = (FIXTURES / "innovator_detail.html").read_text(encoding="utf-8")

        def fake_fetch_text(_session, url, timeout=60):
            time.sleep(0.01 * (6 - int(url[-1])))  # Later tokens finish first.
            if url.endswith("tok2"):
                raise requests.ConnectionError("down")
            return "<html></html>" if url.endswith("tok4") else good

        def run(workers: int):
            with (
                patch.object(scrape, "fetch_json", return_value=({"data": rows, "recordsFiltered": 5}, {})),
                patch.object(scrape, "fetch_text", fake_fetch_text),
            ):
                return scrape.collect_apptech_records(
                    scrape.create_session("test"), entity="innovator", groups={"1": "G"}, group_filter=None,
                    page_size=250, limit=None, delay=0, timeout=1, skip_details=False,
                    reporter=lambda _: None, workers=workers,
                )

        sequential, concurrent = run(1), run(4)
        self.assertEqual(sequential, concurrent)
        records, warnings, _ = concurrent
        self.assertEqual([record["detail"] is None for record in records], [False, True, False, True, False])
        self.assertEqual(len(warnings), 2)
        self.assertIn("tok2 request failed: down", warnings[0])
        self.assertIn("tok4 parse failed", warnings[1])


class CompareWithPreviousTests(unittest.TestCase):
    DATASET = "community_products"

    def record(self, key: str = "1", value: str = "A") -> dict[str, object]:
        return {"record_type": "community_product", "source_key": key, "name": value}

    def records(self, count: int, value: str = "A") -> list[dict[str, object]]:
        return [self.record(str(number), value) for number in range(1, count + 1)]

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "community_products.json"
        path.write_text(json.dumps({"data": records}), encoding="utf-8")
        return path

    def compare(self, records, path, allow_large_removal=False):
        return scrape.compare_with_previous(self.DATASET, records, path, allow_large_removal=allow_large_removal)

    def test_counts_new_changed_unchanged_and_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(4))
            current = self.records(2) + [self.record("3", "changed"), self.record("9")]
            counts = self.compare(current, path)
        self.assertEqual(counts, {"new": 1, "changed": 1, "unchanged": 2, "retired": 1})

    def test_retrieved_at_alone_does_not_count_as_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = {**self.record(), "retrieved_at": "2026-01-01T00:00:00Z"}
            path = self.write_previous(directory, [old])
            same = self.compare([{**self.record(), "retrieved_at": "2026-02-02T00:00:00Z"}], path)
            changed = self.compare([{**self.record(value="B"), "retrieved_at": "x"}], path)
        self.assertEqual(same["unchanged"], 1)
        self.assertEqual(changed["changed"], 1)

    def test_first_run_without_file_counts_everything_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            counts = self.compare(self.records(3), Path(directory) / "missing.json")
        self.assertEqual(counts, {"new": 3, "changed": 0, "unchanged": 0, "retired": 0})

    def test_removal_guard_trips_at_half_with_ten_record_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(30))
            self.assertEqual(self.compare(self.records(16), path)["retired"], 14)  # 14 < 15
            with self.assertRaisesRegex(ValueError, "refusing to retire 15 of 30"):
                self.compare(self.records(15), path)
            self.assertEqual(self.compare(self.records(15), path, allow_large_removal=True)["retired"], 15)

    def test_removal_guard_minimum_is_ten_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(12))
            self.assertEqual(self.compare(self.records(3), path)["retired"], 9)  # 9 < max(10, 6)
            with self.assertRaises(ValueError):
                self.compare(self.records(2), path)  # 10 retired

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "community_products.json"
            bad_contents = (
                "{not json",
                json.dumps([]),
                json.dumps({"data": {}}),
                json.dumps({"data": [{"name": "no key"}]}),
                json.dumps({"data": self.records(1) * 2}),
            )
            for contents in bad_contents:
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    self.compare(self.records(1), path, allow_large_removal=True)

    def test_duplicate_source_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            scrape.validate_records("community_products", [self.record("1", "A"), self.record("1", "B")])

    def test_skip_details_retains_previous_detail(self) -> None:
        complete = {
            "record_type": "apptech_innovator",
            "source_key": "2:42",
            "name": "A",
            "detail": {"collaboration_network": {"start_level": 1, "current_level": 2}},
        }
        smoke = {**complete, "name": "B", "detail": None}
        scrape.retain_known_details({"2:42": complete}, "apptech_innovators", [smoke])
        self.assertEqual(smoke["detail"], complete["detail"])


if __name__ == "__main__":
    unittest.main()
