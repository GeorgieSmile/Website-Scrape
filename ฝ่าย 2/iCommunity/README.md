# iCommunity scraper

Collects public AppTech PMUA dashboard records and public iCommunity
WordPress/WooCommerce data, and compares each run with the previous export. The complete collection contract is in
[SCRAPER_PLAN.md](SCRAPER_PLAN.md).

## Known issue: WordPress datasets blocked

As of the live run on 2026-10-12 the three WordPress/WooCommerce datasets
(`programme_background`, `community_products`, `activity_posts`) do not work.
icommunity.in.th answers HTTP 200 with `{"message": "Access denied by Imunify360
bot-protection. IPs used for automation should be whitelisted"}`, so these
datasets fail and the run exits 1. Their existing output files are left
unchanged. The four AppTech datasets are not affected. No workaround is
implemented; the site owner would need to whitelist the scraper's IP.

## Run

Use the workspace virtual environment:

```bash
../.venv/bin/python scrape.py
```

The default run discovers every current AppTech programme group, fetches the
selected public detail fields, collects summary snapshots, and collects the
WordPress/WooCommerce sources. The AppTech detail pages (the bulk of the
requests) are fetched concurrently; the group lists, summaries, and WordPress
endpoints are fetched one request at a time.

Useful targeted commands:

```bash
# One group, one record, with details. This is a smoke test and writes nothing.
../.venv/bin/python scrape.py --dataset apptech_innovators --groups 2 --limit 1

# Non-AppTech public sources only.
../.venv/bin/python scrape.py --dataset programme_background --dataset community_products --dataset activity_posts

# Skip detail pages while testing list discovery. Details from the previous export are retained.
../.venv/bin/python scrape.py --dataset apptech_innovations --skip-details
```

Options:

- `--workers N` (1–8, default 4): concurrent AppTech detail-page fetches.
  `--workers 1` fetches one page at a time.
- `--delay SECONDS` (default 0): pause each worker takes after every request.
- `--groups` and `--limit`: smoke tests. They write nothing, for every dataset.
- `--page-size`, `--timeout`, `--output-dir`, and `--quiet` control a run.
- `--allow-large-removal` is deliberately required before a run can drop a
  large share of a dataset (50% of the previous records, at least 10).

A JSON run summary (per-dataset status, counts, warnings) is printed to
standard output.

## Output and state

`output/` contains current clean JSON exports, and they are the only state:
each run compares its records with the previous export by `source_key`, reports
`new`, `changed`, `unchanged`, and `retired` counts (also in each file's
`warnings` as a `sync:` entry), and replaces the file only if the dataset
succeeded. A previous file that exists but cannot be read fails that dataset.
Version history is not kept locally, and the scraper does not keep a full HTTP
raw archive.

The AppTech summary counters are exported as source snapshots only. They are
not used to fill, reject, or merge AppTech list records.

Public contact information is redacted from WordPress/WooCommerce descriptive
HTML before persistence and export.

## Tests

```bash
../.venv/bin/python test_scrape.py
```
