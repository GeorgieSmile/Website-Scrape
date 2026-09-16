# Thailand Public Data Scrapers

A collection of Python scrapers for publicly available Thai websites and data
platforms. Each scraper collects structured data for research, analysis, and
future dashboard or database use.

## Projects

| Project | Data collected |
| --- | --- |
| [Visit Lumphun](Visit%20Lumphun/README.md) | Tourism sites, venues, travel services, products, and contacts |
| [Evaluate City](Evaluate%20City/README.md) | City indicators and published measurements |
| [Cultural Map](Cultural%20Map/README.md) | Cultural places, products, activities, recreation, and team data |
| [RinMP](RinMP/README.md) | Appropriate technologies, statistics, and owner profiles |
| [APPTech MRU](APPTech%20MRU/README.md) | Innovations and technology requirements |
| [PMUA Apptech](PMUA%20Apptech/README.md) | Products, technology centers, researchers, universities, and dashboards |
| [AtLocal](AtLocal/README.md) | Articles, cultures, products, entrepreneurs, festivals, and cultural areas |
| [iCommunity](iCommunity/README.md) | AppTech programmes, innovations, innovators, activities, and community products |

## Common Features

The scrapers generally:

- collect only publicly available data
- use conservative request delays and documented source boundaries
- normalize results into JSON exports
- track changes and run history in SQLite
- validate pagination, duplicates, record counts, and source consistency
- use saved fixtures and offline tests for parser validation

Generated files in `output/` and `state/` are ignored by Git. Source code,
fixtures, tests, and documentation remain version controlled.

## Setup

The projects use the shared Python virtual environment in `.venv/`. Install the
dependencies for the scraper you want to run:

```bash
uv pip install --python .venv/bin/python \
  -r "AtLocal/requirements.txt"
```

Replace `AtLocal` with the relevant project directory.

## Run a Scraper

Each project has its own command-line options. See its README before starting a
full collection.

```bash
.venv/bin/python "AtLocal/scrape.py" --dataset all
.venv/bin/python "iCommunity/scrape.py"
```

Most scrapers support limited smoke tests, dataset selection, request delays,
custom output paths, and large-removal safeguards.

## Tests

Run the complete offline test suite from the repository root:

```bash
.venv/bin/python -m unittest discover
```

To test one scraper:

```bash
.venv/bin/python -m unittest discover -s "AtLocal"
```
