# Learning Dashboard scraper

A compact, incremental scraper for the public LeSuper PMUA dashboard
"แดชบอร์ดโครงการธุรกิจปันกัน 4",
[lesuper.app/opendata/pmua/dashboard](https://lesuper.app/opendata/pmua/dashboard).
One `POST` returns the whole dashboard, so a run is a single request. It
creates three clean JSON exports and compares each run with the previous
export.

It follows the source boundary of the page: the only request goes to
`https://lesuper.app/api/opendata/pmua`, the same endpoint the page calls, with
no body. The endpoint only answers `POST` (a `GET` returns 405); the request
changes nothing. Every table row and impact object is exported with the
source's values unchanged.

## Structure

```text
Learning Dashboard/
├── README.md         # this file
├── scrape.py         # fetch, split, cross-checks, comparison, JSON export
├── test_scrape.py    # offline checks
├── fixtures/         # a saved real API response used by the tests
├── requirements.txt  # requests
└── output/           # created at runtime; one JSON file per dataset
```

## How it works

`requests` provides a session with retry handling, including for `POST`. The
response has seven keys, which the script splits into three datasets:

| Dataset | Source keys | Stable key |
| --- | --- | --- |
| `dashboard_counts` | `provinces`, `entityTypes`, `categories`, `geography`: one record per table row | `<table>:<label>` |
| `dashboard_impact` | `geographyImpact`: one record per region | `impact:<region>` |
| `dashboard_totals` | `impactSummary` and `excludedResourceExpense`: one record each | `impact_summary`, `excluded_resource_expense` |

Each count table arrives as a display header row followed by `[label, count]`
rows. The header is not a data row: its two texts are kept on every record of
the table as `dimension_label` and `count_label`, and `row_index` keeps the
source order. Each impact record holds the four source fields
(`localEmployeeAmount`, `localEmployeeExpense`, `localResourceConsumption`,
`localResourceExpense`) unchanged, plus `region` and `region_index`. Every
record also has `record_type`, `source_key` and `source_payload_hash`.

The six impact objects carry no region name. The page labels them by position
with a fixed list in its own code (North, Central, Northeast, West, East,
South), and the script uses the same list. It then checks that the region
names in `geography` are exactly those six; a different set fails the dataset
rather than guessing, and a different order is kept with a warning. The
`totals` dataset is checked against the regions: if an `impactSummary` total
differs from the sum of the six regions, the run records a warning. The count
tables are checked to add up to the same total; a difference is also a
warning.

### Source findings worth knowing

- Treat the figures as programme aggregates, not as the area's economy. The
  page describes them as the overview of selected community businesses for
  2566–2567, and its own notice says they are a preliminary reference and
  should not be used for financial decisions outside the project's target
  group. The project's source catalog lists this source as still needing review.
- The four count tables each total 375 businesses on 2026-10-14. They are
  different breakdowns of the same count, so do not add them together.
- The impact values are monthly (people, baht or kilograms per month). The
  page shows resource consumption in tonnes, as the kilogram value divided
  by 1,000.
- `impactSummary.totalEmplyeeAmount` is spelled that way in the source and is
  kept as sent. Its four totals equal the sum of the six regions.
- `excludedResourceExpense` (a region and an amount) is not used by the page,
  and it is not part of `totalResourceExpense`. What it means is not explained
  by the source; it is exported as sent and kept separate.
- The response has no observation date and no fiscal-year field, so the scrape
  time is the only date, and years cannot be compared.
- The category name `สินค้าอุปโภคสมุนไพรและเวชสำอางค์` is shown on the page
  without the final `ค์`. The export keeps the API's spelling.
- The source labels JSON as `text/html` or `text/plain`, so the body is
  decoded as UTF-8 directly.

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/Learning Dashboard/requirements.txt"

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/Learning Dashboard/scrape.py"

# Re-export one dataset.
.venv/bin/python "ฝ่าย 2/Learning Dashboard/scrape.py" --dataset dashboard_impact
```

Options:

- `--dataset NAME` (repeatable; default: all three datasets)
- `--allow-large-removal`: required before a run can retire half or more of a
  dataset's previous records.
- `--user-agent NAME` (default `LearningDashboardCollector`)
- `--timeout`, `--output-dir`, `--quiet`: run controls.

A JSON run summary (per-dataset status, counts, warnings) is printed to
standard output. A dataset that fails is reported there and keeps its previous
file; the run then exits 1. If the request itself fails, every selected
dataset fails.

## Output

`output/dashboard_counts.json`, `output/dashboard_impact.json` and
`output/dashboard_totals.json`. Every file has the same top-level structure:

```json
{
  "schema_version": 1,
  "dataset": "dashboard_impact",
  "source_urls": ["https://lesuper.app/api/opendata/pmua"],
  "scraped_at": "2026-10-14T06:30:55.100000+00:00",
  "run_id": "20261014T063055Z",
  "record_count": 6,
  "warnings": ["sync: {'new': 0, 'changed': 0, 'unchanged': 6, 'retired': 0}"],
  "data": []
}
```

On 2026-10-14 the exports held 82 count records (66 provinces, 3 entity types,
7 categories, 6 regions), 6 impact records and 2 totals records. Rebuilt into
the original tables and objects, they were identical to the response in the
2026-08-20 evidence run.

## Comparison with the previous run

The previous export in `output/` is the only state. Each run compares its
records with it by `source_key` and reports `new`, `changed`, `unchanged`, and
`retired` counts. A dataset's file is replaced only if it succeeded. A dataset
that would retire half or more of its previous records fails unless
`--allow-large-removal` is passed, and so does a dataset below its minimum
size (50 count records, 6 impact records, 1 totals record) or a previous file
that exists but cannot be read. With no previous file every record counts as
new. Version history is not kept locally; push each run with the evidence
tools to keep it.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/Learning Dashboard"
```

All tests run offline against the saved response: splitting, region labelling,
cross-checks, source-fault handling, comparison, atomic writes, and the full
run with the request mocked.
