# Wallet Cluster scraper

A compact, incremental scraper for the LeSuper "ธุรกิจปันกัน 4" wallet
aggregates by business cluster,
[lesuper.app/opendata/superapp/gen4/integrated/cluster](https://lesuper.app/opendata/superapp/gen4/integrated/cluster).
It asks the household and community-business endpoints for the current
month-to-date figures of each of the 7 business clusters and creates two clean
JSON exports. Each run is compared with the previous export.

It follows the source boundary of the page: every request goes to
`https://lesuper.app/api/opendata/superapp/gen4/cluster/hh` or `.../bu`, with
no body, as the page does. These endpoints only answer `POST`; the requests
change nothing. Each cluster's figures are exported exactly as sent.

**These endpoints take no date, so a run only sees the current month.** Each
run overwrites `output/`. History survives only if each run is pushed with the
evidence tools before the month ends.

## Structure

```text
Wallet Cluster/
├── README.md         # this file
├── scrape.py         # requests, wrapping, cross-checks, comparison, JSON export
├── test_scrape.py    # offline checks
├── fixtures/         # API responses with made-up figures, used by the tests
├── requirements.txt  # requests
└── output/           # created at runtime; one JSON file per dataset
```

## How it works

`requests` provides a session with retry handling, including for `POST`. The
two endpoints are requested one after the other. The server ignores any body,
so none is sent.

| Dataset | Endpoint | Stable key |
| --- | --- | --- |
| `wallet_cluster_household` | `POST /api/opendata/superapp/gen4/cluster/hh` | `hh:<cluster index>` |
| `wallet_cluster_business` | `POST /api/opendata/superapp/gen4/cluster/bu` | `bu:<cluster index>` |

Each record is one cluster. `snapshot` holds that cluster's source object
unchanged (`category` and the money, activity, debt, cash-flow and
satisfaction figures). The other fields are `record_type`, `source_key`,
`wallet`, `month` (the source's `thisMonth`), `thisMonthName`, `yesterdayName`,
`cluster_index`, `cluster_id` (`cat0` to `cat6` on the business endpoint, null
on the household endpoint, which sends no ids), `category`, `business_count`,
`as_of_utc` (that cluster's `updatedAt`) and `source_payload_hash`.

`business_count` is the cluster's count from the source's `categories` table.

The response lists the clusters in three places (`snapshotClusters`,
`clustersName`, `categories`; the business endpoint adds `clusters`). The
script cross-checks them: equal lengths, unique non-blank names, the same names
in the same order, and a count for every cluster. Any disagreement fails the
dataset rather than guessing which list is right.

The key leaves the month out, so a new day or a new month shows up as
`changed` clusters rather than as retired and new ones.

### Source findings worth knowing

- Treat the figures as restricted: the project's source profile puts this
  source in the restricted lane, and the page itself calls them preliminary
  figures for reference only. Do not quote them as final numbers or publish
  KPIs from them until the data owner confirms the definitions.
- Small cells: three clusters have 10 or fewer businesses (ร้านค้า 10,
  ร้านอาหาร/ที่พัก 8, ท่องเที่ยว 7 on 2026-10-14). Do not publish
  cluster-level figures without a suppression rule agreed with the owner.
- The cluster counts total 375, while the page text hard-codes 384
  households and 271 businesses. Which number means what is not confirmed.
- The page's "ผ่อนหนี้สิน" chart reads `totalDebtOriginated`, the same field as
  "สร้างหนี้". The API has a separate `totalDebtRepayed`; use the API values
  as exported, not the chart.
- `satisfied` holds counts of 0 to 3, so a percentage built from it can rest
  on one person.
- A cluster's `updatedAt` differs by a few seconds from the others in the same
  response. Use the latest one as the as-of time of the set.
- The source labels JSON as `text/html` or `text/plain`, so the body is
  decoded as UTF-8 directly.

## Install and run

Run these commands from the repository root using its `.venv`.

```bash
uv pip install --python .venv/bin/python -r "ฝ่าย 2/Wallet Cluster/requirements.txt"

# Full refresh: replaces each successfully validated dataset's output.
.venv/bin/python "ฝ่าย 2/Wallet Cluster/scrape.py"

# Re-export one dataset.
.venv/bin/python "ฝ่าย 2/Wallet Cluster/scrape.py" --dataset wallet_cluster_business
```

Options:

- `--dataset NAME` (repeatable; default: both datasets)
- `--allow-large-removal`: required before a run can retire half or more of
  the previous clusters of a dataset.
- `--user-agent NAME` (default `WalletClusterCollector`)
- `--timeout`, `--output-dir`, `--quiet`: run controls.

A JSON run summary (per-dataset status, counts, warnings) is printed to
standard output. A dataset that fails is reported there and keeps its previous
file; the run then exits 1.

## Output

`output/wallet_cluster_household.json` and
`output/wallet_cluster_business.json`. Every file has the same top-level
structure:

```json
{
  "schema_version": 1,
  "dataset": "wallet_cluster_business",
  "source_urls": ["https://lesuper.app/api/opendata/superapp/gen4/cluster/bu"],
  "scraped_at": "2026-10-14T06:15:26.705337+00:00",
  "run_id": "20261014T061526Z",
  "record_count": 7,
  "warnings": ["sync: {'new': 0, 'changed': 0, 'unchanged': 7, 'retired': 0}"],
  "data": []
}
```

On 2026-10-14 each file held 7 clusters for the month `2026-10`. The cluster
figures matched what the colleague's fetch script returned about 20 minutes earlier.

## Comparison with the previous run

The previous export in `output/` is the only state. Each run compares its
clusters with it by `source_key` and reports `new`, `changed`, `unchanged`, and
`retired` counts. A dataset's file is replaced only if it succeeded. Seven
clusters are below the usual 10-record floor, so here a dataset fails unless
`--allow-large-removal` is passed when half or more of the previous clusters
are missing; an empty answer fails too, and a previous file that exists but
cannot be read fails the dataset. With no previous file every record counts as
new. Version history is not kept locally; push each run with the evidence tools
to keep it.

## Tests

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/Wallet Cluster"
```

All tests run offline against saved responses: wrapping, the cross-checks,
comparison, atomic writes, and the full run with the requests mocked.
