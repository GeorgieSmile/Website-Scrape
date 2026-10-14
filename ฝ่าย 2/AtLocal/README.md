# AtLocal scraper

A compact, incremental scraper for the public At Local Thailand sites,
[atlocalthailand.com](https://atlocalthailand.com/) and
[market.atlocalthailand.com](https://market.atlocalthailand.com/). It creates
seven clean JSON exports and compares each run with the previous export.

It follows the approved source boundary: every request goes to those two
hosts. Map products and marketplace products stay in separate JSON files. The
scraper stores media URLs and filenames but does not download media.

## Structure

```text
AtLocal/
├── README.md         # this file
├── scrape.py         # collection, parsing, cleaning, comparison, JSON export
├── test_scrape.py    # offline parser and previous-output checks
├── fixtures/         # saved API and Next.js payload samples for the tests
├── requirements.txt  # requests and Beautiful Soup
└── output/           # created at runtime; one JSON file per dataset
```

## How it works

`requests` provides a session with retry handling for temporary HTTP failures.
`BeautifulSoup` parses server-rendered pages, and the marketplace products are
read from the embedded Next.js server payload. Requests are sequential.

| Dataset | Source | Stable ID |
| --- | --- | --- |
| Articles | `GET /api/front/articles`, with a detail API URL per record | `article_id` |
| Cultures | `GET /api/front/culture`, with a detail API URL per record | `culture_id` |
| Product map | `POST /api/s9001/get_items_by_search_filter_v1` (the listing API is the full record) | `map_product_id` |
| Marketplace products | homepage, then every discovered `/shop/{id}` page | `shop_id` + `marketplace_product_id` |
| Entrepreneurs | `GET /api/front/owners` | `entrepreneur_id` |
| Festivals | `/activity` links and `GET /api/front/highlights`, then each public `/festival/...` page | `festival_id` (the path) |
| 16 areas | `POST /api/s9001/get_map_info_all_v2` | `area_id` |

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/AtLocal/requirements.txt"

# Smoke test: writes nothing.
.venv/bin/python "ฝ่าย 2/AtLocal/scrape.py" --dataset all --limit 3

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/AtLocal/scrape.py" --dataset all
```

Options:

- `--dataset {all,articles,cultures,product_map,marketplace_products,entrepreneurs,festivals,areas_16}`
- `--delay SECONDS` (default 1.0): pause between requests. This scraper has
  no `--workers` option.
- `--limit N`: smoke test; writes nothing.
- `--allow-large-removal`: override the 25% retirement guard (see below).
- `--user-agent NAME` (default `AtLocalCollector`)
- `--quiet`: suppress progress messages on standard error.

A JSON run summary is printed to standard output. A network, parser, or
validation failure is reported per dataset and does not replace that dataset's
last successful export; the run then exits 1.

## Output

| File | Content | Records (2026-10-12) |
| --- | --- | --- |
| `articles.json` | Articles | 3 |
| `cultures.json` | Culture records | 18 |
| `product_map.json` | Map products (legacy map catalog) | 1,883 |
| `marketplace_products.json` | Marketplace products from every shop page | 636 |
| `entrepreneurs.json` | Entrepreneurs | 4 |
| `festivals.json` | Public festival pages | 8 |
| `areas_16.json` | The 16 cultural areas | 16 |

Every file has the same top-level structure:

```json
{
  "schema_version": 1,
  "dataset": "product_map",
  "source_urls": ["https://atlocalthailand.com/api/s9001/get_items_by_search_filter_v1"],
  "scraped_at": "2026-10-12T09:42:33.393363+00:00",
  "run_id": "20261012T094233Z",
  "record_count": 1883,
  "warnings": [],
  "metadata": {},
  "data": []
}
```

The legacy map catalog has records with an empty `item_name`. The scraper keeps
them because their source IDs, shop names, and locations are still published.
Each has a `Missing item_name in source` warning, and the scraper never
creates a replacement title.

## Comparison with the previous run

The previous `output/<dataset>.json` is the only state. Each run compares the
new records with it by record ID and prints `new`, `changed`, `unchanged`, and
`retired` counts per dataset in its JSON run summary. A dataset is refused,
leaving its previous file untouched, if more than 25% of the previous records
would disappear (override with `--allow-large-removal`) or if the previous file
exists but cannot be read. With no previous file, every record counts as new.
Version history is not kept locally.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/AtLocal"
```

All tests run offline. They cover API payload parsing, marketplace Next.js
payload extraction, festival discovery, 16-area joining, and the
previous-output comparison.
