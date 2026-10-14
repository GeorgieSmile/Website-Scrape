# Thailand Public Data Scrapers

A collection of Python scrapers for publicly available Thai websites and data
platforms. Each scraper collects structured data for research, analysis, and
future dashboard or database use.

## Projects

| Project | Data collected |
| --- | --- |
| [Visit Lumphun](ฝ่าย%203/Visit%20Lumphun/README.md) | Tourism sites, venues, travel services, products, and contacts |
| [Evaluate City](ฝ่าย%203/Evaluate%20City/README.md) | City indicators and published measurements |
| [Cultural Map](ฝ่าย%202/Cultural%20Map/README.md) | Cultural places, products, activities, recreation, and team data |
| [RinMP](ฝ่าย%202/RinMP/README.md) | Appropriate technologies, statistics, and owner profiles |
| [APPTech MRU](ฝ่าย%202/APPTech%20MRU/README.md) | Innovations and technology requirements |
| [PMUA Apptech](ฝ่าย%202/PMUA%20Apptech/README.md) | Products, technology centers, researchers, universities, and dashboards |
| [AtLocal](ฝ่าย%202/AtLocal/README.md) | Articles, cultures, products, entrepreneurs, festivals, and cultural areas |
| [iCommunity](ฝ่าย%202/iCommunity/README.md) | AppTech programmes, innovations, innovators, activities, and community products |
| [Learning Area Based](ฝ่าย%202/Learning%20Area%20Based/README.md) | PMUA area-based business listings and the source's summary statistics |
| [Wallet All](ฝ่าย%202/Wallet%20All/README.md) | Project-wide monthly household and community-business wallet aggregates |
| [Wallet Cluster](ฝ่าย%202/Wallet%20Cluster/README.md) | Current month-to-date wallet aggregates by business cluster |

## Common Features

The scrapers generally:

- collect only publicly available data
- stay inside documented source boundaries and keep request rates configurable
- normalize results into JSON exports
- report what changed since the previous run (ฝ่าย 2 compares with the previous
  JSON export; ฝ่าย 3 keeps SQLite history)
- validate pagination, duplicates, record counts, and source consistency
- use offline tests, and saved fixtures where a parser needs them, for parser
  validation (ฝ่าย 2)

Generated files in `output/` and `state/` are ignored by Git. Source code,
fixtures, tests, and documentation remain version controlled. The ฝ่าย 2
scrapers no longer read or write `state/`; any `state/` folder left in a
ฝ่าย 2 project is an unused leftover from the earlier SQLite design.

## Setup

The projects use the shared Python virtual environment in `.venv/` at the
repository root. Run every command from the repository root. Install the
dependencies for the scraper you want to run:

```bash
uv pip install --python .venv/bin/python \
  -r "ฝ่าย 2/AtLocal/requirements.txt"
```

Replace `AtLocal` with the relevant project directory.

## Run a Scraper

Each project has its own command-line options. See its README before starting a
full collection.

```bash
.venv/bin/python "ฝ่าย 2/AtLocal/scrape.py" --dataset all
.venv/bin/python "ฝ่าย 2/iCommunity/scrape.py"
```

Most scrapers support limited smoke tests, dataset selection, request delays,
and large-removal safeguards. Only iCommunity, RinMP, Learning Area Based,
Wallet All and Wallet Cluster accept `--output-dir`. The ฝ่าย 2 scrapers except
AtLocal, Learning Area Based and Wallet Cluster also support `--workers` for
concurrent fetching (Wallet All fetches months, the others detail pages).

## Tests

The ฝ่าย 2 scrapers each have an offline test suite. Run one at a time from the
repository root, because the project folders are not Python packages and a
single `unittest discover` at the root finds nothing:

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2/AtLocal"
```

Replace `AtLocal` with the relevant project directory. The ฝ่าย 3 scrapers have
no test suites.

The evidence manifest tool has its own test file:

```bash
.venv/bin/python -m unittest discover -s "ฝ่าย 2" -p "test_make_manifest_input.py"
```

## Evidence Manifest (ฝ่าย 2)

Each ฝ่าย 2 project has a committed `evidence.json` that declares its
`source_id` and, for each dataset, the `dataset_key`, `file`, `grain`, and
`identity_fields`. These are the human-reviewed parts of an evidence push.
After a full scraper run, write `output/manifest_input.json` for
`tools/evidence_push.py` in `aiat-dashboard-final`:

```bash
.venv/bin/python "ฝ่าย 2/make_manifest_input.py" "ฝ่าย 2/Cultural Map" --fetched-by <github-name>
```

`as_of` is each file's `scraped_at`, `fetched_at` is the earliest of them, and
`upstream` lists the files' source URLs. The tool writes nothing if `output/`
mixes runs, a declared file is missing, or an undeclared file (such as
`.DS_Store`) would be uploaded with the run. It prints the push command,
including `--run-id`, which is required because the folder name `output` is not
a run ID.
