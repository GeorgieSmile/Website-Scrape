"""Offline checks for the Learning Area Based scraper."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import scrape

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SMALL_FLOOR = {"area_based": 1, "area_based_stats": 1}


def load_response() -> dict:
    return json.loads((FIXTURES / "area_based_response.json").read_text(encoding="utf-8"))


class ListingRecordTests(unittest.TestCase):
    def test_rows_are_wrapped_without_changing_any_value(self) -> None:
        payload = load_response()
        records, _ = scrape.build_listing_records(payload)
        self.assertEqual(len(records), len(payload["data"]))
        for record, row in zip(records, payload["data"]):
            added = {"record_type", "source_key", "source_payload_hash"}
            self.assertEqual({key: value for key, value in record.items() if key not in added}, row)
            self.assertEqual(record["source_key"], row["id"])
            self.assertEqual(record["record_type"], "area_based_listing")

    def test_blank_and_placeholder_values_stay_as_the_source_sent_them(self) -> None:
        records, _ = scrape.build_listing_records(load_response())
        self.assertTrue(any(record["businessName"] == "[object Object]" for record in records))
        self.assertTrue(any(record["province"] == "" for record in records))
        self.assertTrue(any(record["district"] == "" for record in records))
        self.assertTrue(any(record["subDistrict"] == "" for record in records))

    def test_total_mismatch_is_a_warning_not_a_failure(self) -> None:
        payload = load_response()
        _, warnings = scrape.build_listing_records(payload)
        self.assertEqual(len(warnings), 1)
        self.assertIn("stats.totalRecords is 1002", warnings[0])
        payload["stats"]["totalRecords"] = len(payload["data"])
        self.assertEqual(scrape.build_listing_records(payload)[1], [])

    def test_new_source_fields_are_reported_once(self) -> None:
        payload = load_response()
        payload["stats"]["totalRecords"] = len(payload["data"])
        for row in payload["data"][:3]:
            row["extraField"] = 1
        _, warnings = scrape.build_listing_records(payload)
        self.assertEqual(warnings, ["rows carry fields not seen before: extraField"])

    def test_broken_responses_fail(self) -> None:
        def broken(change) -> dict:
            payload = copy.deepcopy(load_response())
            change(payload)
            return payload

        cases = {
            "status": broken(lambda p: p.update(status=500)),
            "data not a list": broken(lambda p: p.update(data={})),
            "row not an object": broken(lambda p: p["data"].append("x")),
            "missing id": broken(lambda p: p["data"][0].pop("id")),
            "blank id": broken(lambda p: p["data"][0].update(id=" ")),
            "duplicate id": broken(lambda p: p["data"].append(dict(p["data"][0]))),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                scrape.build_listing_records(payload)

    def test_stats_record_keeps_stats_and_filters_verbatim(self) -> None:
        payload = load_response()
        (record,) = scrape.build_stats_records(payload)
        self.assertEqual(record["source_key"], "area_based_stats")
        self.assertEqual(record["stats"], payload["stats"])
        self.assertEqual(record["filters"], payload["filters"])

    def test_stats_record_needs_stats_and_filters(self) -> None:
        for field in ("stats", "filters"):
            payload = load_response()
            del payload[field]
            with self.subTest(field), self.assertRaises(ValueError):
                scrape.build_stats_records(payload)


class FetchTests(unittest.TestCase):
    def test_json_is_read_from_the_body_as_utf8(self) -> None:
        body = json.dumps({"status": 200, "data": [], "name": "ภาคเหนือ"}, ensure_ascii=False).encode("utf-8")
        response = requests.Response()
        response.status_code = 200
        response._content = body
        response.headers["Content-Type"] = "text/html"  # No charset: requests would guess Latin-1.

        session = MagicMock(spec=requests.Session)
        session.get.return_value = response
        self.assertEqual(scrape.fetch_payload(session, 5)["name"], "ภาคเหนือ")

    def test_non_object_response_fails(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]"

        session = MagicMock(spec=requests.Session)
        session.get.return_value = response
        with self.assertRaises(ValueError):
            scrape.fetch_payload(session, 5)


class CompareWithPreviousTests(unittest.TestCase):
    DATASET = "area_based"

    def record(self, key: str = "1", value: str = "A") -> dict[str, object]:
        return {"record_type": "area_based_listing", "source_key": key, "businessName": value}

    def records(self, count: int, value: str = "A") -> list[dict[str, object]]:
        return [self.record(str(number), value) for number in range(1, count + 1)]

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "area_based.json"
        path.write_text(json.dumps({"data": records}), encoding="utf-8")
        return path

    def compare(self, records, path, allow_large_removal=False):
        with patch.dict(scrape.MINIMUM_RECORDS, SMALL_FLOOR):
            return scrape.compare_with_previous(self.DATASET, records, path, allow_large_removal=allow_large_removal)

    def test_counts_new_changed_unchanged_and_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(4))
            current = self.records(2) + [self.record("3", "changed"), self.record("9")]
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
            path = Path(directory) / "area_based.json"
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
            scrape.validate_records("area_based", self.records(499))
        scrape.validate_records("area_based", self.records(500))
        with self.assertRaisesRegex(ValueError, "duplicate source_key"):
            scrape.validate_records("area_based_stats", [self.record("1"), self.record("1")])


class WriteTests(unittest.TestCase):
    def test_export_has_the_standard_envelope_and_sorted_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            records = [{"source_key": "b", "z": 1, "a": 2}, {"source_key": "a"}]
            scrape.write_export(Path(directory), "area_based", records, ["w"], "2026-10-14T00:00:00+00:00", "20261014T000000Z")
            written = json.loads((Path(directory) / "area_based.json").read_text(encoding="utf-8"))
        self.assertEqual(
            list(written),
            ["schema_version", "dataset", "source_urls", "scraped_at", "run_id", "record_count", "warnings", "data"],
        )
        self.assertEqual(written["source_urls"], [scrape.SOURCE_URL])
        self.assertEqual([record["source_key"] for record in written["data"]], ["a", "b"])
        self.assertEqual(list(written["data"][1]), ["a", "source_key", "z"])

    def test_failed_write_leaves_old_file_and_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "area_based.json"
            target.write_text('{"keep": true}', encoding="utf-8")
            with self.assertRaises(TypeError):
                scrape.write_json(target, {"bad": object()})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": True})
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["area_based.json"])


class MainTests(unittest.TestCase):
    def run_main(self, directory: str, *extra: str) -> tuple[int, dict]:
        argv = ["scrape.py", "--quiet", "--output-dir", directory, *extra]
        output = io.StringIO()
        with (
            patch("sys.argv", argv),
            patch.dict(scrape.MINIMUM_RECORDS, SMALL_FLOOR),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = scrape.main()
        return code, json.loads(output.getvalue())

    def test_full_run_writes_both_files_and_a_repeat_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "fetch_payload", return_value=load_response()):
            code, first = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["area_based.json", "area_based_stats.json"])
            self.assertEqual(first["datasets"]["area_based"]["new"], 17)
            code, second = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(second["datasets"]["area_based"]["unchanged"], 17)
            self.assertEqual(second["datasets"]["area_based_stats"]["unchanged"], 1)
            envelope = json.loads((Path(directory) / "area_based.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["run_id"], second["run_id"])

    def test_fetch_failure_exits_1_and_keeps_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "area_based.json"
            existing.write_text('{"keep": true}', encoding="utf-8")
            with patch.object(scrape, "fetch_payload", side_effect=requests.ConnectionError("down")):
                code, summary = self.run_main(directory, "--dataset", "area_based")
            self.assertEqual(code, 1)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(json.loads(existing.read_text(encoding="utf-8")), {"keep": True})

    def test_bad_dataset_does_not_block_the_other(self) -> None:
        payload = load_response()
        del payload["filters"]
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "fetch_payload", return_value=payload):
            code, summary = self.run_main(directory)
            self.assertEqual(code, 1)
            self.assertEqual(summary["datasets"]["area_based"]["status"], "success")
            self.assertEqual(summary["datasets"]["area_based_stats"]["status"], "failed")
            self.assertFalse((Path(directory) / "area_based_stats.json").exists())


if __name__ == "__main__":
    unittest.main()
