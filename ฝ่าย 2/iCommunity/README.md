# iCommunity scraper

Incrementally collects public AppTech PMUA dashboard records and public
iCommunity WordPress/WooCommerce data. The complete collection contract is in
[SCRAPER_PLAN.md](SCRAPER_PLAN.md).

## Run

Use the workspace virtual environment:

```bash
../.venv/bin/python scrape.py
```

The default run discovers every current AppTech programme group, fetches the
selected public detail fields, collects summary snapshots, and collects the
WordPress/WooCommerce sources. It may take a long time because the AppTech
detail pages are fetched politely and sequentially.

Useful targeted commands:

```bash
# One group, one record, with details. This is a smoke test and never retires data.
../.venv/bin/python scrape.py --dataset apptech_innovators --groups 2 --limit 1

# Non-AppTech public sources only.
../.venv/bin/python scrape.py --dataset programme_background --dataset community_products --dataset activity_posts

# Skip detail pages while testing list discovery. Existing stored details are retained.
../.venv/bin/python scrape.py --dataset apptech_innovations --groups 2 --skip-details
```

Use `--delay`, `--page-size`, `--timeout`, `--output-dir`, `--state-path`,
and `--quiet` to control a run. `--allow-large-removal` is deliberately
required before a completed crawl can retire a large share of a dataset.

## Output and state

`output/` contains current clean JSON exports. `state/icommunity.sqlite3`
keeps current records, immutable changed versions, and run outcomes. The
scraper does not keep a full HTTP raw archive.

The AppTech summary counters are exported as source snapshots only. They are
not used to fill, reject, or merge AppTech list records.

Public contact information is redacted from WordPress/WooCommerce descriptive
HTML before persistence and export.

## Tests

```bash
../.venv/bin/python test_scrape.py
```
