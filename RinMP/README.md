# RinMP AppTech Scraper

Incremental scraper for the public RinMP AppTech registry at
[app.rinmp.com](https://app.rinmp.com/) — the เครือข่าย มทร. appropriate
technology (เทคโนโลยีที่เหมาะสมและนวัตกรรมพร้อมใช้) registry under the
บพท./PMUA program. See `SCRAPER_PLAN.md` for the investigated design and the
2026-08-22 live-API evidence.

## Datasets

| Export | Records (baseline 2026-08-23) | Content |
| --- | --- | --- |
| `output/app_tech.json` | 630 | One record per technology: title, status, ATL level, TRL, institute, category/sub-category, owner display name, description sections, target segments, structured addresses + resolved province, IP type, commercial cost, tags, media file IDs |
| `output/statistics.json` | — | Platform counters (`requirementCount`, `matchRequirementCount`, `normalUserCount` exist nowhere else) plus the reconciliation proof against the collected catalog |
| `output/owner_profiles.json` | 308 | Owner dimension deduped by normalized fullname (source publishes 335 rows); `owner_key` is a SHA-256-derived stable join key |

## Install and run

Use the workspace virtual environment at `Website Scrape/.venv`
(Python 3.12, only dependency: `requests`).

```bash
../.venv/bin/python test_scrape.py          # offline tests, no network
../.venv/bin/python scrape.py               # full incremental run (~1 min when unchanged)
../.venv/bin/python scrape.py --limit 5     # smoke test; touches neither state nor outputs
../.venv/bin/python scrape.py --full-details  # force re-fetch of every detail page (~11 min)
```

Useful flags: `--delay` (default 1 s), `--progress-every`, `--quiet`,
`--state-path`, `--output-dir`, `--allow-large-removal`.

## How it works

1. **Discovery**: pages through `GET /api/appTechPublic?offset&max=99&__template=appTech.public.list`
   until unique IDs equal `totalCount`. Duplicate IDs, a mid-walk total change,
   or a total below the completeness floor (400) abort the run.
2. **Details**: fetches `GET /api/appTechPublic/{id}` only for new IDs and IDs
   whose stripped listing payload hash changed since the last run.
3. **Reconciliation gate**: `listAppTechStatisticData` must confirm both the
   total count and every per-institute count of the collected records;
   mismatch fails the run and preserves the previous exports.
4. **State**: SQLite tables `runs`, `records`, `record_versions`; content-hash
   change detection; retirements only after a complete successful run and
   capped at 25% unless `--allow-large-removal`.
5. **Exports**: rewritten only after a fully successful run.

## PII boundary

Every list and detail response carries owner contact data, and some emails
hide in free-text fields (e.g. `ownerContact.detail`). `strip_pii()` runs on
everything before persistence: contact keys are dropped and email-shaped
values are replaced with `[redacted-contact]`. No email or phone value should
exist in `state/` or `output/`. Owner identity beyond the public display name
is reduced to a non-reversible `owner_key`.

## Deliberately not scraped

- `listAppTechInstituteMapData` — derivative of the list catalog (verified to
  match our group-by exactly); its choropleth values are all zero.
- `listAppTechInnovatorMapData` — platform-user choropleth (sum equals
  `normalUserCount`), wrong entity type for this dataset.
- `listAppTechInteractionMapData` — unverifiable semantics (values match no
  other endpoint).
- Images/videos — media kept as ManagedFile IDs only.

**Never contact `www.rinmp.com`**: it currently serves a self-signed Traefik
fallback certificate with a bare 404 and has been used to inject fake content.
The scraper only talks to `app.rinmp.com`.

## Baselines and maintenance

Baseline (first successful full runs, 2026-08-22/23 UTC): totalCount = 630,
owners = 308 distinct names, reconciliation clean, second consecutive run
created zero new record versions. Refresh trigger: a changed `totalCount`.
Known data quirks recorded as warnings in the exports (e.g. 18 records without
any expected description section, one institute without an HQ province).
