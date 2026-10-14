# RinMP scraper

A compact, incremental scraper for the public RinMP AppTech registry at
[app.rinmp.com](https://app.rinmp.com/), the เครือข่าย มทร. appropriate
technology (เทคโนโลยีที่เหมาะสมและนวัตกรรมพร้อมใช้) registry under the
บพท./PMUA program. It creates three clean JSON exports and compares each run
with the previous export.

It follows the approved source boundary: every request goes to
`app.rinmp.com`. **Never contact `www.rinmp.com`**: it currently serves a
self-signed Traefik fallback certificate with a bare 404 and has been used to
inject fake content. The scraper collects text data only; media is kept as
ManagedFile IDs and is never downloaded.

## Structure

```text
RinMP/
├── README.md         # this file
├── scrape.py         # collection, normalization, comparison, JSON export
├── test_scrape.py    # offline parser and previous-output checks
├── references/       # institute headquarters provinces (province fallback)
├── requirements.txt  # requests
└── output/           # created at runtime; three JSON exports
```

## How it works

`requests` provides a session with retry handling for temporary HTTP failures.
All data comes from the public JSON API. Detail requests run concurrently on a
small `concurrent.futures` thread pool (one session per worker thread);
normalization stays in the main thread, in ID order.

1. **Discovery**: pages through
   `GET /api/appTechPublic?offset&max=99&__template=appTech.public.list` until
   unique IDs equal `totalCount`. Duplicate IDs, a mid-walk total change, or a
   total below the completeness floor (400) abort the run.
2. **Details**: fetches `GET /api/appTechPublic/{id}` for every catalog ID on
   every run. A failed detail is counted and warned about. Its record is then
   kept from the previous `app_tech.json` (with a warning, so that record may
   be one run stale); the run fails only if a catalog ID has no fresh and no
   previous record.
3. **Reconciliation gate**: `listAppTechStatisticData` must confirm both the
   total count and every per-institute count of the collected records; a
   mismatch fails the run and preserves the previous exports.
4. **Owners**: `listAppTechOwnerProfile` is deduplicated by normalized
   fullname (the source publishes more rows than distinct people).
5. **Exports**: rewritten only after a fully successful run.

### Deliberately not scraped

- `listAppTechInstituteMapData`: derivative of the list catalog (verified to
  match our group-by exactly); its choropleth values are all zero.
- `listAppTechInnovatorMapData`: platform-user choropleth (sum equals
  `normalUserCount`), wrong entity type for this dataset.
- `listAppTechInteractionMapData`: unverifiable semantics (values match no
  other endpoint).
- Images and videos.

### PII boundary

Every list and detail response carries owner contact data, and some emails hide
in free-text fields (for example `ownerContact.detail`). `strip_pii()` runs on
everything before it is written: contact keys are dropped and email-shaped
values are replaced with `[redacted-contact]`. No email or phone value should
exist in `output/`. Owner identity beyond the public display name is reduced to
a non-reversible `owner_key`.

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/RinMP/requirements.txt"

# Smoke test: writes nothing.
.venv/bin/python "ฝ่าย 2/RinMP/scrape.py" --limit 5

# Full refresh: re-fetches all detail pages and replaces the exports.
.venv/bin/python "ฝ่าย 2/RinMP/scrape.py"
```

Options:

- `--workers N` (1–8, default 4): concurrent detail requests. `--workers 1`
  fetches one at a time.
- `--delay SECONDS` (default 0): pause each worker takes after every request.
- `--limit N`: smoke test; processes the first N details and writes nothing.
- `--allow-large-removal`: override the 25% removal guard (see below).
- `--user-agent NAME` (default `RinMPAppTechCollector`)
- `--progress-every N`, `--quiet`, `--output-dir`: run controls.

A full run is about 630 detail requests. A JSON run summary (status,
reconciliation, counts, warnings) is printed to standard output. A failed run
leaves the previous exports untouched and exits 1.

## Output

| Export | Records (2026-10-12) | Content |
| --- | --- | --- |
| `output/app_tech.json` | 632 | One record per technology: title, status, ATL level, TRL, institute, category/sub-category, owner display name, description sections, target segments, structured addresses and resolved province, IP type, commercial cost, tags, media file IDs |
| `output/statistics.json` | n/a | Platform counters (`requirementCount`, `matchRequirementCount`, `normalUserCount` exist nowhere else) plus the reconciliation proof against the collected catalog |
| `output/owner_profiles.json` | 309 | Owner dimension deduplicated by normalized fullname; `owner_key` is a SHA-256-derived stable join key |

The two record exports share this envelope:

```json
{
  "schema_version": 1,
  "dataset": "app_tech",
  "source_base_url": "https://app.rinmp.com/",
  "scraped_at": "2026-10-12T12:00:32.965656+00:00",
  "run_id": "20261012T120021Z",
  "warnings": [],
  "record_count": 632,
  "data": { "records": [] }
}
```

`statistics.json` has the same keys, with `reconciliation` in place of
`record_count`. Known data quirks are recorded as warnings, for example records
without any expected description section and an institute without a
headquarters province.

## Comparison with the previous run

The previous `output/app_tech.json` and `owner_profiles.json` are the only
state. Each run compares its records with them by ID (content hash, ignoring
volatile counters and tag order, because the API returns tags in a different
order on each request) and reports `new`, `updated`, `unchanged`, and `removed`
counts. Dropping more than 25% of the previous records fails the run unless
`--allow-large-removal` is passed, and an unreadable previous file fails it
too. With no previous file every record counts as new. Version history is not
kept locally.

Refresh trigger: a changed `totalCount` in the discovery pass. If it moves
materially, recheck the completeness floor and the reconciliation gate.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/RinMP"
```

All tests run offline: cleaning and normalization, PII stripping,
reconciliation, previous-output comparison, the failed-detail fallback, and
ordered concurrent fetching.
