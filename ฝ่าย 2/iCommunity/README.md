# iCommunity scraper

A compact, incremental scraper for public AppTech PMUA dashboard records at
[apptechpmua.com](https://apptechpmua.com/) and public iCommunity
WordPress/WooCommerce data at [icommunity.in.th](https://www.icommunity.in.th/).
It creates seven clean JSON exports and compares each run with the previous
export.

It follows the approved source boundary: every request goes to
`apptechpmua.com` or `www.icommunity.in.th`. The scraper collects text data
only. It does not collect contacts, patent-holder details, gallery media, or
documents, and it does not keep a full raw HTTP archive.

## Known issue: WordPress datasets blocked

As of the live run on 2026-10-12 the three WordPress/WooCommerce datasets
(`programme_background`, `community_products`, `activity_posts`) do not work.
icommunity.in.th answers HTTP 200 with `{"message": "Access denied by Imunify360
bot-protection. IPs used for automation should be whitelisted"}`, so these
datasets fail and the run exits 1. Their existing output files are left
unchanged. The four AppTech datasets are not affected. No workaround is
implemented; the site owner would need to whitelist the scraper's IP.

## Structure

```text
iCommunity/
├── README.md         # this file
├── scrape.py         # collection, parsing, cleaning, comparison, JSON export
├── test_scrape.py    # offline parser and previous-output checks
├── fixtures/         # saved AppTech pages used by the offline tests
├── requirements.txt  # requests and Beautiful Soup (pinned)
├── SCRAPER_PLAN.md   # local design notes (git-ignored, absent from a clone)
└── output/           # created at runtime; one JSON file per dataset
```

## How it works

`requests` provides a session with retry handling; `BeautifulSoup` parses the
AppTech dashboard and detail pages. The AppTech groups are discovered from the
dashboards on every run and crawled one group at a time. The AppTech detail
pages (the bulk of the requests) are fetched concurrently by a small
`concurrent.futures` thread pool (one session per worker thread); parsing
stays in the main thread, in the original order. The group lists, summaries,
and WordPress endpoints are fetched one request at a time.

| Dataset | Public source | Stable key |
| --- | --- | --- |
| `apptech_group_catalog` | AppTech innovation and innovator dashboards (group filter values) | `dashboard_entity + group_id` |
| `apptech_innovators` | `POST /innovatorExport/fetchExportList`, then `/innovator/content/{token}` per innovator | `group_id + innovatorID` |
| `apptech_innovations` | `POST /innovation/fetchExportList`, then `/innovation/content/{id}` per innovation | `group_id + innovationID` |
| `apptech_dashboard_summaries` | `POST` dashboard summary endpoints per entity and group | entity + group |
| `programme_background` | WordPress pages API | WordPress page `id` |
| `community_products` | WooCommerce Store API | product `id` |
| `activity_posts` | WordPress posts API | post `id` |

Rows from different AppTech groups are kept separately even when they share an
entity ID. The AppTech summary counters are exported as source snapshots only.
They are not used to fill, reject, or merge AppTech list records. Public
contact information is redacted from WordPress/WooCommerce descriptive HTML
before export.

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/iCommunity/requirements.txt"

# One group, one record, with details. Smoke test: writes nothing.
.venv/bin/python "ฝ่าย 2/iCommunity/scrape.py" --dataset apptech_innovators --groups 2 --limit 1

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/iCommunity/scrape.py"

# Non-AppTech public sources only.
.venv/bin/python "ฝ่าย 2/iCommunity/scrape.py" \
  --dataset programme_background --dataset community_products --dataset activity_posts

# Skip detail pages while testing list discovery (details from the previous export are kept).
.venv/bin/python "ฝ่าย 2/iCommunity/scrape.py" --dataset apptech_innovations --skip-details
```

Options:

- `--dataset NAME` (repeatable; default: all seven datasets)
- `--workers N` (1–8, default 4): concurrent AppTech detail-page fetches.
  `--workers 1` fetches one page at a time.
- `--delay SECONDS` (default 0): pause each worker takes after every request.
- `--groups IDS` and `--limit N`: smoke tests. They write nothing, for every
  dataset.
- `--skip-details`: do not fetch AppTech detail pages.
- `--allow-large-removal`: required before a run can retire a large share of a
  dataset (50% of the previous records, at least 10).
- `--user-agent NAME` (default `iCommunityCollector`)
- `--page-size`, `--timeout`, `--output-dir`, `--quiet`: run controls.

A JSON run summary (per-dataset status, counts, warnings) is printed to
standard output. A dataset that fails is reported there and keeps its previous
file; the run then exits 1.

## Output

Every file has the same top-level structure:

```json
{
  "schema_version": 1,
  "dataset": "apptech_innovators",
  "source_urls": ["https://apptechpmua.com/innovatorExport/fetchExportList"],
  "scraped_at": "2026-10-12T12:02:52.413280+00:00",
  "run_id": "20261012T120022Z",
  "record_count": 4998,
  "warnings": [],
  "data": []
}
```

Each record carries a `record_type`, a `source_key`, and a
`source_payload_hash`; every dataset except `community_products` also carries a
`source_url` per record. Each file's `warnings` also holds a `sync:` entry with
the comparison counts.

On 2026-10-12 the exports held: `apptech_group_catalog` 18,
`apptech_innovators` 4,998, `apptech_innovations` 460,
`apptech_dashboard_summaries` 20, `programme_background` 1,
`community_products` 83, and `activity_posts` 12 records. The last three are
from earlier runs, because those datasets are currently blocked.

## Comparison with the previous run

The previous export in `output/` is the only state. Each run compares its
records with it by `source_key` and reports `new`, `changed`, `unchanged`, and
`retired` counts. `retrieved_at` is ignored in the comparison, so unchanged
dashboard summaries count as `unchanged`. A dataset's file is replaced only if
it succeeded. A dataset that would retire 50% or more of its previous records
(at least 10) fails unless `--allow-large-removal` is passed, and a previous
file that exists but cannot be read fails it too. With no previous file every
record counts as new. Version history is not kept locally.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/iCommunity"
```

All tests run offline against saved fixtures: parsers, previous-output
comparison, ordered concurrent fetching, and concurrent detail collection.
