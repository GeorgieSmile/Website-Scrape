# PMUA Apptech scraper

A compact, incremental scraper for the public PMUA AppTech platform at
[pmua-apptech.com](https://pmua-apptech.com/) — the national บพท./PMU-A
platform cataloguing university technology-transfer centers (ศูนย์), their
innovations (เทคโนโลยี / นวัตกรรม), expert researchers, partner universities,
and three public dashboards. It creates five clean JSON datasets and
compares each run with the previous export.

It follows the approved source boundary: every request goes to
`pmua-apptech.com`. The scraper collects text data only — it does not
download images or videos, follow external links, submit forms, or guess
record IDs.

## Structure

```text
PMUA Apptech/
├── scrape.py         # collection, parsing, cleaning, comparison, JSON export
├── test_scrape.py    # offline parser and previous-output checks
├── fixtures/         # saved live pages used by the offline tests
├── requirements.txt  # requests and Beautiful Soup
├── SCRAPER_PLAN.md   # investigated design and source findings
└── output/           # created at runtime; one JSON file per dataset
```

## Stack and collection method

`requests` provides a session with retry handling; `BeautifulSoup` parses the
server-rendered pages. Product and station detail pages, the product search
pages after page 1, and the researcher/university directory offsets are fetched
concurrently by a small `concurrent.futures` thread pool (one session per
worker thread); parsing, the area-name lookups, and all pagination checks stay
in the main thread, in page order. The dashboards and the area API stay
sequential.

| Dataset | Discovery | Detail strategy | Stable ID | Verified count |
| --- | --- | --- | --- | --- |
| Products | `/search?q=&page=N` (16/page; page reports the total) | GET `/product/show/{id}` | `product_id` | 1,172 |
| Stations | `/station/centers` (single page) | GET `/station/public_profile/{id}` | `station_id` | 31 |
| Researchers | `/welcome/researchers?per_page=<offset>` until a short/empty page | card fields only | `agent_id` | 539 |
| Universities | `/welcome/universities?per_page=<offset>` | card fields only | `university_id` | 66 |
| Dashboards | direct fetch × fiscal-year filters (`none`, `2025`, `2026`) | inline `const provData/provinceData` JSON extraction | page+filter | 7 artifacts |

### Source findings worth knowing

- **The sitemap.xml and homepage stat cards are stale** — the sitemap lists
  only 827 of 1,172 products and the homepage claims 54 universities / 596
  researchers versus the real 66 / 539. Neither is used as a data source.
- **Invalid product IDs return HTTP 302**, not 404, so product IDs come from
  search pagination, never blind enumeration.
- **Deployment areas** live on product pages as hidden numeric codes
  (`csv-provinces/csv-districts/csv-tumbons`), resolved to names via the
  public dictionaries at `/api/form/get_province`, `/get_district`, and
  `/get_tumbon` (three cached lookups total).
- **The dashboards embed pre-computed aggregates as inline JSON**; sums are
  validated against the on-page headline numbers during collection. The family
  dashboard's three economic totals are separate server-rendered `.eco-card`
  values, so `output/dashboards/family_*.json` also includes `economic_metrics`
  with raw THB text, parsed integer amounts, and a Net-vs-components check.
- The researcher directory is slow (~9 s/request server-side); its discovery
  and parsing share a single pagination pass for that reason. Its offsets are
  fetched in waves of `--workers` pages; pages after the first short or empty
  one are ignored.

## Install and run

```bash
../.venv/bin/pip install -r requirements.txt   # once
../.venv/bin/python scrape.py --dataset all --user-agent "PmuaApptechCollector"
```

Useful flags:

- `--dataset {all,products,stations,researchers,universities,dashboards}`
- `--workers N` — concurrent page fetches, 1–8 (default 4); `--workers 1` fetches one page at a time
- `--delay SECONDS` — pause each worker takes after every request (default 0)
- `--limit N` — smoke-test mode; fetches N records and writes nothing
- `--allow-large-removal` — override the 25% removal safety guard
- `--quiet` — suppress stderr progress lines

A full run is ≈ 1,350 requests. Sequentially at 0.4 s per request it took
about 20 minutes (the slow researcher directory alone accounts for ~7);
every run re-fetches everything. A failed dataset leaves its previous JSON
file untouched.

## Outputs

```text
output/products.json        1,172 records (description, TRL, IP, price,
                            funding, ROI/SROI, researcher/university links,
                            tags, resolved deployment areas)
output/stations.json         31 records (+ product_ids per station)
output/researchers.json     539 records
output/universities.json     66 records
output/dashboards/*.json     7 artifacts (verbatim provData blobs + computed
                            integrity checks vs on-page headlines)
```

The previous output files are the only state. Each run compares the new
records with them by ID and prints `new`, `updated`, `unchanged`, and
`removed` counts per dataset in its JSON run summary (dashboard artifacts
are compared file by file). A removal of more than 25% of an established
dataset (≥10 previous records) fails that dataset unless
`--allow-large-removal` is passed, and a previous file that exists but
cannot be read fails it too. Version history is not kept locally.

## Tests

```bash
../.venv/bin/python -m unittest discover -s . -v
```

All tests are offline and run against saved fixtures: cleaning rules, product
/station/directory parsers, dashboard blob extraction with anchor-sum checks
(12,063 innovators / 22,945 households / 1,172 mapped products), search
pagination guards (sequential and concurrent), directory waves, ordered
concurrent fetching, previous-output comparison, and duplicate-ID rejection.
