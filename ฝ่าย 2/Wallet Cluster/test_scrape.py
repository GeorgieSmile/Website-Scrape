"""Offline checks for the Wallet Cluster scraper."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import requests
import scrape

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ClusterRecordTests(unittest.TestCase):
    def test_household_clusters_are_wrapped_without_changing_figures(self) -> None:
        payload = load("cluster_hh.json")
        records = scrape.build_cluster_records("hh", payload)
        self.assertEqual(len(records), 7)
        self.assertEqual([record["snapshot"] for record in records], payload["snapshotClusters"])
        first = records[0]
        self.assertEqual(first["source_key"], "hh:0")
        self.assertEqual(first["month"], payload["thisMonth"])
        self.assertEqual(first["category"], payload["clustersName"][0])
        self.assertEqual(first["thisMonthName"], payload["thisMonthName"])
        self.assertEqual(first["yesterdayName"], payload["yesterdayName"])
        self.assertIsNone(first["cluster_id"])  # The household endpoint sends no cluster ids.
        self.assertTrue(first["as_of_utc"].endswith("+00:00"))

    def test_business_clusters_carry_the_source_cluster_ids(self) -> None:
        records = scrape.build_cluster_records("bu", load("cluster_bu.json"))
        self.assertEqual([record["cluster_id"] for record in records], [f"cat{number}" for number in range(7)])
        self.assertEqual([record["source_key"] for record in records], [f"bu:{number}" for number in range(7)])

    def test_business_counts_come_from_the_categories_table(self) -> None:
        payload = load("cluster_hh.json")
        records = scrape.build_cluster_records("hh", payload)
        expected = {name: count for name, count in payload["categories"][1:]}
        self.assertEqual({record["category"]: record["business_count"] for record in records}, expected)
        self.assertEqual(sum(record["business_count"] for record in records), 144)

    def test_hash_covers_the_figures_the_count_and_the_month(self) -> None:
        payload = load("cluster_hh.json")
        base = scrape.build_cluster_records("hh", payload)[0]["source_payload_hash"]
        changed = copy.deepcopy(payload)
        changed["snapshotClusters"][0]["totalCash"] += 1
        self.assertNotEqual(scrape.build_cluster_records("hh", changed)[0]["source_payload_hash"], base)
        changed = copy.deepcopy(payload)
        changed["categories"][1][1] += 1
        self.assertNotEqual(scrape.build_cluster_records("hh", changed)[0]["source_payload_hash"], base)
        changed = copy.deepcopy(payload)
        changed["thisMonth"] = "2030-01"
        self.assertNotEqual(scrape.build_cluster_records("hh", changed)[0]["source_payload_hash"], base)

    def test_disagreeing_cluster_lists_fail(self) -> None:
        def broken(code: str, fixture: str, change) -> tuple[str, dict]:
            payload = copy.deepcopy(load(fixture))
            change(payload)
            return code, payload

        cases = {
            "bad month": broken("hh", "cluster_hh.json", lambda p: p.update(thisMonth="soon")),
            "no clusters": broken("hh", "cluster_hh.json", lambda p: p.update(snapshotClusters=[])),
            "names missing": broken("hh", "cluster_hh.json", lambda p: p.pop("clustersName")),
            "blank name": broken("hh", "cluster_hh.json", lambda p: p["clustersName"].__setitem__(0, " ")),
            "name count": broken("hh", "cluster_hh.json", lambda p: p["clustersName"].pop()),
            "duplicate names": broken("hh", "cluster_hh.json", lambda p: p["clustersName"].__setitem__(1, p["clustersName"][0])),
            "categories rows": broken("hh", "cluster_hh.json", lambda p: p["categories"].pop()),
            "categories row shape": broken("hh", "cluster_hh.json", lambda p: p["categories"].__setitem__(1, ["x"])),
            "categories names": broken("hh", "cluster_hh.json", lambda p: p["categories"][1].__setitem__(0, "other")),
            "cluster name order": broken(
                "hh", "cluster_hh.json", lambda p: p["snapshotClusters"].reverse()
            ),
            "cluster not an object": broken("hh", "cluster_hh.json", lambda p: p["snapshotClusters"].__setitem__(0, 1)),
            "bu ids names": broken("bu", "cluster_bu.json", lambda p: p["clusters"][0].update(name="other")),
            "bu ids not a list": broken("bu", "cluster_bu.json", lambda p: p.update(clusters={})),
        }
        for name, (code, payload) in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                scrape.build_cluster_records(code, payload)

    def test_smallest_cluster_count_is_kept_as_sent(self) -> None:
        records = scrape.build_cluster_records("hh", load("cluster_hh.json"))
        self.assertEqual(min(record["business_count"] for record in records), 9)


class PostSnapshotTests(unittest.TestCase):
    def response(self, body: bytes) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = body
        response.headers["Content-Type"] = "text/html"  # No charset: requests would guess Latin-1.
        return response

    def test_posts_without_a_body_and_reads_thai_text_from_the_body(self) -> None:
        session = MagicMock(spec=requests.Session)
        body = json.dumps({"thisMonthName": "ตุลาคม 69"}, ensure_ascii=False).encode("utf-8")
        session.post.return_value = self.response(body)
        url = f"{scrape.API_BASE}/hh"
        self.assertEqual(scrape.post_snapshot(session, url, 5)["thisMonthName"], "ตุลาคม 69")
        session.post.assert_called_once_with(url, timeout=5)

    def test_non_object_response_fails(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.post.return_value = self.response(b"[]")
        with self.assertRaises(ValueError):
            scrape.post_snapshot(session, scrape.API_BASE, 5)


class CompareWithPreviousTests(unittest.TestCase):
    DATASET = "wallet_cluster_household"

    def records(self, count: int = 7, value: int = 1) -> list[dict[str, object]]:
        return [
            {"record_type": "wallet_cluster_month_to_date", "source_key": f"hh:{number}", "snapshot": {"v": value}}
            for number in range(count)
        ]

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "wallet_cluster_household.json"
        path.write_text(json.dumps({"data": records}), encoding="utf-8")
        return path

    def compare(self, records, path, allow_large_removal=False):
        return scrape.compare_with_previous(self.DATASET, records, path, allow_large_removal=allow_large_removal)

    def test_a_new_day_shows_as_changed_clusters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records())
            counts = self.compare(self.records(value=2), path)
        self.assertEqual(counts, {"new": 0, "changed": 7, "unchanged": 0, "retired": 0})

    def test_first_run_without_file_counts_everything_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            counts = self.compare(self.records(), Path(directory) / "missing.json")
        self.assertEqual(counts, {"new": 7, "changed": 0, "unchanged": 0, "retired": 0})

    def test_removal_guard_trips_when_half_the_clusters_vanish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(7))
            self.assertEqual(self.compare(self.records(4), path)["retired"], 3)  # 3 < ceil(3.5)
            with self.assertRaisesRegex(ValueError, "refusing to retire 4 of 7"):
                self.compare(self.records(3), path)
            self.assertEqual(self.compare(self.records(3), path, allow_large_removal=True)["retired"], 4)

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallet_cluster_household.json"
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
                    self.compare(self.records(), path, allow_large_removal=True)

    def test_empty_and_duplicate_records_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "below safety floor"):
            scrape.validate_records(self.DATASET, [])
        with self.assertRaisesRegex(ValueError, "duplicate source_key"):
            scrape.validate_records(self.DATASET, self.records(1) * 2)


class WriteTests(unittest.TestCase):
    def test_export_has_the_standard_envelope_and_cluster_ordered_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            records = [
                {"source_key": "hh:1", "cluster_index": 1, "z": 1, "a": 2},
                {"source_key": "hh:0", "cluster_index": 0},
                {"source_key": "hh:10", "cluster_index": 10},
            ]
            scrape.write_export(
                Path(directory), "wallet_cluster_household", records, ["w"], "2026-10-14T00:00:00+00:00", "20261014T000000Z"
            )
            written = json.loads((Path(directory) / "wallet_cluster_household.json").read_text(encoding="utf-8"))
        self.assertEqual(
            list(written),
            ["schema_version", "dataset", "source_urls", "scraped_at", "run_id", "record_count", "warnings", "data"],
        )
        self.assertEqual(written["source_urls"], [f"{scrape.API_BASE}/hh"])
        self.assertEqual([record["cluster_index"] for record in written["data"]], [0, 1, 10])

    def test_failed_write_leaves_old_file_and_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "wallet_cluster_household.json"
            target.write_text('{"keep": true}', encoding="utf-8")
            with self.assertRaises(TypeError):
                scrape.write_json(target, {"bad": object()})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": True})
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["wallet_cluster_household.json"])


class MainTests(unittest.TestCase):
    FIXTURE_BY_CODE: ClassVar[dict[str, str]] = {"hh": "cluster_hh.json", "bu": "cluster_bu.json"}

    def fake_post(self, _session, url, _timeout):
        return copy.deepcopy(load(self.FIXTURE_BY_CODE[url.rsplit("/", 1)[1]]))

    def run_main(self, directory: str, *extra: str) -> tuple[int, dict]:
        argv = ["scrape.py", "--quiet", "--output-dir", directory, *extra]
        output = io.StringIO()
        with (
            patch("sys.argv", argv),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = scrape.main()
        return code, json.loads(output.getvalue())

    def test_full_run_writes_both_files_and_a_repeat_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "post_snapshot", self.fake_post):
            code, first = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(
                sorted(path.name for path in Path(directory).iterdir()),
                ["wallet_cluster_business.json", "wallet_cluster_household.json"],
            )
            self.assertEqual(first["datasets"]["wallet_cluster_business"]["new"], 7)
            code, second = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(second["datasets"]["wallet_cluster_household"]["unchanged"], 7)
            envelope = json.loads((Path(directory) / "wallet_cluster_business.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["run_id"], second["run_id"])
            self.assertEqual(envelope["source_urls"], [f"{scrape.API_BASE}/bu"])
            self.assertEqual(envelope["record_count"], 7)

    def test_one_failing_dataset_does_not_block_the_other(self) -> None:
        def post(session, url, timeout):
            if url.endswith("/hh"):
                raise requests.ConnectionError("down")
            return self.fake_post(session, url, timeout)

        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "post_snapshot", post):
            code, summary = self.run_main(directory)
            self.assertEqual(code, 1)
            self.assertEqual(summary["datasets"]["wallet_cluster_household"]["status"], "failed")
            self.assertEqual(summary["datasets"]["wallet_cluster_business"]["status"], "success")
            self.assertFalse((Path(directory) / "wallet_cluster_household.json").exists())


if __name__ == "__main__":
    unittest.main()
