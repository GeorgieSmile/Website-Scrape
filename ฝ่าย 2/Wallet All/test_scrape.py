"""Offline checks for the Wallet All scraper."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import scrape

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HH_URL = f"{scrape.API_BASE}/hh"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fake_post_month(hh_current: dict, current_month: str = "2024-09", calls: list | None = None, null_months=()):
    """Answer like the source: the current month for "", else the asked-for month."""

    def post(_session, _url, request_date, _timeout):
        if calls is not None:
            calls.append(request_date)
        payload = copy.deepcopy(hh_current)
        payload["thisMonth"] = request_date[:7] or current_month
        if payload["thisMonth"] in null_months:
            payload["snapshot"] = None
        return payload

    return post


class MonthHelperTests(unittest.TestCase):
    def test_month_end_handles_leap_years_and_december(self) -> None:
        self.assertEqual(scrape.month_end("2024-02"), "2024-02-29")
        self.assertEqual(scrape.month_end("2025-02"), "2025-02-28")
        self.assertEqual(scrape.month_end("2024-12"), "2024-12-31")
        self.assertEqual(scrape.month_end("2024-09"), "2024-09-30")

    def test_months_before_runs_up_to_but_not_including_the_end_month(self) -> None:
        self.assertEqual(scrape.months_before("2024-06"), [])
        self.assertEqual(scrape.months_before("2024-07"), ["2024-06"])
        months = scrape.months_before("2025-02")
        self.assertEqual(months[0], "2024-06")
        self.assertEqual(months[-1], "2025-01")
        self.assertEqual(months[6:8], ["2024-12", "2025-01"])
        self.assertEqual(len(months), 8)


class MonthRecordTests(unittest.TestCase):
    def test_response_is_kept_exactly_as_sent(self) -> None:
        payload = load("hh_2024-09.json")
        record = scrape.month_record("hh", payload, "2024-09-30", month_to_date=False)
        self.assertEqual(record["response"], payload)
        self.assertEqual(record["source_key"], "hh:2024-09")
        self.assertEqual(record["month"], "2024-09")
        self.assertEqual(record["request_date"], "2024-09-30")
        self.assertFalse(record["is_month_to_date"])
        self.assertTrue(record["has_snapshot"])
        self.assertEqual(record["as_of_utc"], scrape.snapshot_time(payload))
        self.assertTrue(record["as_of_utc"].endswith("+00:00"))

    def test_current_month_request_uses_the_month_the_source_reports(self) -> None:
        payload = load("bu_current.json")
        record = scrape.month_record("bu", payload, "", month_to_date=True)
        self.assertEqual(record["source_key"], f"bu:{payload['thisMonth']}")
        self.assertEqual(record["request_date"], "")
        self.assertTrue(record["is_month_to_date"])

    def test_answer_for_another_month_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "asked for 2024-10 but the source answered for 2024-09"):
            scrape.month_record("hh", load("hh_2024-09.json"), "2024-10-31", month_to_date=False)

    def test_unusable_month_fails(self) -> None:
        for value in (None, "", "2024-13", "Sept"):
            payload = {**load("hh_2024-09.json"), "thisMonth": value}
            with self.subTest(value), self.assertRaises(ValueError):
                scrape.month_record("hh", payload, "", month_to_date=True)

    def test_null_snapshot_month_is_kept_and_flagged(self) -> None:
        payload = {**load("hh_2024-09.json"), "snapshot": None}
        record = scrape.month_record("hh", payload, "2024-09-30", month_to_date=False)
        self.assertFalse(record["has_snapshot"])
        self.assertIsNone(record["as_of_utc"])
        self.assertIsNone(record["response"]["snapshot"])

    def test_snapshot_time_ignores_missing_or_odd_values(self) -> None:
        self.assertIsNone(scrape.snapshot_time({}))
        self.assertIsNone(scrape.snapshot_time({"snapshot": {"updatedAt": None}}))
        self.assertIsNone(scrape.snapshot_time({"snapshot": {"updatedAt": {"_seconds": True}}}))
        self.assertEqual(
            scrape.snapshot_time({"snapshot": {"updatedAt": {"_seconds": 0}}}), "1970-01-01T00:00:00+00:00"
        )


class PostMonthTests(unittest.TestCase):
    def response(self, body: bytes) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = body
        response.headers["Content-Type"] = "text/html"  # No charset: requests would guess Latin-1.
        return response

    def test_sends_the_date_body_and_reads_thai_text_from_the_body(self) -> None:
        session = MagicMock(spec=requests.Session)
        body = json.dumps({"thisMonth": "2024-09", "thisMonthName": "กันยายน 67"}, ensure_ascii=False).encode("utf-8")
        session.post.return_value = self.response(body)
        payload = scrape.post_month(session, HH_URL, "2024-09-30", 5)
        self.assertEqual(payload["thisMonthName"], "กันยายน 67")
        session.post.assert_called_once_with(HH_URL, json={"date": "2024-09-30"}, timeout=5)

    def test_non_object_response_fails(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.post.return_value = self.response(b"[]")
        with self.assertRaises(ValueError):
            scrape.post_month(session, HH_URL, "", 5)


class FetchOrderedTests(unittest.TestCase):
    def test_results_come_back_in_input_order(self) -> None:
        def fetch(number: int) -> int:
            time.sleep(0.01 * (5 - number))  # Later items finish first.
            return number * 10

        self.assertEqual(list(scrape.fetch_ordered(fetch, [0, 1, 2, 3, 4], 4)), [0, 10, 20, 30, 40])

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


class CollectMonthsTests(unittest.TestCase):
    def collect(self, post, months=None, workers=1, dataset="wallet_household"):
        with patch.object(scrape, "post_month", post):
            return scrape.collect_months(
                dataset, months=months, workers=workers, delay=0, timeout=1, user_agent="test", reporter=lambda _: None
            )

    def test_full_collection_is_current_month_plus_every_earlier_month(self) -> None:
        calls: list[str] = []
        post = fake_post_month(load("hh_current.json"), current_month="2024-09", calls=calls)
        records, warnings = self.collect(post)
        self.assertEqual(calls[0], "")
        self.assertEqual(calls[1:], ["2024-06-30", "2024-07-31", "2024-08-31"])
        self.assertEqual([record["month"] for record in records], ["2024-06", "2024-07", "2024-08", "2024-09"])
        self.assertEqual([record["is_month_to_date"] for record in records], [False, False, False, True])
        self.assertEqual(warnings, [])

    def test_end_month_comes_from_the_source_not_the_clock(self) -> None:
        calls: list[str] = []
        self.collect(fake_post_month(load("hh_current.json"), current_month="2024-07", calls=calls))
        self.assertEqual(calls, ["", "2024-06-30"])

    def test_workers_do_not_change_the_result(self) -> None:
        post = fake_post_month(load("hh_current.json"), current_month="2025-03")
        self.assertEqual(self.collect(post, workers=1), self.collect(post, workers=4))

    def test_empty_past_month_is_kept_with_a_warning(self) -> None:
        post = fake_post_month(load("hh_current.json"), current_month="2024-09", null_months={"2024-07"})
        records, warnings = self.collect(post)
        self.assertEqual(len(records), 4)
        self.assertEqual(warnings, ["hh:2024-07: the source answered without a snapshot"])

    def test_current_month_without_snapshot_fails(self) -> None:
        post = fake_post_month(load("hh_current.json"), current_month="2024-09", null_months={"2024-09"})
        with self.assertRaisesRegex(ValueError, "current month came back without a snapshot"):
            self.collect(post)

    def test_source_answering_the_wrong_month_fails(self) -> None:
        def post(_session, _url, request_date, _timeout):
            payload = copy.deepcopy(load("hh_current.json"))
            payload["thisMonth"] = "2024-09" if not request_date else "2024-05"
            return payload

        with self.assertRaisesRegex(ValueError, "answered for 2024-05"):
            self.collect(post)

    def test_smoke_months_are_fetched_without_the_current_month(self) -> None:
        calls: list[str] = []
        records, _ = self.collect(fake_post_month(load("hh_current.json"), calls=calls), months=["2024-09"])
        self.assertEqual(calls, ["2024-09-30"])
        self.assertEqual([record["source_key"] for record in records], ["hh:2024-09"])

    def test_business_dataset_uses_its_own_endpoint_code(self) -> None:
        urls: list[str] = []

        def post(_session, url, request_date, _timeout):
            urls.append(url)
            payload = copy.deepcopy(load("bu_current.json"))
            payload["thisMonth"] = request_date[:7] or "2024-07"
            return payload

        records, _ = self.collect(post, dataset="wallet_business")
        self.assertEqual({url for url in urls}, {f"{scrape.API_BASE}/bu"})
        self.assertEqual(records[0]["source_key"], "bu:2024-06")


class CompareWithPreviousTests(unittest.TestCase):
    DATASET = "wallet_household"

    def record(self, key: str = "hh:2024-06", value: int = 1) -> dict[str, object]:
        return {"record_type": "wallet_month", "source_key": key, "response": {"v": value}}

    def records(self, count: int, value: int = 1) -> list[dict[str, object]]:
        return [self.record(f"hh:{number:04d}", value) for number in range(1, count + 1)]

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "wallet_household.json"
        path.write_text(json.dumps({"data": records}), encoding="utf-8")
        return path

    def compare(self, records, path, allow_large_removal=False, minimum=1):
        return scrape.compare_with_previous(
            self.DATASET, records, path, allow_large_removal=allow_large_removal, minimum=minimum
        )

    def test_counts_new_changed_unchanged_and_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(4))
            current = self.records(2) + [self.record("hh:0003", 2), self.record("hh:9999")]
            counts = self.compare(current, path)
        self.assertEqual(counts, {"new": 1, "changed": 1, "unchanged": 2, "retired": 1})

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

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallet_household.json"
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

    def test_safety_floor_and_duplicate_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "below safety floor"):
            scrape.validate_records(self.DATASET, self.records(scrape.MINIMUM_RECORDS - 1))
        scrape.validate_records(self.DATASET, self.records(scrape.MINIMUM_RECORDS))
        with self.assertRaisesRegex(ValueError, "duplicate source_key"):
            scrape.validate_records(self.DATASET, [self.record("hh:2024-06"), self.record("hh:2024-06")], minimum=1)


class WriteTests(unittest.TestCase):
    def test_export_has_the_standard_envelope_and_month_ordered_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            records = [{"source_key": "hh:2024-07", "z": 1, "a": 2}, {"source_key": "hh:2024-06"}]
            scrape.write_export(Path(directory), "wallet_household", records, ["w"], "2026-10-14T00:00:00+00:00", "20261014T000000Z")
            written = json.loads((Path(directory) / "wallet_household.json").read_text(encoding="utf-8"))
        self.assertEqual(
            list(written),
            ["schema_version", "dataset", "source_urls", "scraped_at", "run_id", "record_count", "warnings", "data"],
        )
        self.assertEqual(written["source_urls"], [HH_URL])
        self.assertEqual([record["source_key"] for record in written["data"]], ["hh:2024-06", "hh:2024-07"])
        self.assertEqual(list(written["data"][1]), ["a", "source_key", "z"])

    def test_failed_write_leaves_old_file_and_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "wallet_household.json"
            target.write_text('{"keep": true}', encoding="utf-8")
            with self.assertRaises(TypeError):
                scrape.write_json(target, {"bad": object()})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": True})
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["wallet_household.json"])


class MainTests(unittest.TestCase):
    def run_main(self, directory: str, *extra: str) -> tuple[int, dict]:
        argv = ["scrape.py", "--quiet", "--delay", "0", "--output-dir", directory, *extra]
        output = io.StringIO()
        with (
            patch("sys.argv", argv),
            patch.object(scrape, "MINIMUM_RECORDS", 1),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = scrape.main()
        return code, json.loads(output.getvalue())

    def test_full_run_writes_both_files_and_a_repeat_run_changes_nothing(self) -> None:
        post = fake_post_month(load("hh_current.json"), current_month="2024-08")
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "post_month", post):
            code, first = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(
                sorted(path.name for path in Path(directory).iterdir()), ["wallet_business.json", "wallet_household.json"]
            )
            self.assertEqual(first["datasets"]["wallet_household"]["new"], 3)
            code, second = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(second["datasets"]["wallet_household"]["unchanged"], 3)
            envelope = json.loads((Path(directory) / "wallet_business.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["run_id"], second["run_id"])
            self.assertEqual(envelope["source_urls"], [f"{scrape.API_BASE}/bu"])

    def test_smoke_test_writes_nothing(self) -> None:
        post = fake_post_month(load("hh_current.json"))
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "post_month", post):
            code, summary = self.run_main(directory, "--month", "2024-09", "--dataset", "wallet_household")
            self.assertEqual(code, 0)
            self.assertEqual(summary["datasets"]["wallet_household"]["status"], "smoke_test")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failure_exits_1_and_keeps_the_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "wallet_household.json"
            existing.write_text('{"keep": true}', encoding="utf-8")
            with patch.object(scrape, "post_month", side_effect=requests.ConnectionError("down")):
                code, summary = self.run_main(directory, "--dataset", "wallet_household")
            self.assertEqual(code, 1)
            self.assertEqual(summary["datasets"]["wallet_household"]["status"], "failed")
            self.assertEqual(json.loads(existing.read_text(encoding="utf-8")), {"keep": True})


if __name__ == "__main__":
    unittest.main()
