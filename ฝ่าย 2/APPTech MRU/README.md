# APPTech MRU scraper

This is a compact, incremental scraper for the public APPTech MRU platform at
[38rat.nstru.ac.th](https://38rat.nstru.ac.th/) — the Appropriate Technology
marketplace hosted by Nakhon Sawan Rajabhat University. It creates two clean
JSON exports (innovations and requirements) and compares each run with the
previous export.

It follows the approved source boundary: every request is to
`38rat.nstru.ac.th`. The scraper collects text data only — it does not
download images or videos, follow external links, submit forms, or guess
record IDs.

## Structure

```text
APPTech MRU/
├── scrape.py         # collection, parsing, cleaning, comparison, and JSON export
├── browser_access.py # coordinated Chrome-cookie handoff for requests sessions
├── chrome_verify.mjs # internal Chrome DevTools helper (requires Node.js 22+)
├── test_scrape.py     # offline parser and previous-output checks
├── test_browser_access.py # offline verification, concurrency, and cleanup checks
├── fixtures/         # saved live detail pages used by the offline tests
├── requirements.txt  # requests and Beautiful Soup
├── SCRAPER_PLAN.md   # investigated design and source findings
└── output/           # created at runtime; innovations.json and requirements.json
```

## Stack and collection method

`requests` provides a persistent session (GET for detail pages, POST for the
catalog API) with retry handling for temporary HTTP failures. `BeautifulSoup`
parses the server-rendered detail pages. Detail pages are fetched concurrently
by a small `concurrent.futures` thread pool (one session per worker thread);
parsing stays in the main thread, in catalog order.

If the site returns its **“One moment, please…”** browser check and `--browser`
is given, the scraper opens visible Chrome with a fresh temporary profile and a fixed, nonzero
localhost debugging port. After the normal check completes, it transfers the
browser cookies and actual user agent to the catalog and worker sessions.
Collection continues through `requests`, not through browser page loads.
There are no browser-property overrides, stealth patches, or CAPTCHA solvers.

Verification is shared across workers: at most one initial browser startup
and one refresh per run. A failed startup or exhausted refresh budget stops
collection safely. Chrome is closed and its temporary profile removed after
each startup; existing personal browser profiles are not accessed. Transferred
cookies stay in Python memory and are never logged or saved to the repository.

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

Run these commands from the `Website Scrape` workspace root using its existing
`.venv`. Browser verification additionally requires **Google Chrome/Chromium,
Node.js 22+ on PATH, and a visible desktop session**. It does not install a
browser or use Playwright. On macOS, the standard Google Chrome application
path is detected automatically; on Linux, common Chrome/Chromium executable
names are searched on PATH. Other executable locations need `--chrome-path`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/APPTech MRU/requirements.txt"

# Safe live smoke test: writes no output.
.venv/bin/python "ฝ่าย 2/APPTech MRU/scrape.py" --dataset all --limit 4 --workers 4

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/APPTech MRU/scrape.py" \
  --dataset all --workers 4 --browser \
  --user-agent "ApptechMruCollector"
```

A full refresh is roughly 550 requests. Options:

- `--workers N` (1–8, default 4): concurrent detail-page fetches. The catalog
  API is still paged sequentially. `--workers 1` fetches one page at a time.
- `--delay SECONDS` (default 0): pause each worker takes after every request.
  The effective request rate is roughly `workers / (response time + delay)`.
- `--limit N`: smoke test; writes nothing.
- `--allow-large-removal`: override the removal guard (see below).
- `--chrome-path PATH`: explicit Chrome/Chromium executable.
- `--browser-timeout SECONDS`: per-startup verification deadline (default 40;
  greater than 0 and at most 300). Process startup/cleanup can add a few seconds.
- `--browser`: allow the Chrome handoff described above. Off by default; without
  it, a blocked request stops with an explicit verification error.

`--user-agent` identifies normal requests, but a successful browser handoff
replaces it with Chrome's actual user agent to match the verified session.
If unattended collection cannot use a visible browser, request API access or
an allowlist from the site administrator instead of assuming headless Chrome
will pass this check.

A network, parser, or validation failure is reported per dataset and does not
replace that dataset's last successful JSON export.

Useful development commands:

```bash
# Offline tests (no network)
.venv/bin/python -m unittest discover -s "ฝ่าย 2/APPTech MRU"

# Smoke test a few records without writing outputs
.venv/bin/python "ฝ่าย 2/APPTech MRU/scrape.py" --limit 3
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

## Comparison with the previous run

The previous `output/<dataset>.json` is the only state. Every run re-fetches
the catalog and all detail pages, then compares the new records with that file
by record ID. The JSON run summary printed to stdout reports `new`, `updated`,
`unchanged`, and `removed` counts per dataset. Hashes ignore `view_count`, so a
view-count change alone is `unchanged` — but the new file always carries the
current view count.

A removal of more than 25% of an established dataset (≥10 previous records)
fails that dataset unless `--allow-large-removal` is passed. If the previous
file exists but cannot be read, the dataset fails rather than skipping the
guard; with no previous file (first run) every record counts as new. Version
history is not kept locally.

## Verified baselines

First full run (2026-08-23): 501 innovations (all with category, TRL, SRL,
innovation type), 2 requirements; a second unchanged run produced zero new
record versions. Innovation 10620 matches its live page: category
`เกษตรและเทคโนโลยี`, type `พร้อมใช้`, TRL 6 (image `TRL-6.png`), SRL 7,
funding 550,000 บาท.

Browser-access smoke test (2026-10-12): the implemented CLI completed with
`--dataset all --limit 13 --workers 4`, parsing 13 innovations across two
catalog pages and both requirements after one Chrome startup. The APIs reported 501 innovations and
2 requirements. Both output files retained their original SHA-256 hashes.
All 49 offline tests passed, including coordinated refresh, bounded cleanup,
and CLI help rendering.
The final live check left no temporary Chrome profiles. This was not a full
refresh or a new performance baseline; long-duration cookie expiry remains
unverified live (coordinated refresh is tested offline).
