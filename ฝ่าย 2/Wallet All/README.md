# Wallet All scraper

A compact, incremental scraper for the project-wide wallet aggregates of the
LeSuper "ธุรกิจปันกัน 4" dashboard,
[lesuper.app/opendata/superapp/gen4/integrated/all](https://lesuper.app/opendata/superapp/gen4/integrated/all).
It asks the household and community-business endpoints for every month from
2024-06 to the current month-to-date and creates two clean JSON exports. Each
run is compared with the previous export.

It follows the source boundary of the page: every request goes to
`https://lesuper.app/api/opendata/superapp/gen4/hh` or `.../bu`, with the same
body the page sends. These endpoints only answer `POST`; the requests change
nothing. Each source response is exported exactly as sent.

## Structure

```text
Wallet All/
├── README.md         # this file
├── scrape.py         # month requests, wrapping, validation, comparison, JSON export
├── test_scrape.py    # offline checks
├── fixtures/         # API responses with made-up figures, used by the tests
├── requirements.txt  # requests
└── output/           # created at runtime; one JSON file per dataset
```

## How it works

`requests` provides a session with retry handling, including for `POST`. The
script first asks for the current month (`{"date": ""}`) and reads
`thisMonth` from the answer, so the last month to collect comes from the
source, not from the local clock. It then asks for each earlier month from
2024-06 with the month-end date (`{"date": "2024-09-30"}`), a small thread
pool fetching them in input order.

| Dataset | Endpoint | Stable key |
| --- | --- | --- |
| `wallet_household` | `POST /api/opendata/superapp/gen4/hh` | `hh:YYYY-MM` |
| `wallet_business` | `POST /api/opendata/superapp/gen4/bu` | `bu:YYYY-MM` |

Each record is one month. `response` holds the source answer unchanged
(`snapshot`, `snapshotDiff`, the series, and the Thai labels). The other
fields are `record_type`, `source_key`, `wallet` (`hh` or `bu`), `month`,
`request_date` (empty for the current month), `is_month_to_date`,
`has_snapshot`, `as_of_utc` (the source's own `snapshot.updatedAt`) and
`source_payload_hash`.

Every answer must report the month that was asked for; a different month fails
the dataset. A past month that comes back without a snapshot is kept with a
warning. A current month without a snapshot, fewer than 20 months, or a
duplicate key fails the dataset.

### Source findings worth knowing

- Treat the figures as restricted: the project's source profile puts this
  source in the restricted lane, and the page itself calls them preliminary
  figures for reference only. Do not quote them as final numbers or publish
  KPIs from them until the data owner confirms the definitions.
- The participant counts shown on the page (384 households, 271 businesses)
  are hard-coded in the page, not returned by the API.
- The current month changes during the day. Earlier months are frozen.
- For future days of the current month the household answer uses `0` and the
  business answer uses `null`. Both are kept as sent.
- Months before 2024-06 and months that have not started answer `200` with
  null values. The scraper does not ask for them. The 2026-08-20 evidence run
  also stored one empty future month-end answer per wallet type; this scraper
  does not.
- The source labels JSON as `text/html` or `text/plain`, so the body is
  decoded as UTF-8 directly.

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/Wallet All/requirements.txt"

# One month, one dataset. Smoke test: writes nothing.
.venv/bin/python "ฝ่าย 2/Wallet All/scrape.py" --dataset wallet_household --month 2024-09

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/Wallet All/scrape.py"
```

Options:

- `--dataset NAME` (repeatable; default: both datasets)
- `--month YYYY-MM` (repeatable): smoke test of those months only. It writes
  nothing and does not ask for the current month.
- `--workers N` (1–8, default 4): concurrent month requests.
- `--delay SECONDS` (default 0.25): pause each worker takes after every request.
- `--allow-large-removal`: required before a run can retire a large share of a
  dataset (50% of the previous records, at least 10).
- `--user-agent NAME` (default `WalletAllCollector`)
- `--timeout`, `--output-dir`, `--quiet`: run controls.

A full run makes about 60 requests and takes around 10 seconds. A JSON run
summary (per-dataset status, counts, warnings) is printed to standard output.
A dataset that fails is reported there and keeps its previous file; the run
then exits 1.

## Output

`output/wallet_household.json` and `output/wallet_business.json`. Every file
has the same top-level structure:

```json
{
  "schema_version": 1,
  "dataset": "wallet_household",
  "source_urls": ["https://lesuper.app/api/opendata/superapp/gen4/hh"],
  "scraped_at": "2026-10-14T06:15:21.366918+00:00",
  "run_id": "20261014T061515Z",
  "record_count": 29,
  "warnings": ["sync: {'new': 0, 'changed': 0, 'unchanged': 29, 'retired': 0}"],
  "data": []
}
```

On 2026-10-14 each file held 29 months, `2024-06` to `2026-10`. The 26 closed
months that overlap the 2026-08-20 evidence run were identical to it.

## Comparison with the previous run

The previous export in `output/` is the only state. Each run compares its
records with it by `source_key` and reports `new`, `changed`, `unchanged`, and
`retired` counts. A new month adds a record, and the current month shows as
`changed` while it is still filling. A dataset's file is replaced only if it
succeeded. A dataset that would retire 50% or more of its previous records (at
least 10) fails unless `--allow-large-removal` is passed, and a previous file
that exists but cannot be read fails it too. With no previous file every
record counts as new. Version history is not kept locally; push each run with
the evidence tools to keep it.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/Wallet All"
```

All tests run offline against saved responses: month arithmetic, wrapping,
month checks, ordered concurrent fetching, comparison, atomic writes, and the
full run with the requests mocked.
