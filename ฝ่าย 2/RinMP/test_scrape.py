"""Offline tests for the RinMP AppTech scraper. No network access."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scrape import (
    PAGE_APP_TECH,
    PAGE_OWNER,
    ScrapeError,
    build_owner_display_name,
    clean_text,
    collect_details,
    compare_with_previous,
    derive_record_warnings,
    fetch_ordered,
    html_to_text,
    keep_previous_for_failed,
    load_hq_provinces,
    normalize_detail,
    optional_text,
    owner_key_for,
    parse_addresses,
    parse_owner_profiles,
    parse_trl,
    reconcile_statistics,
    resolve_province,
    semantic_hash,
    strip_pii,
)


class CleaningTests(unittest.TestCase):
    def test_clean_text_normalizes_and_collapses(self) -> None:
        self.assertEqual(clean_text("\u00a0  \ufeffสวัสดี  \u200b  ครับ "), "สวัสดี ครับ")

    def test_optional_text_placeholders_become_none(self) -> None:
        self.assertIsNone(optional_text("-"))
        self.assertIsNone(optional_text("ไม่มี"))
        self.assertIsNone(optional_text("-ไม่มี-"))
        self.assertIsNone(optional_text("   "))
        self.assertEqual(optional_text("TRL_8"), "TRL_8")

    def test_html_to_text_strips_tags_keeps_lines(self) -> None:
        text = html_to_text("<p>บรรทัดหนึ่ง</p><ol><li>ข้อหนึ่ง</li><li>ข้อสอง</li></ol>")
        self.assertEqual(text, "บรรทัดหนึ่ง\nข้อหนึ่ง\nข้อสอง")

    def test_semantic_hash_ignores_volatile_fields(self) -> None:
        first = {"id": 1, "title": "a", "currentAdminQuantity": 3}
        second = {"id": 1, "title": "a", "currentAdminQuantity": 9, "run_id": "x"}
        self.assertEqual(semantic_hash(first), semantic_hash(second))
        self.assertNotEqual(semantic_hash(first), semantic_hash({"id": 1, "title": "b"}))


class PiiStripTests(unittest.TestCase):
    def test_contact_keys_removed_recursively(self) -> None:
        payload = {
            "name": "a",
            "ownerContact": {"email": "x@y.z", "phone": "099", "name": "b"},
            "gallery": [{"email": "e@f.g", "size": 1}],
        }
        cleaned = strip_pii(payload)
        self.assertNotIn("email", json.dumps(cleaned))
        self.assertNotIn("phone", json.dumps(cleaned))
        self.assertEqual(cleaned["ownerContact"]["name"], "b")
        self.assertEqual(cleaned["gallery"][0]["size"], 1)

    def test_intellectual_property_survives(self) -> None:
        payload = {"intellectualPropertyDetail": {"category": "PATENT"}}
        self.assertEqual(strip_pii(payload)["intellectualPropertyDetail"]["category"], "PATENT")

    def test_email_in_free_text_values_is_redacted(self) -> None:
        payload = {
            "ownerContact": {"detail": "โทร sutthida1@rmutl.ac.th", "name": "b"},
            "description_sections": [{"label": "l", "text": "ติดต่อ a.b+x@ac.th ได้"}],
        }
        cleaned = strip_pii(payload)
        self.assertNotIn("@", json.dumps(cleaned, ensure_ascii=False))
        self.assertIn("[redacted-contact]", cleaned["ownerContact"]["detail"])
        self.assertIn("[redacted-contact]", cleaned["description_sections"][0]["text"])


class ParsingTests(unittest.TestCase):
    def test_parse_trl_valid(self) -> None:
        self.assertEqual(parse_trl("TRL_8"), (8, "TRL_8"))
        self.assertEqual(parse_trl("trl 7"), (7, "trl 7"))

    def test_parse_trl_invalid(self) -> None:
        level, raw = parse_trl("advanced")
        self.assertIsNone(level)
        self.assertEqual(raw, "advanced")
        level, _ = parse_trl(None)
        self.assertIsNone(level)

    def test_parse_addresses_valid(self) -> None:
        raw = json.dumps(
            [{"value": {"name": None, "subDistrict": "ปิยามุมัง", "district": "ยะหริ่ง",
                         "province": "ปัตตานี", "otherInfo": None}}]
        )
        addresses = parse_addresses(raw, warnings := [])
        self.assertEqual(addresses[0]["province"], "ปัตตานี")
        self.assertEqual(warnings, [])

    def test_parse_addresses_invalid_json_warns(self) -> None:
        warnings: list[str] = []
        self.assertEqual(parse_addresses("not-json{", warnings), [])
        self.assertTrue(warnings)

    def test_resolve_province_prefers_address_then_hq_then_none(self) -> None:
        hq = {"มทร.ศรีวิชัย": "สงขลา"}
        with_addr = resolve_province([{"province": "ปัตตานี"}], "มทร.ศรีวิชัย", hq, [])
        self.assertEqual(with_addr, "ปัตตานี")
        warnings: list[str] = []
        fallback = resolve_province([{"province": None}], "มทร.ศรีวิชัย", hq, warnings)
        self.assertEqual(fallback, "สงขลา")
        unknown = resolve_province([], "ไม่รู้จัก", hq, warnings)
        self.assertIsNone(unknown)
        self.assertTrue(warnings)

    def test_owner_display_name_joins_rank(self) -> None:
        name = build_owner_display_name(
            {"academicRank": "รศ.", "name": "วรพงค์", "lastname": "บุญช่วยแทน"}
        )
        self.assertEqual(name, "รศ. วรพงค์ บุญช่วยแทน")


class NormalizeDetailTests(unittest.TestCase):
    LISTING = {
        "id": 696,
        "displayText": "เครื่องแยกขุย",
        "institute": {"id": 1, "displayText": "มทร.ศรีวิชัย"},
        "subCategory": {
            "displayText": "อาหาร",
            "category": {"displayText": "อาหารและเทคโนโลยีการแปรรูป"},
        },
    }

    DETAIL = {
        "id": 696,
        "displayText": "เครื่องแยกขุย",
        "atlLevel": "A",
        "trlLevel": "TRL_8",
        "status": "ON_SHELF",
        "isRecommend": False,
        "createDateTime": "2026-08-16T17:01:40.887393",
        "lastUpdateDateTime": "2026-08-16T17:02:00.477716",
        "detail": {
            "appTechDescription": "<p>คำอธิบาย</p>",
            "functionalDetail": "-",
            "targetUserList": "SMALL_COMMUNITY|SMALL_ENTREPRENEURS",
            "userAddressListString": json.dumps(
                [{"value": {"subDistrict": "ปิยามุมัง", "district": "ยะหริ่ง",
                             "province": "ปัตตานี"}}]
            ),
        },
        "commercialDetail": {"budget": 0, "costPerUnit": 100000.0, "lifeTimeYearCount": 0},
        "intellectualPropertyDetail": {"category": "PATENT", "intellectualPropertyNumber": "-"},
        "tagList": [{"searchTag": {"displayText": "มะพร้าว"}}],
        "profilePicture": {"id": "pic-id"},
        "galleryImageList": [{"id": "g1"}, {"id": None}],
        "matrixScoresMissing": True,
    }

    def test_normalize_detail_builds_record_without_pii(self) -> None:
        record, warnings = normalize_detail(self.DETAIL, self.LISTING, load_hq_provinces())
        self.assertEqual(record["id"], 696)
        self.assertEqual(record["trl"], {"level": 8, "raw": "TRL_8"})
        self.assertEqual(record["target_segments"], ["SMALL_COMMUNITY", "SMALL_ENTREPRENEURS"])
        self.assertEqual(record["addresses"][0]["province"], "ปัตตานี")
        self.assertEqual(record["province_resolved"], "ปัตตานี")
        self.assertEqual(record["ip"], {"category": "PATENT", "number_raw": None})
        self.assertEqual(record["commercial"]["cost_per_unit_thb"], 100000.0)
        self.assertIsNone(record["commercial"]["budget_thb"])
        self.assertEqual(record["tags"], ["มะพร้าว"])
        self.assertEqual(record["media"]["gallery_file_ids"], ["g1"])
        # functionalDetail is a placeholder -> section dropped; description kept
        labels = [section["label"] for section in record["description_sections"]]
        self.assertEqual(labels, ["appTechDescription"])
        self.assertEqual(record["description_sections"][0]["text"], "คำอธิบาย")
        self.assertNotIn("email", json.dumps(record))

    def test_normalize_detail_missing_title_raises(self) -> None:
        with self.assertRaises(ScrapeError):
            normalize_detail({"id": 1}, {"id": 1}, {})


class RecordWarningTests(unittest.TestCase):
    def test_derive_record_warnings_reports_gaps(self) -> None:
        records = [
            {"id": 1, "description_sections": [], "province_resolved": None},
            {"id": 2, "description_sections": [{"label": "x", "text": "y"}],
             "province_resolved": "ปัตตานี"},
            {"id": 3, "description_sections": [], "province_resolved": None},
        ]
        warnings = derive_record_warnings(records)
        self.assertEqual(len(warnings), 2)
        self.assertIn("2 records have none of the expected description sections", warnings[0])
        self.assertIn("ids: 1, 3", warnings[0])
        self.assertIn("2 records have no resolvable province", warnings[1])

    def test_derive_record_warnings_silent_when_clean(self) -> None:
        records = [{"id": 1, "description_sections": [{"label": "x", "text": "y"}],
                    "province_resolved": "สงขลา"}]
        self.assertEqual(derive_record_warnings(records), [])


class OwnerCollectionTests(unittest.TestCase):
    def test_owners_dedupe_by_fullname_and_map_titles(self) -> None:
        rows = [
            {"fullname": "คน เดียวกัน", "institute": "มทร.อีสาน", "appTechList": ["ตู้อบใบไม้"]},
            {"fullname": "คน เดียวกัน ", "institute": "มทร.อีสาน", "appTechList": ["ของใหม่"]},
            {"fullname": None, "institute": "x", "appTechList": []},
        ]
        title_to_ids = {"ตู้อบใบไม้": {123}, "ของใหม่": {124}}
        warnings: list[str] = []
        owners = parse_owner_profiles(rows, title_to_ids, warnings)
        self.assertEqual(len(owners), 1)
        self.assertEqual(sorted(owners[0]["app_tech_ids"]), [123, 124])
        self.assertTrue(owners[0]["owner_key"].startswith("OWNER-"))
        self.assertTrue(any("duplicate fullname" in warning for warning in warnings))

    def test_owner_key_is_stable(self) -> None:
        self.assertEqual(owner_key_for("a b"), owner_key_for("a b"))
        self.assertNotEqual(owner_key_for("a b"), owner_key_for("b a"))


class ReconciliationTests(unittest.TestCase):
    def test_reconcile_passes_on_matching_counts(self) -> None:
        records = [
            {"institute": {"name": "A"}},
            {"institute": {"name": "B"}},
            {"institute": {"name": "A"}},
        ]
        stats = {
            "total_app_tech_count": 3,
            "institute_graph": [{"institute": "A", "count": 2}, {"institute": "B", "count": 1}],
        }
        result = reconcile_statistics(stats, records)
        self.assertTrue(result["counts_match"])

    def test_reconcile_fails_on_mismatch(self) -> None:
        stats = {"total_app_tech_count": 5, "institute_graph": []}
        with self.assertRaises(ScrapeError):
            reconcile_statistics(stats, [{"institute": {"name": "A"}}])


class FetchOrderedTests(unittest.TestCase):
    def test_results_come_back_in_input_order(self) -> None:
        def fetch(number: int) -> int:
            time.sleep(0.01 * (5 - number))  # Later items finish first.
            return number * 10

        self.assertEqual(list(fetch_ordered(fetch, [0, 1, 2, 3, 4], 4)), [0, 10, 20, 30, 40])

    def test_first_error_propagates_and_cancels_pending_work(self) -> None:
        calls: list[int] = []

        def fetch(number: int) -> int:
            calls.append(number)
            time.sleep(0.02)
            if number == 1:
                raise RuntimeError("boom")
            return number

        with self.assertRaisesRegex(RuntimeError, "boom"):
            list(fetch_ordered(fetch, list(range(50)), 1))
        self.assertLess(len(calls), 50)


class CollectDetailsTests(unittest.TestCase):
    def test_failed_details_are_counted_and_warned_in_id_order(self) -> None:
        listings = {number: {"id": number, "displayText": f"t{number}"} for number in range(1, 7)}

        def fake_fetch_json(_session, url, **_kwargs):
            number = int(url.rsplit("/", 1)[1])
            time.sleep(0.01 * (7 - number))  # Later ids finish first.
            if number == 2:
                raise ScrapeError("transient HTTP 503")
            if number == 5:
                return {"data": None}
            return {"data": {"id": number, "displayText": f"t{number}", "detail": {}}}

        def run(workers: int):
            with patch("scrape.fetch_json", fake_fetch_json):
                return collect_details(
                    list(listings), listings, {}, user_agent="t", workers=workers,
                    delay=0, progress_every=100, report=lambda _: None,
                )

        sequential, concurrent = run(1), run(4)
        self.assertEqual(sequential, concurrent)
        records, warnings, failures = concurrent
        self.assertEqual(sorted(records), [1, 3, 4, 6])
        self.assertEqual(failures, 2)
        self.assertIn("id 2: detail fetch failed (transient HTTP 503)", warnings)
        self.assertLess(
            warnings.index("id 2: detail fetch failed (transient HTTP 503)"),
            warnings.index("id 5: detail fetch failed (detail 5 returned no data object)"),
        )


class KeepPreviousForFailedTests(unittest.TestCase):
    def test_failed_ids_use_previous_record_and_unknown_ids_stay_missing(self) -> None:
        new = {1: {"id": 1, "title": "fresh"}}
        previous = {"1": {"id": 1, "title": "old"}, "2": {"id": 2, "title": "old2"}}
        warnings = keep_previous_for_failed([1, 2, 3], new, previous)
        self.assertEqual(new[1]["title"], "fresh")
        self.assertEqual(new[2], {"id": 2, "title": "old2"})
        self.assertNotIn(3, new)
        self.assertEqual(
            warnings, ["id 2: kept previous record because the detail fetch failed"]
        )

    def test_previous_record_for_id_no_longer_in_catalog_is_ignored(self) -> None:
        new: dict[int, dict] = {}
        keep_previous_for_failed([1], new, {"9": {"id": 9}})
        self.assertEqual(new, {})


class CompareWithPreviousTests(unittest.TestCase):
    def _records(self, titles: list[str]) -> list[dict]:
        return [{"id": index + 1, "title": title} for index, title in enumerate(titles)]

    def _write_previous(self, directory: str, records: list[dict]) -> Path:
        path = Path(directory) / "app_tech.json"
        path.write_text(json.dumps({"data": {"records": records}}), encoding="utf-8")
        return path

    def test_counts_new_updated_unchanged_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_previous(directory, self._records(["a", "b", "c", "d", "e"]))
            current = self._records(["a", "changed", "c", "d"]) + [{"id": 9, "title": "new"}]
            counts = compare_with_previous(PAGE_APP_TECH, current, path)
        self.assertEqual(counts, {"new": 1, "updated": 1, "unchanged": 3, "removed": 1})

    def test_volatile_fields_do_not_count_as_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_previous(directory, [{"id": 1, "title": "a", "currentAdminQuantity": 3}])
            counts = compare_with_previous(
                PAGE_APP_TECH, [{"id": 1, "title": "a", "currentAdminQuantity": 9}], path
            )
        self.assertEqual(counts["unchanged"], 1)

    def test_tag_order_does_not_count_as_update_but_tag_change_does(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_previous(directory, [{"id": 1, "title": "a", "tags": ["x", "y"]}])
            reordered = compare_with_previous(
                PAGE_APP_TECH, [{"id": 1, "title": "a", "tags": ["y", "x"]}], path
            )
            changed = compare_with_previous(
                PAGE_APP_TECH, [{"id": 1, "title": "a", "tags": ["x", "z"]}], path
            )
        self.assertEqual(reordered["unchanged"], 1)
        self.assertEqual(changed["updated"], 1)

    def test_first_run_without_file_counts_everything_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            counts = compare_with_previous(
                PAGE_APP_TECH, self._records(["a", "b"]), Path(directory) / "missing.json"
            )
        self.assertEqual(counts, {"new": 2, "updated": 0, "unchanged": 0, "removed": 0})

    def test_large_removal_requires_explicit_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_previous(directory, self._records(["a", "b", "c", "d"]))
            with self.assertRaises(ScrapeError):
                compare_with_previous(PAGE_APP_TECH, self._records(["a"]), path)
            counts = compare_with_previous(
                PAGE_APP_TECH, self._records(["a"]), path, allow_large_removal=True
            )
            at_limit = compare_with_previous(PAGE_APP_TECH, self._records(["a", "b", "c"]), path)
        self.assertEqual(counts["removed"], 3)
        self.assertEqual(at_limit["removed"], 1)

    def test_owner_records_are_keyed_by_owner_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner_profiles.json"
            path.write_text(
                json.dumps({"data": {"records": [{"owner_key": "OWNER-1", "fullname": "a"}]}}),
                encoding="utf-8",
            )
            counts = compare_with_previous(
                PAGE_OWNER, [{"owner_key": "OWNER-1", "fullname": "a"}, {"owner_key": "OWNER-2"}], path
            )
        self.assertEqual(counts, {"new": 1, "updated": 0, "unchanged": 1, "removed": 0})

    def test_unreadable_previous_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_tech.json"
            bad_contents = (
                "{not json",
                json.dumps([]),
                json.dumps({"data": []}),
                json.dumps({"data": {"records": {}}}),
                json.dumps({"data": {"records": [{"title": "no id"}, {"title": "no id"}]}}),
                json.dumps({"data": {"records": self._records(["a"]) * 2}}),
            )
            for contents in bad_contents:
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(ScrapeError):
                    compare_with_previous(
                        PAGE_APP_TECH, self._records(["a"]), path, allow_large_removal=True
                    )

    def test_duplicate_ids_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ScrapeError):
            compare_with_previous(PAGE_APP_TECH, [{"id": 1}, {"id": 1}], Path(directory) / "x.json")


if __name__ == "__main__":
    unittest.main()
