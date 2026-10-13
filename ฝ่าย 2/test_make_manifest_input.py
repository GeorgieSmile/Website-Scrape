import json
import tempfile
import unittest
from pathlib import Path

import make_manifest_input as tool

DECLARATION = {
    "source_id": "f2_example",
    "as_of_rule": "scraped_at",
    "datasets": [
        {"dataset_key": "products", "file": "products.json", "grain": "one file of products", "identity_fields": ["data[].id"]},
        {"dataset_key": "family_all", "file": "dashboards/family_all.json", "grain": "one dashboard", "identity_fields": []},
    ],
}


class BuildManifestInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scraper = Path(self._tmp.name)
        self.output = self.scraper / "output"
        (self.output / "dashboards").mkdir(parents=True)
        self.write(self.scraper / "evidence.json", DECLARATION)
        self.write_envelope("products.json", "2026-10-13T04:38:00+00:00", source_urls=["https://example.org/a", "https://example.org/b"])
        self.write_envelope("dashboards/family_all.json", "2026-10-13T04:37:00+00:00", source_url="https://example.org/a")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def write_envelope(self, file: str, scraped_at: str, run_id: str = "20261013T043700Z", **extra: object) -> None:
        self.write(self.output / file, {"scraped_at": scraped_at, "run_id": run_id, **extra, "data": []})

    def test_builds_manifest_from_declaration_and_one_run(self) -> None:
        manifest, run_id = tool.build_manifest_input(self.scraper, "georgiesmile")
        self.assertEqual(run_id, "20261013T043700Z")
        self.assertEqual(manifest["fetched_by"], "georgiesmile")
        self.assertEqual(manifest["fetched_at"], "2026-10-13T04:37:00+00:00")
        self.assertEqual(manifest["upstream"], [{"url": "https://example.org/a"}, {"url": "https://example.org/b"}])
        self.assertEqual(
            manifest["datasets"][0],
            {
                "dataset_key": "products",
                "file": "products.json",
                "as_of": "2026-10-13T04:38:00+00:00",
                "grain": "one file of products",
                "identity_fields": ["data[].id"],
            },
        )
        self.assertEqual(manifest["datasets"][1]["as_of"], "2026-10-13T04:37:00+00:00")

    def test_existing_manifest_input_is_not_an_undeclared_file(self) -> None:
        self.write(self.output / "manifest_input.json", {})
        tool.build_manifest_input(self.scraper, "georgiesmile")

    def test_mixed_runs_are_refused(self) -> None:
        self.write_envelope("products.json", "2026-10-13T04:38:00+00:00", run_id="20261012T120000Z")
        with self.assertRaisesRegex(tool.ManifestError, "mixes runs"):
            tool.build_manifest_input(self.scraper, "georgiesmile")

    def test_missing_declared_file_is_refused(self) -> None:
        (self.output / "products.json").unlink()
        with self.assertRaisesRegex(tool.ManifestError, "missing from output"):
            tool.build_manifest_input(self.scraper, "georgiesmile")

    def test_undeclared_file_is_refused(self) -> None:
        (self.output / ".DS_Store").write_bytes(b"")
        with self.assertRaisesRegex(tool.ManifestError, r"undeclared files.*\.DS_Store"):
            tool.build_manifest_input(self.scraper, "georgiesmile")

    def test_missing_fetched_by_is_refused(self) -> None:
        with self.assertRaisesRegex(tool.ManifestError, "fetched_by"):
            tool.build_manifest_input(self.scraper, "  ")

    def test_missing_run_id_or_bad_scraped_at_is_refused(self) -> None:
        self.write(self.output / "products.json", {"scraped_at": "2026-10-13T04:38:00+00:00", "data": []})
        with self.assertRaisesRegex(tool.ManifestError, "run_id"):
            tool.build_manifest_input(self.scraper, "georgiesmile")
        self.write_envelope("products.json", "2026-10-13T04:38:00")
        with self.assertRaisesRegex(tool.ManifestError, "no timezone"):
            tool.build_manifest_input(self.scraper, "georgiesmile")

    def test_declaration_problems_are_refused(self) -> None:
        cases = {
            "grain": lambda d: d["datasets"][0].update(grain=""),
            "as_of_rule": lambda d: d.update(as_of_rule="guess"),
            "safe relative": lambda d: d["datasets"][0].update(file="../products.json"),
            "duplicate dataset_key": lambda d: d["datasets"][1].update(dataset_key="products"),
            "identity_fields": lambda d: d["datasets"][0].update(identity_fields="data[].id"),
        }
        for message, change in cases.items():
            with self.subTest(message):
                declaration = json.loads(json.dumps(DECLARATION))
                change(declaration)
                self.write(self.scraper / "evidence.json", declaration)
                with self.assertRaisesRegex(tool.ManifestError, message):
                    tool.build_manifest_input(self.scraper, "georgiesmile")
        (self.scraper / "evidence.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(tool.ManifestError, "cannot parse"):
            tool.build_manifest_input(self.scraper, "georgiesmile")


if __name__ == "__main__":
    unittest.main()
