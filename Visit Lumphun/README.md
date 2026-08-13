# Visit Lamphun Public Content Scraper

This folder contains an incremental scraper for the public content on
[Visit Lamphun](https://visit-lamphun.web.app). It collects the five public
subpages below and writes one cleaned JSON output per page:

- `/app/homepage`
- `/app/recommend`
- `/app/travel`
- `/app/komepage`
- `/app/contact`

The scraper is intentionally one readable Python file. Runtime dependencies are
Tree-sitter and the maintained JavaScript grammar; Ruff and Pyright are also
pinned in `requirements.txt` for formatting and static type checks.

## Website structure and stack

Visit Lamphun is a client-side React single-page application built with Vite.
The server response for each route is a small HTML shell containing a root
`<div>` and an `index-*.js` application bundle. The visible static content is
stored as JavaScript object and array literals inside that public bundle.

The site uses **Firebase Storage** to host SVG and image assets.

The site map is not a conventional Google Maps, Leaflet, or Mapbox map. It is
an SVG route image with 12 transparent React click regions. Each region opens
a station view that lists nearby venues and links to their Google Maps URLs.

| Page | Content collected | Current observed volume |
| --- | --- | --- |
| Homepage | Stations, venue cards, image URLs, and Google Maps URLs | 12 stations, 97 venues |
| Recommend | `ของดี`, `ที่เด่น`, and `คนดัง` cards | 7, 3, and 3 items |
| Travel | Train, tourism tram, and other transport records | 8, 2, and 3 services |
| Komepage | Lantern-production community groups | 10 groups |
| Contact | Emergency numbers, service contacts, and public resources | 6 emergency numbers, 3 service contacts |

These counts are a live-site baseline, not a fixed contract: content can
change when the site is updated.

## Scraping method

The scraper does **not** use browser automation or execute remote JavaScript.
Instead it:

1. Requests all five public routes to confirm they are available.
2. Finds the Vite `index-*.js` bundle from the homepage HTML.
3. Downloads and hashes that public bundle.
4. Parses the complete JavaScript bundle into an AST with Tree-sitter.
5. Finds the published variable assignments and converts only their literal
   object, array, string, number, boolean, and null values to Python data.
6. Converts the site data into the page-specific JSON schemas.
7. Stores normalized records and their history in SQLite, then exports the
   latest active records as five JSON files.

This is more reliable than reading visible page text because the HTML response
does not contain the React-rendered cards. It is also lighter and less fragile
than Playwright selectors for a site whose primary public data is already
published in its bundle.

The scraper does not follow Google Maps, Facebook, LINE, QR-code, or other
third-party destinations. It saves a third-party URL only when Visit Lamphun
publishes that URL in its own public data.

## Files and outputs

```text
scrape.py                       # scraper implementation
SCRAPER_PLAN.md                 # agreed scraper design
state/visit_lamphun.sqlite3     # incremental state and version history
output/homepage.json            # station and venue data
output/recommend.json           # three recommendation categories
output/travel.json              # transport schedules and providers
output/komepage.json            # lantern production groups
output/contact.json             # emergency and public service contacts
```

Every output begins with common scrape metadata:

```json
{
  "schema_version": 1,
  "page_id": "homepage",
  "source_url": "https://visit-lamphun.web.app/app/homepage",
  "scraped_at": "2026-08-13T09:11:12+00:00",
  "bundle_sha256": "...",
  "warnings": [],
  "data": {}
}
```

The data contains Thai (`TH`), English (`EN`), and Chinese (`CN`) values when
the website publishes them. A representative homepage venue is:

```json
{
  "venue_id": "3bb3659bfbb114d4fbb3",
  "name": {
    "TH": "วัดพระธาตุหริภุญชัย",
    "EN": "Wat Phra That Hariphunchai",
    "CN": "哈里蓬猜佛塔寺"
  },
  "google_maps_url": "https://maps.app.goo.gl/7RhvG8sf8BmTuCkZ9",
  "source_status": true
}
```

`source_status` is a raw field published by the site. The scraper preserves
it but does not infer an operational meaning from it. For example, every
recommendation card currently has `source_status: false` while still being
shown normally.

## Run

Install the pinned parser dependencies into the existing virtual environment:

```bash
source .venv/bin/activate
uv pip install -r "Website Scrape/Visit Lumphun/requirements.txt"
```

Then run from the workspace root:

```bash
.venv/bin/python "Website Scrape/Visit Lumphun/scrape.py"
```

To reparse all static content even when the site bundle has not changed:

```bash
.venv/bin/python "Website Scrape/Visit Lumphun/scrape.py" --force-static
```

Run the development checks with:

```bash
.venv/bin/python -m ruff check "Website Scrape/Visit Lumphun/scrape.py"
.venv/bin/python -m ruff format --check "Website Scrape/Visit Lumphun/scrape.py"
.venv/bin/python -m pyright "Website Scrape/Visit Lumphun/scrape.py"
```

## Code guide

`scrape.py` is organized from general utilities to page-specific extraction
and persistence:

| Part | Main functions/classes | Responsibility |
| --- | --- | --- |
| HTTP and cleaning | `fetch`, `clean_text`, `clean_value` | Download public content; normalize Unicode and whitespace. |
| Field normalization | `absolute_url`, `normalized_phone`, `normalize_time`, `fare` | Clean URLs, phones, times, and fares without discarding their original form. |
| Bundle parser | `BundleData`, `literal` | Use Tree-sitter's JavaScript AST to find assignments and convert only published literal values, without executing them. |
| Page extraction | `extract_homepage`, `extract_recommend`, `extract_travel`, `extract_komepage`, `extract_contact` | Transform each site's published collection into its output schema. |
| Incremental store | `setup_database`, `flatten`, `save_page` | Upsert normalized records, create history versions, and retire confirmed missing records. |
| Program entry point | `main` | Download the pages/bundle, select incremental or forced parsing, save JSON, and record the run. |

## Incremental behavior

SQLite is the source of truth for history. JSON files are the latest clean
page views.

The database has three tables:

- `runs`: bundle hash, start/end time, success/failure outcome, and warnings.
- `records`: newest normalized version of each record, its content hash, first
  seen, last seen, last changed, and active status.
- `record_versions`: immutable snapshots created when a record is inserted,
  changed, or retired.

On a normal run:

1. The script calculates the public bundle's SHA-256 hash.
2. If it matches the last successful run, static page data is reused from the
   previous JSON output.
3. If the hash changed, every static page section is parsed again.
4. A new stable record ID is inserted. An existing ID with a changed content
   hash is updated and versioned. An unchanged ID only receives a new
   `last_seen_at` timestamp.
5. A record is marked inactive only after a complete successful extraction
   confirms it is absent. A partial error never deletes historical data.

This allows you to answer both “what is currently on the site?” from the JSON
files and “when did this item change?” from SQLite.

## Data quality rules

- Unicode is normalized to NFC and non-breaking spaces are replaced.
- Text is trimmed and accidental repeated spaces are collapsed.
- Phone numbers retain their source format and get a digits-only companion.
- Times such as `07.30` become `07:30` when they match a valid clock time.
- Fares retain `raw` source text and add amount/currency only when clear.
- Relative URLs become absolute Visit Lamphun HTTPS URLs; malformed URLs are
  represented as `null` rather than guessed.
- Missing translations become warnings; valid static data is still kept.

## Maintenance notes

The React bundle is minified, so its short collection names (for example,
`kM`, `OM`, `MM`, and `FM`) may change when the site is rebuilt. Tree-sitter
continues to parse valid JavaScript even when formatting and nesting change,
but the extractor still needs an updated collection name if the developer
renames one. An error such as `missing expected published data collection` is
deliberate: it prevents silently exporting an incomplete or incorrect dataset.

Only public routes are in scope. Do not add private driver routes,
authentication, or Firebase write operations to this scraper.
