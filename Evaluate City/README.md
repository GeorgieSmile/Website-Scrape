# Evaluate City scraper

This project collects the public city-indicator data from
<https://evaluatethecity.netlify.app/> and prepares a normalized JSON export
for a later dashboard database import.

The scraper is intentionally one main Python file. SQLite is used locally for
incremental state and history; it is not a production dashboard database.

## Website structure and stack

The current site is a static Thai-language dashboard rather than a React or
server-rendered data application.

| Site part | Current finding |
|---|---|
| Hosting | Netlify at `evaluatethecity.netlify.app` |
| Page response | One homepage HTML document, about 49 KB at the time of inspection |
| Data source | One inline JavaScript block, about 31 KB, containing the published arrays |
| External assets | Tailwind CDN and Chart.js CDN for styling and charts |
| Data requests | No `fetch`, XHR, or public API request was observed for the dashboard data |
| User interface | City selector, comparison selector, category tabs, score selector, and two Chart.js canvases |
| Current shape | 18 municipalities, 39 raw indicators, and three categories: environment, infrastructure, and society/economy |
| Crawl guidance | `/robots.txt` currently returns a Netlify 404 page; the scraper still uses conservative one-request-per-run behavior |

The source script currently publishes these four data structures:

- `cityNames`: the ordered list of municipalities.
- `geoData`: seven environment arrays.
- `infData`: fifteen infrastructure arrays.
- `pplData`: seventeen society/economy arrays.

Each metric array is aligned by index with `cityNames`. For example, index 0
in `geoData.pm25` belongs to index 0 in `cityNames`.

The dashboard also calculates quintile-style scores for presentation. This
project stores the raw published measurements and does not make those derived
scores canonical.

## Scraping method

The scraper does not use browser automation, DOM selectors, chart pixels, or
remote JavaScript execution. It uses this pipeline:

```text
HTTP GET homepage
  -> locate the inline data script
  -> extract balanced JavaScript array/object literals
  -> parse only cityNames, geoData, infData, and pplData with ChompJS
  -> clean text and validate the complete data shape
  -> calculate a canonical SHA-256 hash of the parsed data
  -> compare the hash with the previous successful snapshot
  -> normalize city × metric observations
  -> update SQLite and write output/latest.json
```

Every run makes one normal GET because the page is small and the parsed-data
hash is the authoritative change detector. The request includes
`Cache-Control: no-cache` as a cache revalidation hint, but the scraper does not
depend on server validators or a special bypass mode. A repeated run still
downloads and validates the page, then records `unchanged_data` without adding
duplicate observation versions when the parsed hash is the same.

The balanced-literal extractor understands nested arrays/objects, quoted
strings, line comments, and block comments. ChompJS converts the extracted
literals into Python values; the scraper never evaluates the downloaded page
code. ChompJS is documented as a parser for JavaScript objects embedded in web
pages: <https://pypi.org/project/chompjs/>.

## Files

```text
scrape.py          # scraper, parser, catalog, validation, SQLite, CLI, self-tests
requirements.txt   # pinned runtime dependency
README.md           # this documentation
SCRAPER_PLAN.md    # approved design plan
output/            # generated latest.json and optional raw HTML
state/             # generated SQLite database and SQLite WAL files
```

`output/` and `state/` are runtime artifacts and should not be treated as
source code. The scraper creates them automatically.

## Setup and running

The project uses the existing virtual environment at `Website Scrape/.venv`.
Install the pinned dependency with `uv pip`:

```bash
uv pip install \
  --python "Website Scrape/.venv/bin/python" \
  -r "Website Scrape/Evaluate City/requirements.txt"
```

Run the normal incremental collection from the workspace root:

```bash
"Website Scrape/.venv/bin/python" \
  "Website Scrape/Evaluate City/scrape.py"
```

Useful commands:

```bash
# Keep the exact downloaded HTML for audit/debugging.
"Website Scrape/.venv/bin/python" \
  "Website Scrape/Evaluate City/scrape.py" --save-html

# Run parser, cleaning, catalog, and validation tests without network access.
"Website Scrape/.venv/bin/python" \
  "Website Scrape/Evaluate City/scrape.py" --self-test
```

For editor/static checks, point Pyright/Pylance at the same project
interpreter so the installed ChompJS package is resolved:

```bash
"Website Scrape/.venv/bin/ruff" check \
  "Website Scrape/Evaluate City/scrape.py"
"Website Scrape/.venv/bin/ruff" format --check \
  "Website Scrape/Evaluate City/scrape.py"
"Website Scrape/.venv/bin/pyright" \
  --pythonpath "Website Scrape/.venv/bin/python" \
  "Website Scrape/Evaluate City/scrape.py"
```

The default paths can be changed with `--output-dir` and `--state-path`.
`--timeout` changes the HTTP timeout, and `--user-agent` supplies an approved
contact-aware user-agent when the project has one.

## Output design

The primary export is `output/latest.json`. It uses a normalized,
database-ready shape:

```json
{
  "schema_version": 1,
  "parser_version": "evaluate-city-v2",
  "run_id": 3,
  "source": {
    "url": "https://evaluatethecity.netlify.app/",
    "snapshot_id": "sha256:...",
    "data_observed_at": "2026-08-13T14:13:20+00:00",
    "last_checked_at": "2026-08-13T14:13:31+00:00"
  },
  "cities": [
    {
      "city_id": "city_26c6df3cae7867b9f9f7",
      "name_th": "เทศบาลนครลำปาง",
      "source_index": 0
    }
  ],
  "metrics": [
    {
      "metric_id": "environment.pm25",
      "category_id": "environment",
      "source_key": "pm25",
      "label_th": "PM2.5",
      "display_unit": "µg/m³",
      "description_th": "เฉลี่ยรายปี",
      "value_type": "number",
      "nullable": true,
      "hard_min": 0,
      "hard_max": null
    }
  ],
  "observations": [
    {
      "city_id": "city_26c6df3cae7867b9f9f7",
      "metric_id": "environment.pm25",
      "value": 25
    }
  ],
  "warnings": []
}
```

The complete current export contains 18 city records, 39 metric definitions,
and 702 observations (`18 × 39`). A dashboard importer can load these as
`cities`, `metrics`, and `observations` tables. Each observation is identified
by the pair `(city_id, metric_id)`.

The export intentionally contains raw values, including source `null` values.
It does not contain the dashboard's derived 1–5 ranking scores or overall
category scores.

## SQLite state and history

The default state file is `state/evaluate_city.sqlite3`.

| Table | Stored information |
|---|---|
| `runs` | Every attempt, including start/end time, HTTP status, parsed-data hash, outcome, warnings, errors, and optional saved-HTML path |
| `snapshots` | One canonical JSON copy of each unique parsed source dataset, keyed by SHA-256 hash |
| `observations` | Latest value for each city × metric pair, value hash, active state, and first/last/changed timestamps |
| `observation_versions` | Immutable `inserted`, `updated`, and `retired` events for individual observations |

State databases created by an older scraper version may retain an unused
legacy column in `runs`; the current code neither reads nor writes it. New
databases use the schema shown above.

On a first successful run, the database contains 702 current observations and
702 inserted history events.

If one value changes—for example, Lampang PM2.5 changes from 25 to 27—only
`environment.pm25` for Lampang receives an `updated` history event. Unchanged
values receive a new `last_seen_at` but no duplicate history event.

A record is retired only after a complete, successfully validated scrape
confirms that its city or metric is no longer published. A failed or partially
parsed run never retires or replaces clean data.

## Cleaning and validation

The rules are centralized in `validate_dataset` and `METRIC_CATALOG`:

- City labels are Unicode NFC-normalized; non-breaking spaces are replaced; surrounding and repeated whitespace is cleaned.
- Numeric source values are preserved exactly as parsed. The scraper does not impute, round, or coerce questionable values.
- Every city name must be non-empty and unique after normalization.
- Every catalogued metric must be present as an array with exactly one value per city.
- Values must be finite numbers or `null`; booleans, strings, NaN, and infinity are rejected.
- Structurally impossible values fail validation, including negative counts/distances and percentages outside 0–100.
- Soft plausibility limits produce warnings without changing the source value.
- A transition from a populated value to `null` produces a warning; `null` is never treated as zero.
- New, removed, or renamed source metrics fail validation until the catalog is reviewed.

The catalog contains the current seven environment, fifteen infrastructure, and
seventeen society/economy metrics with their source keys, Thai labels, display
units, descriptions, and validation limits.

## Incremental outcomes

Each run is recorded with one of these outcomes:

- `success`: a new parsed source dataset was validated and observations were applied.
- `unchanged_data`: the page was fetched and validated, but the canonical parsed data hash did not change; no observation versions were added.
- `failed`: fetching, parsing, validation, or output writing failed; the prior clean export remains in place.

The scraper retries temporary 429/5xx responses with backoff and respects a
numeric or HTTP-date `Retry-After` header. It only requests the public homepage
and does not follow third-party links or attempt authentication.

## Code guide

The single script is organized from source description to runtime behavior:

| Area | Main functions/data | Responsibility |
|---|---|---|
| Catalog | `MetricDefinition`, `METRIC_CATALOG` | Define stable metric IDs, labels, units, and validation limits |
| Cleaning and parsing | `clean_text`, `extract_js_literal`, `find_data_script`, `parse_raw_data` | Read only the published JavaScript literals without execution |
| Validation | `validate_dataset` | Enforce complete city/metric alignment and value quality rules |
| HTTP | `fetch_page`, `parse_retry_after` | Unconditional GET, response decoding, retries, and error handling |
| State | `open_database`, `save_snapshot`, `apply_observations` | Maintain SQLite snapshots, latest observations, and immutable changes |
| Export | `build_export`, `write_json_atomic` | Produce a safe, database-ready `latest.json` |
| CLI/tests | `run_pipeline`, `main`, `ScraperTests` | Run collection, expose options, and test core behavior |

## Maintenance

If the site changes its data contract, the scraper should fail visibly rather
than silently collect the wrong values. Review these items together:

1. Inspect the homepage source and confirm whether the data is still in an
   inline script.
2. Update `DATA_VARIABLES`, `CATEGORIES`, and `METRIC_CATALOG` if source names
   or metrics change.
3. Add or update self-tests for the changed structure.
4. Run `--save-html`, inspect `latest.json`, and compare city/metric counts,
   source hashes, and warnings.
5. Increment `PARSER_VERSION` when the output or parsing contract changes.

The scraper should be run at a polite schedule chosen by the project team.
Before production use, replace the default user-agent with an approved team
contact and confirm that collecting the public data is permitted for the
intended use.

## Validation completed

The implementation was validated against the current public page and the
existing saved source snapshot:

- `--self-test`: six parser, catalog, cleaning, validation, and stable-ID tests passed.
- Saved snapshot parse: 18 cities, 39 metrics, no validation errors, and the same canonical source hash as the existing reference run.
- Live run: HTTP 200, 18 cities, and 702 observations processed successfully.
- Repeated live run: HTTP 200, the same parsed-data hash, and no new observation versions created.
- Simulated metric change: one changed city × metric value created exactly one `updated` history event; the other 701 observations remained unchanged.
- Simulated malformed source: the run failed without replacing the previous valid `latest.json`.
