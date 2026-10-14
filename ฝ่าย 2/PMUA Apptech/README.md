# PMUA Apptech scraper

A compact, incremental scraper for the public PMUA AppTech platform at
[pmua-apptech.com](https://pmua-apptech.com/), the national บพท./PMU-A
platform cataloguing university technology-transfer centers (ศูนย์), their
innovations (เทคโนโลยี / นวัตกรรม), expert researchers, partner universities,
and three public dashboards. It creates five clean JSON datasets and compares
each run with the previous export.

It follows the approved source boundary: every request goes to
`pmua-apptech.com`. The scraper collects text data only. It does not download
images or videos, follow external links, submit forms, or guess record IDs.

## Structure

```text
PMUA Apptech/
├── README.md         # this file
├── scrape.py         # collection, parsing, cleaning, comparison, JSON export
├── test_scrape.py    # offline parser and previous-output checks
├── fixtures/         # saved live pages used by the offline tests
├── requirements.txt  # requests and Beautiful Soup
└── output/           # created at runtime; one JSON file per dataset
```

## How it works

`requests` provides a session with retry handling; `BeautifulSoup` parses the
server-rendered pages. Product and station detail pages, the product search
pages after page 1, and the researcher/university directory offsets are fetched
concurrently by a small `concurrent.futures` thread pool (one session per
worker thread); parsing, the area-name lookups, and all pagination checks stay
in the main thread, in page order. The dashboards and the area API stay
sequential.

| Dataset | Discovery | Detail strategy | Stable ID |
| --- | --- | --- | --- |
| Products | `/search?q=&page=N` (16/page; page reports the total) | GET `/product/show/{id}` | `product_id` |
| Stations | `/station/centers` (single page) | GET `/station/public_profile/{id}` | `station_id` |
| Researchers | `/welcome/researchers?per_page=<offset>` until a short/empty page | card fields only | `agent_id` |
| Universities | `/welcome/universities?per_page=<offset>` | card fields only | `university_id` |
| Dashboards | direct fetch × fiscal-year filters (`none`, `2025`, `2026`) | inline `const provData/provinceData` JSON extraction | page+filter |

### Source findings worth knowing

- **The sitemap.xml and homepage stat cards are stale.** The sitemap lists only
  part of the products, and the homepage claims different university and
  researcher counts than the real directories. Neither is used as a data
  source.
- **Invalid product IDs return HTTP 302**, not 404, so product IDs come from
  search pagination, never blind enumeration.
- **Deployment areas** live on product pages as hidden numeric codes
  (`csv-provinces/csv-districts/csv-tumbons`), resolved to names via the public
  dictionaries at `/api/form/get_province`, `/get_district`, and `/get_tumbon`
  (three cached lookups total).
- **The dashboards embed pre-computed aggregates as inline JSON**; sums are
  validated against the on-page headline numbers during collection. The family
  dashboard's three economic totals are separate server-rendered `.eco-card`
  values, so `output/dashboards/family_*.json` also includes `economic_metrics`
  with raw THB text, parsed integer amounts, and a Net-vs-components check.
- The researcher directory is slow (about 9 s per request server-side), so its
  discovery and parsing share a single pagination pass. Its offsets are fetched
  in waves of `--workers` pages; pages after the first short or empty one are
  ignored. The server appears to handle these slow requests largely one at a
  time, so more workers help less here than for the other datasets.

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/PMUA Apptech/requirements.txt"

# Smoke test: writes nothing.
.venv/bin/python "ฝ่าย 2/PMUA Apptech/scrape.py" --dataset products --limit 5

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/PMUA Apptech/scrape.py" --dataset all
```

Options:

- `--dataset {all,products,stations,researchers,universities,dashboards}`
- `--workers N` (1–8, default 4): concurrent page fetches. `--workers 1`
  fetches one page at a time.
- `--delay SECONDS` (default 0): pause each worker takes after every request.
- `--limit N`: smoke test; fetches N records and writes nothing.
- `--allow-large-removal`: override the 25% removal guard (see below).
- `--user-agent NAME` (default `PmuaApptechCollector`)
- `--progress-every N`, `--quiet`: progress control on standard error.

A full run is roughly 1,350 requests and takes roughly 6 minutes with the default
4 workers (about 20 minutes when run sequentially at 0.4 s per request). Every
run re-fetches everything. A JSON run summary is printed to standard output. A
failed dataset leaves its previous JSON file untouched, and the run then exits
1.

## Output

| File | Content | Records (2026-10-12) |
| --- | --- | --- |
| `output/products.json` | Description, TRL, IP, price, funding, ROI/SROI, researcher and university links, tags, resolved deployment areas | 1,192 |
| `output/stations.json` | Technology centers, with `product_ids` per station | 31 |
| `output/researchers.json` | Researcher cards | 541 |
| `output/universities.json` | University cards | 66 |
| `output/dashboards/*.json` | Verbatim `provData` blobs plus computed integrity checks against the on-page headlines | 7 artifacts |

The four directory exports share this envelope:

```json
{
  "schema_version": 1,
  "dataset": "product",
  "source_base_url": "https://pmua-apptech.com/",
  "scraped_at": "2026-10-12T12:01:36.375554+00:00",
  "run_id": "20261012T120015Z",
  "record_count": 1192,
  "warnings": [],
  "data": []
}
```

Each dashboard artifact is a single record instead. It starts with the same
`schema_version`, `dataset`, `source_base_url`, `scraped_at`, `run_id`, and
`warnings`, followed by the dashboard content (`record_id`, `record_type`,
`dashboard`, `year_filter`, `headline`, `prov_data`, `integrity_checks`,
`validation_warnings`, and `economic_metrics` for the family dashboard). It has
no `record_count` or `data`.

## Comparison with the previous run

The previous output files are the only state. Each run compares the new records
with them by ID and prints `new`, `updated`, `unchanged`, and `removed` counts
per dataset in its JSON run summary (dashboard artifacts are compared file by
file). A removal of more than 25% of an established dataset (at least 10
previous records) fails that dataset unless `--allow-large-removal` is passed,
and a previous file that exists but cannot be read fails it too. With no
previous file every record counts as new. Version history is not kept locally.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/PMUA Apptech"
```

All tests run offline against saved fixtures: cleaning rules,
product/station/directory parsers, dashboard blob extraction with anchor-sum
checks, search pagination guards (sequential and concurrent), directory waves,
ordered concurrent fetching, previous-output comparison, and duplicate-ID
rejection.
