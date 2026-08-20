# Cultural Map WWW scraper

This is a compact, incremental scraper for the public `www.culturalmapthailand.info` site. It creates five clean JSON exports from the Map/Inspiration, Products, Activities, Re-Creation, and Team content while retaining the previous successful result in SQLite.

It follows the approved source boundary: every request is to `www.culturalmapthailand.info`. The Map/Inspiration feed can publish image locations on the similar `dp` hostname; those locations are retained as **unfetched metadata** only. The scraper neither requests `dp` nor downloads images, follows Google Maps/external links, submits forms, or guesses record IDs.

## Structure

```text
Cultural Map/
├── scrape.py         # collection, parsing, cleaning, SQLite sync, and JSON export
├── test_scrape.py    # offline parser and incremental-state checks
├── references/       # pinned DOPA names/codes and verified local-ID crosswalks
├── requirements.txt  # requests and Beautiful Soup
├── SCRAPER_PLAN.md   # investigated design and source findings
├── state/            # created at runtime; SQLite incremental state
└── output/           # created at runtime; five public-data JSON exports
```

The intentionally small implementation has two Python files: one readable pipeline and one focused offline test module.

## Stack and collection method

`requests` provides a persistent GET-only session with retry handling for temporary HTTP failures. `BeautifulSoup` parses server-rendered listings and detail pages. Python's standard-library `sqlite3` keeps the latest active version of each record.

| Content | Canonical www discovery | Detail strategy | Stable ID | Export |
| --- | --- | --- | --- | --- |
| Map / Inspiration | `json-mark.php?...` | one public JSON response | `CD-<CId>` | `map_inspiration.json` |
| Products | `/culturalproduct`, then `P-1`–`P-6` | every linked `PD-*` page | `PD-<id>` | `products.json` |
| Activities | `/activity` | every linked `G-*` page | `G-<id>` | `activities.json` |
| Re-Creation | `/ReAll` | every linked `REDetail-*` page | `REDetail-<id>` | `recreation.json` |
| Team | `/team` | page itself | hash of group and name | `team.json` |

Map and Inspiration are deliberately one dataset: the inspected Inspiration gallery was a client-side 500-card subset of the larger Map JSON feed, and its sampled records matched the feed by ID, title, category, and image. Scraping it separately would duplicate records and lose the majority of the public corpus.

## Install and run

Run these commands from the workspace root. The project must use the existing virtual environment at `Website Scrape/.venv`.

```bash
uv pip install --python "Website Scrape/.venv/bin/python" -r "Website Scrape/Cultural Map/requirements.txt"
"Website Scrape/.venv/bin/python" "Website Scrape/Cultural Map/scrape.py" \
  --source all --delay 1 \
  --user-agent "CulturalMapWWWCollector/1.0 (contact: you@example.org)"
```

Use a contactable User-Agent before a production run. A one-second delay is the normal policy between detail requests. A network failure, parser failure, or validation failure is reported per source and does not replace that source's last successful JSON export.

Useful development commands:

```bash
# Parse just a small live sample without touching state or JSON output.
"Website Scrape/.venv/bin/python" "Website Scrape/Cultural Map/scrape.py" --limit 2

# Re-run one source.
"Website Scrape/.venv/bin/python" "Website Scrape/Cultural Map/scrape.py" --source products --delay 1

# Run offline tests.
cd "Website Scrape/Cultural Map"
../.venv/bin/python -m unittest -v test_scrape.py
```

`--limit` is explicitly a smoke test: it discovers and parses only the first natural-ID records needed for the sample, then exits without changing SQLite or the JSON exports. `--allow-large-removal` is the conscious override for the normal removal guard.

Long runs report timestamped progress to standard error: source start, discovery totals, the first/final detail fetch, and every tenth detail page by default. The final JSON run summary remains on standard output, so it is still safe to redirect or parse. Use `--progress-every 25` to reduce the detail-page messages, or `--quiet` to suppress them.

## Output contract

Each output file has this common envelope:

```json
{
  "schema_version": 1,
  "page_id": "products",
  "source_urls": ["https://www.culturalmapthailand.info/culturalproduct"],
  "scraped_at": "2026-08-14T00:00:00+00:00",
  "run_id": "20260814T000000Z",
  "warnings": [],
  "data": { "records": [] }
}
```

Every record has `external_id`, `title`, canonical `source_url`, `discovered_from`, cleaned page-specific `data`, and `validation_warnings`. Product and Activity records retain published external-link and gallery metadata. Re-Creation retains its typed gallery, image-link, and video-link metadata without duplicating gallery URLs as generic external links. These URLs are never requested. Team records contain the section group and profile-image URL.

`map_inspiration.json` is schema version 2 because it has a richer normalized contract. Its records have `identifiers`, bilingual `names`, labelled `classification`, `description`, `people`, `funding`, `media`, `assessment`, `keywords`, dates, and metrics. It deliberately has no raw `source_fields` object.

Map location is structured as `address_raw`, `postal_code`, a coordinate pair, and named Province/Amphure/Tambon objects. For a published six-digit `CulDistrict`, the scraper uses it as an official DOPA Tambon code: its first two digits identify Province, its first four Amphure, and all six Tambon. The checked lookup resolves all 5,179 current standard codes. For a record without that code, a verified, evidence-counted Cultural Map local-ID crosswalk can resolve Province and—in three current cases—Amphure. The remaining unavailable levels are `null` and receive a validation warning; address text and coordinates are never reverse-geocoded or guessed.

`references/map_inspiration_lookups_v1.json` pins the DOPA dataset source/date, cultural taxonomy, institution names, and local-ID crosswalk. The latter was empirically derived only where a local ID and valid DOPA code occurred together. Each normal run checks it for conflicts, so a changed local-ID meaning becomes visible as a record warning rather than silently remapping data.

Funding is deliberately three-valued. `FundType: "y"` becomes `project_funded: true` and `status: "funded"`; a missing value becomes `project_funded: null` and `status: "not_indicated"`; any unfamiliar value is `null`, `unrecognized_source_value`, and a warning. An empty source value is not evidence that a project was unfunded.

## Incremental state, cleaning, and validation

`state/culturalmap_www.sqlite3` has a run log and one active record per `(page_id, external_id)`. Records are inserted, updated only when meaningful content changes, or marked inactive when absent from a later successful crawl. The semantic hash ignores public `view_count` fluctuations and scrape/run timestamps, so a changing counter does not create an update.

The central cleaning functions normalize Unicode NFC, remove BOM/NBSP artifacts, collapse excess whitespace, and convert site placeholders such as `-` and `ไม่มี` to `null`. Coordinates and integer fields are parsed conservatively; invalid values stay `null` with a record warning. URLs are made absolute only when they are valid HTTP(S), and all canonical record pages must resolve to the `www` hostname.

Before a source is committed, the scraper rejects duplicate IDs and implausibly small results: at least 4,000 Map/Inspiration, 150 Products, 25 Activities, 50 Re-Creation, and 8 Team records. It also rejects a run that would remove more than 25% of an already-active source, unless `--allow-large-removal` is given explicitly. JSON output is written atomically only after the SQLite update succeeds.

## Main functions

- `collect_*`: discover public records, fetch allowed detail pages, and return normalized records.
- `parse_*_detail`: isolate each known public detail template and extract its page-specific fields.
- `clean_text`, `optional_text`, `parse_coordinate`, and `clean_links`: central normalization and non-following link handling.
- `administrative_location`: DOPA-first geographic lookup with an evidence-checked local-ID fallback.
- `sync_records`: validates a full source, compares semantic hashes, and writes incremental SQLite state in one transaction.
- `export_page`: makes the stable, sorted JSON export from active state.
- `run_scrape`: coordinates per-source failure isolation and prints a machine-readable run summary.
