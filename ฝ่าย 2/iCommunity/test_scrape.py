"""Offline checks for the iCommunity scraper."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class StateTests(unittest.TestCase):
    def record(self, value: str) -> dict[str, object]:
        return {"record_type": "community_product", "source_key": "1", "name": value}

    def test_versions_and_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(Path(directory) / "state.sqlite3")
            scrape.init_db(connection)
            first = scrape.sync_records(connection, "community_products", [self.record("A")], "2026-01-01T00:00:00Z", allow_retirement=False, allow_large_removal=False)
            same = scrape.sync_records(connection, "community_products", [self.record("A")], "2026-01-02T00:00:00Z", allow_retirement=False, allow_large_removal=False)
            changed = scrape.sync_records(connection, "community_products", [self.record("B")], "2026-01-03T00:00:00Z", allow_retirement=False, allow_large_removal=False)
            retired = scrape.sync_records(connection, "community_products", [], "2026-01-04T00:00:00Z", allow_retirement=True, allow_large_removal=True)
            self.assertEqual(first["new"], 1)
            self.assertEqual(same["unchanged"], 1)
            self.assertEqual(changed["changed"], 1)
            self.assertEqual(retired["retired"], 1)
            self.assertEqual(scrape.active_records(connection, "community_products"), [])

    def test_duplicate_source_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            scrape.validate_records("community_products", [self.record("A"), self.record("B")])

    def test_skip_details_retains_previous_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(Path(directory) / "state.sqlite3")
            scrape.init_db(connection)
            complete = {
                "record_type": "apptech_innovator",
                "source_key": "2:42",
                "name": "A",
                "detail": {"collaboration_network": {"start_level": 1, "current_level": 2}},
            }
            scrape.sync_records(connection, "apptech_innovators", [complete], "2026-01-01T00:00:00Z", allow_retirement=False, allow_large_removal=False)
            smoke = {**complete, "name": "B", "detail": None}
            scrape.retain_known_details_for_smoke_run(connection, "apptech_innovators", [smoke])
            self.assertEqual(smoke["detail"], complete["detail"])

    def test_interrupted_run_is_recovered_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            scrape.init_db(connection)
            run_id = scrape.start_run(connection, "community_products")
            connection.close()
            reopened = sqlite3.connect(path)
            scrape.init_db(reopened)
            outcome, error = reopened.execute("SELECT outcome, error_text FROM runs WHERE run_id=?", (run_id,)).fetchone()
            self.assertEqual(outcome, "interrupted")
            self.assertIn("process did not finish", error)


if __name__ == "__main__":
    unittest.main()
