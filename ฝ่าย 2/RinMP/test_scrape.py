"""Offline tests for the RinMP AppTech scraper. No network access."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scrape import (
    PAGE_APP_TECH,
    ScrapeError,
    build_owner_display_name,
    clean_text,
    derive_record_warnings,
    html_to_text,
    load_hq_provinces,
    normalize_detail,
    open_database,
    optional_text,
    owner_key_for,
    parse_addresses,
    parse_owner_profiles,
    parse_trl,
    reconcile_statistics,
    resolve_province,
    semantic_hash,
    strip_pii,
    sync_records,
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


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.connection = open_database(Path(self.tmp.name) / "state.sqlite3")
        self.now = "2026-08-23T00:00:00+00:00"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _records(self, titles: list[str]) -> list[dict]:
        return [{"id": index + 1, "title": title} for index, title in enumerate(titles)]

    def test_insert_update_retire_lifecycle_with_versions(self) -> None:
        sync_records(self.connection, PAGE_APP_TECH, self._records(["a", "b", "c", "d"]), "r1", self.now)
        versions = self.connection.execute(
            "SELECT event FROM record_versions WHERE page_id = ?", (PAGE_APP_TECH,)
        ).fetchall()
        self.assertEqual([row["event"] for row in versions], ["inserted"] * 4)

        sync_records(
            self.connection, PAGE_APP_TECH, self._records(["a", "b", "c", "d"]), "r2", self.now
        )
        unchanged = self.connection.execute(
            "SELECT COUNT(*) AS n FROM record_versions WHERE page_id = ? AND event != 'inserted'",
            (PAGE_APP_TECH,),
        ).fetchone()["n"]
        self.assertEqual(unchanged, 0)

        sync_records(
            self.connection, PAGE_APP_TECH, self._records(["a", "changed", "c", "d"]), "r3", self.now
        )
        sync_records(self.connection, PAGE_APP_TECH, self._records(["changed", "c", "d"]), "r4", self.now)
        changes = [
            (row["event"], row["external_id"])
            for row in self.connection.execute(
                "SELECT event, external_id FROM record_versions WHERE page_id = ? AND event != 'inserted'",
                (PAGE_APP_TECH,),
            )
        ]
        # r3 rewrote title of record 2 only; r4 retired record 4 (the other
        # 'updated' entries are title shifts caused by positional ids).
        self.assertIn(("updated", "2"), changes)
        self.assertIn(("retired", "4"), changes)
        self.assertEqual(changes.count(("retired", "4")), 1)

    def test_duplicate_ids_fail(self) -> None:
        with self.assertRaises(ScrapeError):
            sync_records(
                self.connection, PAGE_APP_TECH, [{"id": 1}, {"id": 1}], "r1", self.now
            )

    def test_large_removal_requires_explicit_allowance(self) -> None:
        sync_records(self.connection, PAGE_APP_TECH, self._records(["a", "b", "c", "d"]), "r1", self.now)
        with self.assertRaises(ScrapeError):
            sync_records(self.connection, PAGE_APP_TECH, self._records(["a"]), "r2", self.now)
        sync_records(
            self.connection, PAGE_APP_TECH, self._records(["a"]), "r2", self.now,
            allow_large_removal=True,
        )


if __name__ == "__main__":
    unittest.main()
