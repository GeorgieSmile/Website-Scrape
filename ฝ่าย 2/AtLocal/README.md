# At Local scraper

This scraper collects public At Local Thailand articles, cultures, map products,
marketplace products, entrepreneurs, festivals, and the 16 cultural areas.

Map products and marketplace products stay in separate JSON files. The scraper
stores media URLs and filenames but does not download media.

## Run

```bash
../.venv/bin/python scrape.py --dataset all --user-agent "AtLocalCollector"
```

Useful options:

- `--dataset {all,articles,cultures,product_map,marketplace_products,entrepreneurs,festivals,areas_16}`
- `--delay SECONDS`, default `1.0`
- `--limit N` for a read-only smoke test
- `--allow-large-removal` to override the 25% retirement guard

The scraper writes current data to `output/`. Each run compares the new records
with the previous output file (matched by record ID) and prints new, changed, unchanged, and retired counts in its JSON run summary.
A dataset is refused, leaving its previous file untouched, if more than 25% of
the previous records would disappear (override with `--allow-large-removal`) or
if the previous file exists but cannot be read. Version history is not kept
locally.

## Verified first run

The first complete collection on 2026-08-25 produced 3 articles, 18 culture
records, 1,883 map products, 543 marketplace products, 4 entrepreneurs, 8
public festival pages, and 16 areas.

The legacy map catalog has 78 records with an empty `item_name`. The scraper
keeps them because their source IDs, shop names, and locations are still
published. Each has a `Missing item_name in source` warning. It never creates a
replacement title.

## Test

```bash
../.venv/bin/python -m unittest -v
```

All tests run offline. They cover API payload parsing, marketplace Next.js
payload extraction, festival discovery, 16-area joining, and the previous-output
comparison.
