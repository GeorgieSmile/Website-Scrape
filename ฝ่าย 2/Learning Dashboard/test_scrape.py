"""Offline checks for the Learning Dashboard scraper."""

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
ADDED = {"record_type", "source_key", "source_payload_hash"}


def load_response() -> dict:
    return json.loads((FIXTURES / "dashboard_response.json").read_text(encoding="utf-8"))


def changed(change) -> dict:
    payload = copy.deepcopy(load_response())
    change(payload)
    return payload


class CountRecordTests(unittest.TestCase):
    def test_every_table_row_becomes_a_record_without_the_header(self) -> None:
        payload = load_response()
        records, warnings = scrape.build_count_records(payload)
        self.assertEqual(warnings, [])
        for dimension in scrape.COUNT_DIMENSIONS:
            rows = sorted((r for r in records if r["dimension"] == dimension), key=lambda r: r["row_index"])
            header, *source_rows = payload[dimension]
            self.assertEqual([[r["label"], r["count"]] for r in rows], source_rows)
            self.assertTrue(all((r["dimension_label"], r["count_label"]) == tuple(header) for r in rows))
        self.assertNotIn("Province", {r["label"] for r in records})

    def test_record_shape_and_totals(self) -> None:
        records, _ = scrape.build_count_records(load_response())
        first = next(r for r in records if r["dimension"] == "entityTypes")
        self.assertEqual(first["source_key"], f"entityTypes:{first['label']}")
        self.assertEqual(first["record_type"], "dashboard_count")
        for dimension in scrape.COUNT_DIMENSIONS:
            self.assertEqual(sum(r["count"] for r in records if r["dimension"] == dimension), 375)

    def test_tables_that_do_not_add_up_are_a_warning(self) -> None:
        payload = changed(lambda p: p["categories"][1].__setitem__(1, 999))
        _, warnings = scrape.build_count_records(payload)
        self.assertEqual(len(warnings), 1)
        self.assertIn("do not add up to the same total", warnings[0])

    def test_broken_tables_fail(self) -> None:
        cases = {
            "missing table": changed(lambda p: p.pop("geography")),
            "header only": changed(lambda p: p.update(geography=p["geography"][:1])),
            "bad header": changed(lambda p: p["provinces"].__setitem__(0, ["only one"])),
            "row shape": changed(lambda p: p["provinces"].__setitem__(1, ["x"])),
            "blank label": changed(lambda p: p["provinces"][1].__setitem__(0, " ")),
            "text count": changed(lambda p: p["provinces"][1].__setitem__(1, "4")),
            "bool count": changed(lambda p: p["provinces"][1].__setitem__(1, True)),
            "duplicate label": changed(lambda p: p["provinces"].append(list(p["provinces"][1]))),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                scrape.build_count_records(payload)


class ImpactRecordTests(unittest.TestCase):
    def test_objects_are_labelled_by_position_and_values_are_unchanged(self) -> None:
        payload = load_response()
        records, warnings = scrape.build_impact_records(payload)
        self.assertEqual(warnings, [])
        self.assertEqual([r["region"] for r in records], list(scrape.REGION_ORDER))
        for record, source in zip(records, payload["geographyImpact"]):
            self.assertEqual({k: v for k, v in record.items() if k not in ADDED | {"region", "region_index"}}, source)
        self.assertEqual(records[0]["source_key"], "impact:ภาคเหนือ")
        self.assertEqual(records[5]["region_index"], 5)

    def test_page_order_matches_the_regions_the_api_lists_today(self) -> None:
        regions = [row[0] for row in load_response()["geography"][1:]]
        self.assertEqual(tuple(regions), scrape.REGION_ORDER)

    def test_different_region_order_labels_by_the_page_list_and_warns(self) -> None:
        payload = changed(lambda p: p["geography"].__setitem__(slice(1, 3), [p["geography"][2], p["geography"][1]]))
        records, warnings = scrape.build_impact_records(payload)
        self.assertEqual(records[0]["region"], "ภาคเหนือ")
        self.assertEqual(len(warnings), 1)
        self.assertIn("different order", warnings[0])

    def test_unknown_or_missing_regions_fail_instead_of_guessing(self) -> None:
        cases = {
            "renamed region": changed(lambda p: p["geography"][1].__setitem__(0, "ภาคอื่น")),
            "missing region": changed(lambda p: p["geography"].pop()),
            "no geography": changed(lambda p: p.pop("geography")),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                scrape.build_impact_records(payload)

    def test_broken_impact_objects_fail(self) -> None:
        cases = {
            "five objects": changed(lambda p: p["geographyImpact"].pop()),
            "not a list": changed(lambda p: p.update(geographyImpact={})),
            "not an object": changed(lambda p: p["geographyImpact"].__setitem__(0, 1)),
            "missing field": changed(lambda p: p["geographyImpact"][0].pop("localEmployeeAmount")),
            "text field": changed(lambda p: p["geographyImpact"][0].update(localEmployeeAmount="1")),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                scrape.build_impact_records(payload)


class TotalsRecordTests(unittest.TestCase):
    def test_summary_and_excluded_value_are_kept_as_sent(self) -> None:
        payload = load_response()
        records, warnings = scrape.build_totals_records(payload)
        self.assertEqual(warnings, [])
        by_key = {r["source_key"]: r for r in records}
        self.assertEqual({k: v for k, v in by_key["impact_summary"].items() if k not in ADDED}, payload["impactSummary"])
        self.assertEqual(
            {k: v for k, v in by_key["excluded_resource_expense"].items() if k not in ADDED}, payload["excludedResourceExpense"]
        )
        self.assertIn("totalEmplyeeAmount", by_key["impact_summary"])  # The source's spelling stays.

    def test_summary_that_differs_from_the_regions_is_a_warning(self) -> None:
        payload = changed(lambda p: p["impactSummary"].update(totalEmployeeExpense=1))
        _, warnings = scrape.build_totals_records(payload)
        self.assertEqual(len(warnings), 1)
        self.assertIn("totalEmployeeExpense", warnings[0])

    def test_missing_excluded_value_keeps_the_summary_with_a_warning(self) -> None:
        records, warnings = scrape.build_totals_records(changed(lambda p: p.pop("excludedResourceExpense")))
        self.assertEqual([r["source_key"] for r in records], ["impact_summary"])
        self.assertEqual(len(warnings), 1)

    def test_missing_summary_fails(self) -> None:
        for payload in (changed(lambda p: p.pop("impactSummary")), changed(lambda p: p.update(impactSummary={}))):
            with self.assertRaises(ValueError):
                scrape.build_totals_records(payload)


class FetchTests(unittest.TestCase):
    def response(self, body: bytes) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = body
        response.headers["Content-Type"] = "text/html"  # No charset: requests would guess Latin-1.
        return response

    def test_posts_without_a_body_and_reads_thai_text_from_the_body(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.post.return_value = self.response(json.dumps({"name": "ภาคเหนือ"}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(scrape.fetch_payload(session, 5)["name"], "ภาคเหนือ")
        session.post.assert_called_once_with(scrape.SOURCE_URL, timeout=5)

    def test_non_object_response_fails(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.post.return_value = self.response(b"[]")
        with self.assertRaises(ValueError):
            scrape.fetch_payload(session, 5)


class CompareWithPreviousTests(unittest.TestCase):
    DATASET = "dashboard_counts"

    def record(self, key: str = "1", value: int = 1) -> dict[str, object]:
        return {"record_type": "dashboard_count", "source_key": key, "count": value}

    def records(self, count: int, value: int = 1) -> list[dict[str, object]]:
        return [self.record(str(number), value) for number in range(1, count + 1)]

    def write_previous(self, directory: str, records: list[dict[str, object]]) -> Path:
        path = Path(directory) / "dashboard_counts.json"
        path.write_text(json.dumps({"data": records}), encoding="utf-8")
        return path

    def compare(self, records, path, allow_large_removal=False, dataset=None):
        return scrape.compare_with_previous(
            dataset or self.DATASET, records, path, allow_large_removal=allow_large_removal
        )

    def test_counts_new_changed_unchanged_and_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(60))
            current = self.records(55)
            current[2] = self.record("3", 2)
            current.append(self.record("900"))
            counts = self.compare(current, path)
        self.assertEqual(counts, {"new": 1, "changed": 1, "unchanged": 54, "retired": 5})

    def test_first_run_without_file_counts_everything_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            counts = self.compare(self.records(60), Path(directory) / "missing.json")
        self.assertEqual(counts, {"new": 60, "changed": 0, "unchanged": 0, "retired": 0})

    def test_removal_guard_trips_when_half_of_the_records_vanish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_previous(directory, self.records(100))
            self.assertEqual(self.compare(self.records(51), path)["retired"], 49)  # 49 < 50
            with self.assertRaisesRegex(ValueError, "refusing to retire 50 of 100"):
                self.compare(self.records(50), path)
            self.assertEqual(self.compare(self.records(50), path, allow_large_removal=True)["retired"], 50)

    def test_losing_one_of_the_two_totals_trips_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard_totals.json"
            path.write_text(json.dumps({"data": [self.record("impact_summary"), self.record("excluded_resource_expense")]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to retire 1 of 2"):
                self.compare([self.record("impact_summary")], path, dataset="dashboard_totals")

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard_counts.json"
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
                    self.compare(self.records(60), path, allow_large_removal=True)

    def test_safety_floor_and_duplicate_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "below safety floor"):
            scrape.validate_records("dashboard_counts", self.records(49))
        scrape.validate_records("dashboard_counts", self.records(50))
        with self.assertRaisesRegex(ValueError, "duplicate source_key"):
            scrape.validate_records("dashboard_totals", [self.record("1"), self.record("1")])


class WriteTests(unittest.TestCase):
    def test_export_has_the_standard_envelope_and_sorted_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            records = [{"source_key": "b", "z": 1, "a": 2}, {"source_key": "a"}]
            scrape.write_export(Path(directory), "dashboard_counts", records, ["w"], "2026-10-14T00:00:00+00:00", "20261014T000000Z")
            written = json.loads((Path(directory) / "dashboard_counts.json").read_text(encoding="utf-8"))
        self.assertEqual(
            list(written),
            ["schema_version", "dataset", "source_urls", "scraped_at", "run_id", "record_count", "warnings", "data"],
        )
        self.assertEqual(written["source_urls"], [scrape.SOURCE_URL])
        self.assertEqual([record["source_key"] for record in written["data"]], ["a", "b"])
        self.assertEqual(list(written["data"][1]), ["a", "source_key", "z"])

    def test_failed_write_leaves_old_file_and_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "dashboard_counts.json"
            target.write_text('{"keep": true}', encoding="utf-8")
            with self.assertRaises(TypeError):
                scrape.write_json(target, {"bad": object()})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": True})
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["dashboard_counts.json"])


class MainTests(unittest.TestCase):
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

    def test_full_run_writes_all_files_and_a_repeat_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "fetch_payload", return_value=load_response()):
            code, first = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(
                sorted(path.name for path in Path(directory).iterdir()),
                ["dashboard_counts.json", "dashboard_impact.json", "dashboard_totals.json"],
            )
            self.assertEqual(first["datasets"]["dashboard_counts"]["new"], 82)
            code, second = self.run_main(directory)
            self.assertEqual(code, 0)
            self.assertEqual(second["datasets"]["dashboard_counts"]["unchanged"], 82)
            self.assertEqual(second["datasets"]["dashboard_impact"]["unchanged"], 6)
            self.assertEqual(second["datasets"]["dashboard_totals"]["unchanged"], 2)
            envelope = json.loads((Path(directory) / "dashboard_totals.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["run_id"], second["run_id"])

    def test_fetch_failure_exits_1_and_keeps_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "dashboard_counts.json"
            existing.write_text('{"keep": true}', encoding="utf-8")
            with patch.object(scrape, "fetch_payload", side_effect=requests.ConnectionError("down")):
                code, summary = self.run_main(directory, "--dataset", "dashboard_counts")
            self.assertEqual(code, 1)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(json.loads(existing.read_text(encoding="utf-8")), {"keep": True})

    def test_one_broken_table_does_not_block_the_other_datasets(self) -> None:
        payload = changed(lambda p: p["geographyImpact"].pop())
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "fetch_payload", return_value=payload):
            code, summary = self.run_main(directory)
            self.assertEqual(code, 1)
            self.assertEqual(summary["datasets"]["dashboard_counts"]["status"], "success")
            self.assertEqual(summary["datasets"]["dashboard_impact"]["status"], "failed")
            self.assertEqual(summary["datasets"]["dashboard_totals"]["status"], "success")
            self.assertFalse((Path(directory) / "dashboard_impact.json").exists())


if __name__ == "__main__":
    unittest.main()
