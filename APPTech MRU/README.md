# APPTech MRU scraper

This is a compact, incremental scraper for the public APPTech MRU platform at
[38rat.nstru.ac.th](https://38rat.nstru.ac.th/) — the Appropriate Technology
marketplace hosted by Nakhon Sawan Rajabhat University. It creates two clean
JSON exports (innovations and requirements) while retaining full change
history in SQLite.

It follows the approved source boundary: every request is to
`38rat.nstru.ac.th`. The scraper collects text data only — it does not
download images or videos, follow external links, submit forms, or guess
record IDs.

## Structure

```text
APPTech MRU/
├── scrape.py         # collection, parsing, cleaning, SQLite sync, and JSON export
├── test_scrape.py    # offline parser and incremental-state checks
├── fixtures/         # saved live detail pages used by the offline tests
├── requirements.txt  # requests and Beautiful Soup
├── SCRAPER_PLAN.md   # investigated design and source findings
├── state/            # created at runtime; SQLite incremental state
└── output/           # created at runtime; innovations.json and requirements.json
```

## Stack and collection method

`requests` provides a persistent session (GET for detail pages, POST for the
catalog API) with retry handling for temporary HTTP failures. `BeautifulSoup`
parses the server-rendered detail pages. Python's standard-library `sqlite3`
keeps the latest active version of each record plus an immutable version log.

The scraper is a **hybrid**, verified against the live site on 2026-08-22:

| Dataset | Discovery (public JSON API) | Detail strategy | Stable ID | Export |
| --- | --- | --- | --- | --- |
| Innovations | `POST /backend/ajax/public/innovation.php` (`fetch_innovationAll_JSON`, 12 rows/page, totaldata=501) | one GET per `innovationdetail.php?innovationid=N` | `innovationid` | `innovations.json` |
| Requirements | `POST /backend/ajax/public/requirement.php` (`fetch_requirementAll_JSON`, totaldata=2) | one GET per `requirementdetail.php?requirementid=N` | `requirementid` | `requirements.json` |

There is no detail JSON API: the catalog endpoints expose only listing-level
fields, and the detail pages render the full record server-side. Catalog
completeness is guarded — unique IDs collected must equal the API's reported
`totaldata`, which must not change mid-pagination.

### Source findings worth knowing

- **TRL is published as a level image** (`/store/TRL-9Lv/TRL-N.png`). The
  commented-out `ระดับผลงาน: TRL7` heading in the HTML is stale template text,
  identical on every page, and must not be trusted. The scraper takes the TRL
  level from the image filename and keeps the image URL as evidence.
- **Placeholder text** (`-`, `ไม่มี`) means "not provided" and becomes `null`.
- **Inline lists** (inventor names, required-expertise fields) are rendered as
  plain text separated by wide spacing; the scraper splits on runs of two or
  more spaces and keeps the verbatim text in a `*_raw` field, because the
  source grouping is ambiguous.
- The API ignores `targetpagenumber` and hard-caps the window at 12 rows;
  pagination uses `startlimit` as the offset.

## Install and run

Run these commands from the workspace root. The project must use the existing
virtual environment at `Website Scrape/.venv`.

```bash
uv pip install --python "Website Scrape/.venv/bin/python" -r "Website Scrape/APPTech MRU/requirements.txt"
"Website Scrape/.venv/bin/python" "Website Scrape/APPTech MRU/scrape.py" \
  --dataset all --delay 1 \
  --user-agent "ApptechMruCollector/1.0 (contact: you@example.org)"
```

Use a contactable User-Agent before a production run. A one-second delay is
the normal policy between requests; a full refresh is roughly 550 requests
(~10 minutes). A network, parser, or validation failure is reported per
dataset and does not replace that dataset's last successful JSON export.

Useful development commands:

```bash
# Offline tests (no network)
"Website Scrape/.venv/bin/python" -m unittest discover -s "Website Scrape/APPTech MRU"

# Smoke test a few records without updating state or outputs
"Website Scrape/.venv/bin/python" "Website Scrape/APPTech MRU/scrape.py" --limit 3
```

## Output envelope

Both exports share the same top-level structure:

```json
{
  "schema_version": 1,
  "dataset": "innovation",
  "source_base_url": "https://38rat.nstru.ac.th/",
  "scraped_at": "2026-08-23T09:00:00+00:00",
  "run_id": "20260823T090000Z",
  "record_count": 501,
  "warnings": [],
  "data": []
}
```

Each element of `data[]` carries `record_type`, `record_id`, `title`,
`detail_url`, the parsed `data` payload, and per-record
`validation_warnings`. Innovations include listing metadata (owner,
university), category, innovation type, TRL/SRL, description sections,
rights holder, IP details, inventors and researchers (lead/co with faculty
and university), target groups, highlights, funding (with parsed THB
amounts), ROI/SROI, and innovation value. Requirements include the
description sections, required expertise, impact sections, funding, type,
category, and tags. Ambiguous values stay verbatim in `*_raw` fields; missing
values become `null` with a warning — nothing is guessed.

## Incremental behavior

SQLite (`state/apptech_mru.sqlite3`) is the authoritative store:

- `runs` — one row per run with its outcome and summary.
- `records` — the current payload per (dataset, record ID) with content
  hash and first-seen / last-seen / last-changed timestamps.
- `record_versions` — an immutable copy of every created, updated, or
  retired payload.

Every run re-fetches the (cheap) catalog and the detail pages, hashes the
cleaned payload, and writes only actual changes. View counts are excluded
from the hash, so they do not create versions. Records are deactivated only
when a complete successful run confirms their absence, and a removal of more
than 25% of an established dataset (≥10 records) fails the run unless
`--allow-large-removal` is passed.

## Verified baselines

First full run (2026-08-23): 501 innovations (all with category, TRL, SRL,
innovation type), 2 requirements; a second unchanged run produced zero new
record versions. Innovation 10620 matches its live page: category
`เกษตรและเทคโนโลยี`, type `พร้อมใช้`, TRL 6 (image `TRL-6.png`), SRL 7,
funding 550,000 บาท.
