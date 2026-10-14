# Learning Area Based scraper

A compact, incremental scraper for the public LeSuper PMUA area-based page,
[lesuper.app/opendata/pmua/area-based](https://lesuper.app/opendata/pmua/area-based).
One `GET` returns the whole dataset, so a run is a single request. It creates
two clean JSON exports and compares each run with the previous export.

It follows the source boundary of the page: the only request goes to
`https://lesuper.app/api/opendata/pmua/area-based`, the same endpoint the page
calls. Rows are exported exactly as the source sent them; nothing is imputed,
trimmed or renamed.

## Structure

```text
Learning Area Based/
├── README.md         # this file
├── scrape.py         # fetch, wrap, validation, comparison, JSON export
├── test_scrape.py    # offline checks
├── fixtures/         # a trimmed real API response used by the tests
├── requirements.txt  # requests
└── output/           # created at runtime; one JSON file per dataset
```

## How it works

`requests` provides a session with retry handling for temporary HTTP failures.
The response is a JSON object `{status, message, data, stats, filters}`. The
script checks `status == 200`, that `data` is a list, and that every row has a
non-empty, unique `id`. It also compares `stats.totalRecords` with the number of
rows and records a warning if they differ.

| Dataset | Source field | Stable key |
| --- | --- | --- |
| `area_based` | `data[]`, one row per listed business | `id` |
| `area_based_stats` | `stats` and `filters`, kept whole in a single record | the fixed key `area_based_stats` |

Each `area_based` record holds the 12 source fields unchanged (`id`,
`fiscalYear`, `projectName`, `researchUnit`, `region`, `province`, `district`,
`subDistrict`, `businessName`, `sequence`, `createdAt`, `updatedAt`) plus
`record_type`, `source_key` (the `id`) and `source_payload_hash`. A new source
field is kept and reported as a warning.

### Source findings worth knowing

- One row is a programme listing, not a unique legal business. Names repeat
  across years and projects, so identify rows by `id`, never by `businessName`.
- Blank geography is kept as an empty string: on 2026-10-14, 6 rows have no
  province, 14 no district and 17 no subdistrict.
- Four rows have the literal business name `[object Object]`. It is a source
  fault and is exported as sent.
- `createdAt` and `updatedAt` are the same value for every row, so they are a
  batch timestamp, not a per-business date.
- `stats.byBusinessType` has no matching field in the rows, so it cannot be
  checked against `area_based`. The other statistics reconcile with the rows.
- The page shows selected programme participants, not every business in the
  area. Whether the endpoint is meant for reuse is not confirmed with the owner.

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/Learning Area Based/requirements.txt"

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/Learning Area Based/scrape.py"

# Re-export one dataset.
.venv/bin/python "ฝ่าย 2/Learning Area Based/scrape.py" --dataset area_based_stats
```

Options:

- `--dataset NAME` (repeatable; default: both datasets)
- `--allow-large-removal`: required before a run can retire a large share of
  `area_based` (50% of the previous records, at least 10).
- `--user-agent NAME` (default `LearningAreaBasedCollector`)
- `--timeout`, `--output-dir`, `--quiet`: run controls.

A JSON run summary (per-dataset status, counts, warnings) is printed to
standard output. A dataset that fails is reported there and keeps its previous
file; the run then exits 1. If the request itself fails, both datasets fail.

## Output

`output/area_based.json` and `output/area_based_stats.json`. Every file has
the same top-level structure:

```json
{
  "schema_version": 1,
  "dataset": "area_based",
  "source_urls": ["https://lesuper.app/api/opendata/pmua/area-based"],
  "scraped_at": "2026-10-14T06:15:13.100000+00:00",
  "run_id": "20261014T061513Z",
  "record_count": 1002,
  "warnings": ["sync: {'new': 0, 'changed': 0, 'unchanged': 1002, 'retired': 0}"],
  "data": []
}
```

On 2026-10-14 the exports held 1,002 listings and one stats record. The
listings were identical to the 1,002 rows in the 2026-08-20 evidence run.

## Comparison with the previous run

The previous export in `output/` is the only state. Each run compares its
records with it by `source_key` and reports `new`, `changed`, `unchanged`, and
`retired` counts. A dataset's file is replaced only if it succeeded. A dataset
that would retire 50% or more of its previous records (at least 10) fails
unless `--allow-large-removal` is passed, a response with fewer than 500
listings fails, and a previous file that exists but cannot be read fails the
dataset too. With no previous file every record counts as new. Version history
is not kept locally; push each run with the evidence tools to keep it.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/Learning Area Based"
```

All tests run offline against the saved response: wrapping, source-fault
handling, comparison, atomic writes, and the full run with the request mocked.
