#!/usr/bin/env python3
"""Incremental scraper for the public RinMP AppTech registry (app.rinmp.com).

Collects three datasets from public GET JSON endpoints:

- app_tech          technology catalog: list discovery + per-ID detail pass
- statistics        platform counters, used both as export and as the
                    reconciliation gate for the app_tech dataset
- owner_profiles    owner dimension (email/phone stripped at ingest)

SQLite (state/rinmp.sqlite3) is the authoritative incremental store; JSON
exports in output/ are the latest clean datasets. On any failure the run is
recorded and the previous good exports are left untouched.

Only https://app.rinmp.com is contacted. www.rinmp.com is untrusted
(self-signed fallback certificate) and never requested.
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "output"
DEFAULT_STATE_PATH = ROOT / "state" / "rinmp.sqlite3"
HQ_PROVINCES_PATH = ROOT / "references" / "institute_hq_provinces.json"

BASE_URL = "https://app.rinmp.com/"
LIST_URL = BASE_URL + "api/appTechPublic"
DETAIL_URL = BASE_URL + "api/appTechPublic/{id}"
STATS_URL = BASE_URL + "api/custom/appTechPublic/listAppTechStatisticData"
OWNERS_URL = BASE_URL + "api/custom/appTechPublic/listAppTechOwnerProfile"

PAGE_LISTING = "app_tech_listing"
PAGE_APP_TECH = "app_tech"
PAGE_OWNER = "owner_profile"

SCHEMA_VERSION = 1
LIST_PAGE_SIZE = 99
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_USER_AGENT = "RinMPAppTechCollector/1.0 (public registry incremental scraper)"

PLACEHOLDER_TEXT = {"-", "ไม่มี", "-ไม่มี-"}
# Completeness floor: the catalog has been 600+ records since at least
# 2026-08; a smaller result means a broken walk or an API change.
MIN_CATALOG_RECORDS = 400
REMOVAL_LIMIT = 0.25

DESCRIPTION_LABELS = (
    "appTechDescription",
    "functionalDetail",
    "highlightDetail",
    "serviceDetail",
    "targetUserAddressDetail",
)
MATRIX_SCORE_FIELDS = (
    "easeCostMatrixScore",
    "localTechMatrixScore",
    "impact3EMatrixScore",
    "techEfficiencyMatrixScore",
)
VOLATILE_HASH_FIELDS = {
    "scraped_at",
    "run_id",
    "last_seen_at",
    "currentAdminQuantity",
    "currentLeadAdminQuantity",
}
PII_SUBSTRINGS = ("email", "phone", "mobile")
PII_EXACT_KEYS = {
    "tel",
    "citizenid",
    "citizen_id",
    "nationalid",
    "national_id",
    "passportno",
    "passport_no",
    "lineid",
    "line_id",
}
# Contact addresses also appear as free-text values (e.g. the API stores an
# email under ownerContact.detail), so values are scrubbed as well.
EMAIL_VALUE_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REDACTED_CONTACT = "[redacted-contact]"


class ScrapeError(RuntimeError):
    """Fatal problem that must abort the run before any state change."""


ProgressReporter = Callable[[str], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def no_progress(_: str) -> None:
    """Default reporter for direct calls and offline tests."""


def create_progress_reporter(quiet: bool) -> ProgressReporter:
    if quiet:
        return no_progress

    def report(message: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"{stamp} {message}", file=sys.stderr, flush=True)

    return report


# ---------------------------------------------------------------------------
# HTTP


def create_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    return session


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 4,
) -> Any:
    backoff_seconds = 2.0
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=60)
            if response.status_code in (429, 500, 502, 503, 504):
                raise ScrapeError(f"transient HTTP {response.status_code} for {url}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(backoff_seconds)
            backoff_seconds *= 2
    raise ScrapeError(f"failed to fetch {url}: {last_error}")


# ---------------------------------------------------------------------------
# Cleaning


def clean_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def optional_text(value: Any) -> str | None:
    text = clean_text(value) if isinstance(value, str) else None
    if text is None or text in PLACEHOLDER_TEXT:
        return None
    return text


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "ul", "ol", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Any) -> str | None:
    text = optional_text(value)
    if text is None:
        return None
    extractor = _TextExtractor()
    try:
        extractor.feed(text)
    except Exception:
        return text
    lines = [clean_text(line) for line in "".join(extractor.parts).split("\n")]
    return "\n".join(line for line in lines if line) or None


def strip_pii(value: Any) -> Any:
    """Drop contact/identity keys and scrub contact values recursively so
    PII never reaches disk."""

    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in PII_SUBSTRINGS) or lowered in PII_EXACT_KEYS:
                continue
            kept[key] = strip_pii(child)
        return kept
    if isinstance(value, list):
        return [strip_pii(item) for item in value]
    if isinstance(value, str):
        return EMAIL_VALUE_PATTERN.sub(REDACTED_CONTACT, value)
    return value


def semantic_hash(value: Any) -> str:
    def remove_volatile(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: remove_volatile(child)
                for key, child in item.items()
                if key not in VOLATILE_HASH_FIELDS
            }
        if isinstance(item, list):
            return [remove_volatile(child) for child in item]
        return item

    canonical = json.dumps(
        remove_volatile(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_trl(raw: Any) -> tuple[int | None, str | None]:
    text = optional_text(raw)
    if text is None:
        return None, None
    match = re.fullmatch(r"TRL[_\s-]?(\d)", text, flags=re.IGNORECASE)
    if not match:
        return None, text
    level = int(match.group(1))
    if not 1 <= level <= 9:
        return None, text
    return level, text


def parse_addresses(raw: Any, warnings: list[str]) -> list[dict[str, str | None]]:
    text = optional_text(raw)
    if text is None:
        return []
    try:
        items = json.loads(text)
    except ValueError:
        warnings.append("userAddressListString is not valid JSON")
        return []
    if not isinstance(items, list):
        warnings.append("userAddressListString is not a JSON array")
        return []
    addresses: list[dict[str, str | None]] = []
    for item in items:
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict):
            warnings.append("address entry without value object")
            continue
        addresses.append(
            {
                "name": optional_text(value.get("name")),
                "sub_district": optional_text(value.get("subDistrict")),
                "district": optional_text(value.get("district")),
                "province": optional_text(value.get("province")),
            }
        )
    return addresses


def load_hq_provinces(path: Path = HQ_PROVINCES_PATH) -> dict[str, str]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["institute_hq_provinces"]


def resolve_province(
    addresses: list[dict[str, str | None]],
    institute_name: str | None,
    hq_provinces: dict[str, str],
    warnings: list[str],
) -> str | None:
    for address in addresses:
        province = address.get("province")
        if province:
            return province
    if institute_name:
        fallback = hq_provinces.get(institute_name)
        if fallback:
            return fallback
        warnings.append(f"no province resolvable for institute {institute_name!r}")
    else:
        warnings.append("no institute available for province fallback")
    return None


def derive_record_warnings(records: list[dict[str, Any]]) -> list[str]:
    """Data-quality warnings derived from the stored dataset so they stay
    visible in every export, not only in runs that re-fetched details."""

    warnings: list[str] = []
    missing_sections = [r["id"] for r in records if not r["description_sections"]]
    if missing_sections:
        sample = ", ".join(str(rid) for rid in missing_sections[:10])
        warnings.append(
            f"{len(missing_sections)} records have none of the expected description sections "
            f"(empty at the source; ids: {sample})"
        )
    unresolved_province = [r["id"] for r in records if r.get("province_resolved") is None]
    if unresolved_province:
        sample = ", ".join(str(rid) for rid in unresolved_province[:10])
        warnings.append(
            f"{len(unresolved_province)} records have no resolvable province "
            f"(ids: {sample})"
        )
    return warnings


def build_owner_display_name(owner_contact: Any) -> str | None:
    if not isinstance(owner_contact, dict):
        return None
    parts = [
        optional_text(owner_contact.get("academicRank")),
        optional_text(owner_contact.get("name")),
        optional_text(owner_contact.get("lastname")),
    ]
    return " ".join(part for part in parts if part) or None


def normalize_owner_name(fullname: Any) -> str | None:
    return optional_text(fullname)


def owner_key_for(fullname: str) -> str:
    digest = hashlib.sha256(fullname.encode("utf-8")).hexdigest()
    return f"OWNER-{digest[:16]}"


# ---------------------------------------------------------------------------
# Record normalization


def normalize_detail(
    detail_raw: dict[str, Any],
    listing: dict[str, Any],
    hq_provinces: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    detail_block = detail_raw.get("detail") or {}

    description_sections = []
    for label in DESCRIPTION_LABELS:
        text = html_to_text(detail_block.get(label))
        if text is not None:
            description_sections.append({"label": label, "text": text})
    if not description_sections:
        warnings.append("detail page yielded none of the expected description sections")

    segments_raw = detail_block.get("targetUserList")
    target_segments = (
        [part for part in segments_raw.split("|") if part] if isinstance(segments_raw, str) else []
    )

    addresses = parse_addresses(detail_block.get("userAddressListString"), warnings)

    sub_category = listing.get("subCategory") or {}
    category = sub_category.get("category") or {}
    institute = listing.get("institute") or {}
    institute_name = optional_text(institute.get("displayText"))
    trl_level, trl_raw = parse_trl(detail_raw.get("trlLevel"))

    commercial_raw = detail_raw.get("commercialDetail") or {}
    cost_per_unit = commercial_raw.get("costPerUnit")
    budget = commercial_raw.get("budget")

    matrix_scores = {field: detail_raw.get(field) for field in MATRIX_SCORE_FIELDS}
    matrix_scores["matrixScoreAverage"] = detail_raw.get("matrixScoreAverage")
    if all(value is None for value in matrix_scores.values()):
        matrix_scores = None

    ip_raw = detail_raw.get("intellectualPropertyDetail")
    ip = None
    if isinstance(ip_raw, dict):
        ip = {
            "category": optional_text(ip_raw.get("category")),
            "number_raw": optional_text(ip_raw.get("intellectualPropertyNumber")),
        }

    tags = []
    for tag in detail_raw.get("tagList") or []:
        search_tag = tag.get("searchTag") or {}
        display = optional_text(search_tag.get("displayText"))
        if display:
            tags.append(display)

    media_profile = detail_raw.get("profilePicture") or {}
    gallery_ids = [
        entry.get("id") for entry in detail_raw.get("galleryImageList") or [] if entry.get("id")
    ]

    title = optional_text(detail_raw.get("displayText")) or optional_text(listing.get("displayText"))
    if title is None:
        raise ScrapeError(f"detail {detail_raw.get('id')} has no title")

    record = {
        "record_type": PAGE_APP_TECH,
        "id": detail_raw.get("id"),
        "detail_url": DETAIL_URL.format(id=detail_raw.get("id")),
        "title": title,
        "status": optional_text(detail_raw.get("status")),
        "atl_level": optional_text(detail_raw.get("atlLevel")),
        "trl": {"level": trl_level, "raw": trl_raw},
        "certificate": {
            "level_raw": optional_text(detail_raw.get("certificateAtlLevel")),
            "expired_datetime": optional_text(detail_raw.get("expiredCertificateDateTime")),
        },
        "is_recommend": bool(detail_raw.get("isRecommend")),
        "industrial_type_raw": optional_text(detail_raw.get("industrialType")),
        "institute": {
            "id": institute.get("id"),
            "name": institute_name,
        },
        "category": optional_text(category.get("displayText")),
        "sub_category": optional_text(sub_category.get("displayText")),
        "owner_display_name": build_owner_display_name(detail_raw.get("ownerContact")),
        "description_sections": description_sections,
        "target_segments": target_segments,
        "addresses": addresses,
        "province_resolved": resolve_province(addresses, institute_name, hq_provinces, warnings),
        "ip": ip,
        "commercial": {
            "cost_per_unit_thb": float(cost_per_unit) if cost_per_unit is not None else None,
            "budget_thb": float(budget) if budget else None,
            "life_time_year_count": commercial_raw.get("lifeTimeYearCount"),
        },
        "matrix_scores": matrix_scores,
        "tags": tags,
        "media": {
            "profile_picture_file_id": media_profile.get("id"),
            "gallery_file_ids": gallery_ids,
        },
        "source_created_at": optional_text(detail_raw.get("createDateTime")),
        "source_last_update_at": optional_text(detail_raw.get("lastUpdateDateTime")),
    }
    return record, warnings


# ---------------------------------------------------------------------------
# Collectors


def collect_catalog(
    session: requests.Session,
    report: ProgressReporter,
) -> tuple[dict[int, dict[str, Any]], int]:
    seen_total: int | None = None
    listings: dict[int, dict[str, Any]] = {}
    offset = 0
    while True:
        payload = fetch_json(
            session,
            LIST_URL,
            params={
                "offset": offset,
                "max": LIST_PAGE_SIZE,
                "__template": "appTech.public.list",
            },
        )
        total = payload.get("totalCount")
        if not isinstance(total, int):
            raise ScrapeError(f"list response missing integer totalCount at offset {offset}")
        if seen_total is None:
            if total < MIN_CATALOG_RECORDS:
                raise ScrapeError(
                    f"totalCount={total} is below the completeness floor {MIN_CATALOG_RECORDS}"
                )
            report(f"catalog: totalCount={total}")
            seen_total = total
        elif total != seen_total:
            raise ScrapeError("totalCount changed mid-pagination; rerun to retry")

        rows = payload.get("data") or []
        for row in rows:
            external_id = row.get("id")
            if external_id is None:
                raise ScrapeError(f"catalog row without id at offset {offset}")
            if external_id in listings:
                raise ScrapeError(f"duplicate catalog id {external_id}")
            listings[external_id] = row
        offset += LIST_PAGE_SIZE
        report(f"catalog: {len(listings)}/{total} rows")
        if len(listings) >= total or not rows:
            break

    if len(listings) != seen_total:
        raise ScrapeError(
            f"catalog incomplete: collected {len(listings)} unique ids, expected {seen_total}"
        )
    return listings, seen_total


def fetch_detail_record(
    session: requests.Session,
    external_id: int,
    listing: dict[str, Any],
    hq_provinces: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    payload = fetch_json(session, DETAIL_URL.format(id=external_id))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ScrapeError(f"detail {external_id} returned no data object")
    normalized, warnings = normalize_detail(strip_pii(data), listing, hq_provinces)
    return normalized, warnings


def collect_statistics(session: requests.Session) -> dict[str, Any]:
    payload = fetch_json(session, STATS_URL)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ScrapeError("statistics endpoint returned no data object")
    graph = data.get("instituteGraphData") or {}
    labels = graph.get("labels") or []
    counts = (graph.get("datasets") or [{}])[0].get("data") or []
    institute_graph = [
        {"institute": label, "count": count} for label, count in zip(labels, counts)
    ]
    return {
        "total_app_tech_count": data.get("totalAppTechCount"),
        "institute_count": data.get("instituteCount"),
        "innovator_count": data.get("innovatorCount"),
        "normal_user_count": data.get("normalUserCount"),
        "requirement_count": data.get("requirementCount"),
        "match_requirement_count": data.get("matchRequirementCount"),
        "institute_graph": institute_graph,
    }


def reconcile_statistics(
    stats: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    from collections import Counter

    list_total = len(records)
    reported_total = stats["total_app_tech_count"]
    counts_match = reported_total == list_total

    computed = Counter(record["institute"]["name"] for record in records)
    per_institute_match = all(
        entry["count"] == computed.get(entry["institute"], 0) for entry in stats["institute_graph"]
    ) and sum(entry["count"] for entry in stats["institute_graph"]) == list_total

    if not counts_match or not per_institute_match:
        raise ScrapeError(
            "reconciliation gate failed: "
            f"reported total={reported_total}, collected={list_total}, "
            f"per_institute_match={per_institute_match}"
        )
    return {
        "list_total_count": list_total,
        "counts_match": counts_match,
        "per_institute_match": per_institute_match,
    }


def collect_owner_profiles(
    session: requests.Session,
    title_to_ids: dict[str, set[int]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    payload = fetch_json(session, OWNERS_URL)
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ScrapeError("owner profile endpoint returned no data array")
    return parse_owner_profiles(rows, title_to_ids, warnings)


def parse_owner_profiles(
    rows: list[Any],
    title_to_ids: dict[str, set[int]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    unmatched_titles = 0
    for row in rows:
        fullname = normalize_owner_name(row.get("fullname"))
        if fullname is None:
            warnings.append("owner profile row without fullname skipped")
            continue
        titles = [optional_text(title) for title in row.get("appTechList") or []]
        titles = [title for title in titles if title]
        tech_ids: set[int] = set()
        for title in titles:
            matched = title_to_ids.get(title)
            if matched:
                tech_ids |= matched
            else:
                unmatched_titles += 1
        institute = optional_text(row.get("institute"))
        key = owner_key_for(fullname)
        profile = profiles.setdefault(
            key,
            {
                "record_type": PAGE_OWNER,
                "owner_key": key,
                "fullname": fullname,
                "institute": institute,
                "app_tech_ids": set(),
                "app_tech_titles": [],
            },
        )
        profile["app_tech_ids"] |= tech_ids
        for title in titles:
            if title not in profile["app_tech_titles"]:
                profile["app_tech_titles"].append(title)

    if unmatched_titles:
        warnings.append(f"{unmatched_titles} owner tech titles did not match any catalog title")
    if len(profiles) < len(rows):
        warnings.append(
            f"owner rows collapsed by duplicate fullname: {len(rows)} rows -> {len(profiles)} owners"
        )

    result = []
    for profile in sorted(profiles.values(), key=lambda item: item["owner_key"]):
        result.append(
            {
                "record_type": PAGE_OWNER,
                "owner_key": profile["owner_key"],
                "fullname": profile["fullname"],
                "institute": profile["institute"],
                "app_tech_ids": sorted(profile["app_tech_ids"]),
                "app_tech_titles": profile["app_tech_titles"],
            }
        )
    return result


# ---------------------------------------------------------------------------
# SQLite state


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            summary_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            page_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            last_run_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (page_id, external_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS record_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            event TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        )
        """
    )
    return connection


def active_records(connection: sqlite3.Connection, page_id: str) -> dict[str, sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM records WHERE page_id = ? AND active = 1", (page_id,)
    ).fetchall()
    return {row["external_id"]: row for row in rows}


def sync_records(
    connection: sqlite3.Connection,
    page_id: str,
    records: list[dict[str, Any]],
    run_id: str,
    now: str,
    allow_large_removal: bool = False,
) -> dict[str, int]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        external_id = str(record.get("id") if page_id != PAGE_OWNER else record.get("owner_key"))
        if external_id in by_id:
            raise ScrapeError(f"{page_id}: duplicate external id {external_id}")
        by_id[external_id] = record

    existing = active_records(connection, page_id)
    removed_ids = set(existing) - set(by_id)
    if existing and len(removed_ids) / len(existing) > REMOVAL_LIMIT and not allow_large_removal:
        raise ScrapeError(
            f"{page_id}: removing {len(removed_ids)}/{len(existing)} exceeds "
            f"the {REMOVAL_LIMIT:.0%} safety limit"
        )

    counts = {"new": 0, "updated": 0, "unchanged": 0, "removed": 0}

    def insert_version(external_id: str, event: str, payload_json: str, content_hash: str) -> None:
        connection.execute(
            """
            INSERT INTO record_versions
                (page_id, external_id, observed_at, event, payload_json, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (page_id, external_id, now, event, payload_json, content_hash),
        )

    with connection:
        for external_id, record in by_id.items():
            payload_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
            content_hash = semantic_hash(record)
            previous = existing.get(external_id)
            if previous is None:
                connection.execute(
                    """
                    INSERT INTO records (
                        page_id, external_id, payload_json, content_hash, first_seen_at,
                        last_seen_at, last_changed_at, last_run_id, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (page_id, external_id, payload_json, content_hash, now, now, now, run_id),
                )
                insert_version(external_id, "inserted", payload_json, content_hash)
                counts["new"] += 1
            elif previous["content_hash"] != content_hash:
                connection.execute(
                    """
                    UPDATE records SET payload_json = ?, content_hash = ?, last_seen_at = ?,
                        last_changed_at = ?, last_run_id = ?, active = 1
                    WHERE page_id = ? AND external_id = ?
                    """,
                    (payload_json, content_hash, now, now, run_id, page_id, external_id),
                )
                insert_version(external_id, "updated", payload_json, content_hash)
                counts["updated"] += 1
            else:
                connection.execute(
                    """
                    UPDATE records SET last_seen_at = ?, last_run_id = ?, active = 1
                    WHERE page_id = ? AND external_id = ?
                    """,
                    (now, run_id, page_id, external_id),
                )
                counts["unchanged"] += 1

        for external_id in removed_ids:
            previous = existing[external_id]
            connection.execute(
                "UPDATE records SET active = 0, last_seen_at = ?, last_run_id = ? "
                "WHERE page_id = ? AND external_id = ?",
                (now, run_id, page_id, external_id),
            )
            insert_version(external_id, "retired", previous["payload_json"], previous["content_hash"])
            counts["removed"] += 1
    return counts


# ---------------------------------------------------------------------------
# Exports


def write_output(path: Path, envelope: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


def base_envelope(dataset: str, run_id: str, scraped_at: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "source_base_url": BASE_URL,
        "scraped_at": scraped_at,
        "run_id": run_id,
        "warnings": warnings,
    }


def export_page_records(
    connection: sqlite3.Connection,
    page_id: str,
    output_path: Path,
    dataset: str,
    run_id: str,
    scraped_at: str,
    warnings: list[str],
) -> int:
    rows = connection.execute(
        "SELECT payload_json FROM records WHERE page_id = ? AND active = 1", (page_id,)
    ).fetchall()
    records = [json.loads(row["payload_json"]) for row in rows]
    if page_id == PAGE_APP_TECH:
        records.sort(key=lambda record: record["id"])
    elif page_id == PAGE_OWNER:
        records.sort(key=lambda record: record["owner_key"])

    envelope = base_envelope(dataset, run_id, scraped_at, warnings)
    envelope["record_count"] = len(records)
    envelope["data"] = {"records": records}
    write_output(output_path, envelope)
    return len(records)


# ---------------------------------------------------------------------------
# Run flow


def build_title_index(listings: dict[int, dict[str, Any]]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {}
    for external_id, row in listings.items():
        title = optional_text(row.get("displayText"))
        if title:
            index.setdefault(title, set()).add(external_id)
    return index


def run_scrape(args: argparse.Namespace) -> int:
    report = create_progress_reporter(args.quiet)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = utc_now()
    output_dir = Path(args.output_dir)
    connection = open_database(Path(args.state_path))
    summary: dict[str, Any] = {"run_id": run_id, "warnings": []}
    connection.execute(
        "INSERT INTO runs (run_id, started_at, status, summary_json) VALUES (?, ?, ?, ?)",
        (run_id, started_at, "running", "{}"),
    )
    connection.commit()

    try:
        session = create_session(args.user_agent)
        hq_provinces = load_hq_provinces()

        listings, total = collect_catalog(session, report)
        report(f"catalog complete: {total} unique ids")

        existing_details = active_records(connection, PAGE_APP_TECH)
        existing_listings = active_records(connection, PAGE_LISTING)
        old_listing_hashes = {row["external_id"]: row["content_hash"] for row in existing_listings.values()}

        # Hash and persist the same stripped shape so delta detection is stable.
        listing_rows = [strip_pii(dict(row)) for row in listings.values()]
        stripped_listings = {row["id"]: row for row in listing_rows}

        needed: list[int] = []
        for external_id, row in stripped_listings.items():
            if args.full_details or str(external_id) not in existing_details:
                needed.append(external_id)
                continue
            previous_hash = old_listing_hashes.get(str(external_id))
            if previous_hash != semantic_hash(row):
                needed.append(external_id)
        report(f"details to fetch: {len(needed)} of {total}")

        fetched_warnings: list[str] = []
        new_records: dict[int, dict[str, Any]] = {}
        failures = 0
        for index, external_id in enumerate(sorted(needed)):
            if args.limit and index >= args.limit:
                break
            try:
                record, warnings = fetch_detail_record(
                    session, external_id, stripped_listings[external_id], hq_provinces
                )
                new_records[external_id] = record
                fetched_warnings.extend(f"id {external_id}: {warning}" for warning in warnings)
            except ScrapeError as exc:
                failures += 1
                fetched_warnings.append(f"id {external_id}: detail fetch failed ({exc})")
            if (index + 1) % max(args.progress_every, 1) == 0 or index + 1 == len(needed):
                report(f"details: {index + 1}/{len(needed)} fetched, {failures} failed")
            time.sleep(args.delay)

        if args.limit:
            summary["status"] = f"smoke_test: processed first {min(len(needed), args.limit)} details"
            finish_run(connection, run_id, "smoke_test", summary)
            report(summary["status"])
            return 0

        # Reuse unchanged stored payloads for IDs not re-fetched this run.
        records_by_id: dict[int, dict[str, Any]] = {}
        for external_id, row in existing_details.items():
            records_by_id[int(external_id)] = json.loads(row["payload_json"])
        records_by_id.update(new_records)

        missing = set(listings) - set(records_by_id)
        if missing:
            sample = ", ".join(str(value) for value in sorted(missing)[:5])
            raise ScrapeError(
                f"{len(missing)} catalog ids have no usable record (sample: {sample})"
            )

        records = [records_by_id[external_id] for external_id in sorted(records_by_id)]

        stats = collect_statistics(session)
        reconciliation = reconcile_statistics(stats, records)
        summary["reconciliation"] = reconciliation

        counts_listing = sync_records(connection, PAGE_LISTING, listing_rows, run_id, utc_now())
        counts_detail = sync_records(connection, PAGE_APP_TECH, records, run_id, utc_now())
        title_to_ids = build_title_index(listings)
        owners = collect_owner_profiles(session, title_to_ids, summary["warnings"])
        counts_owners = sync_records(connection, PAGE_OWNER, owners, run_id, utc_now())

        summary["counts"] = {
            PAGE_LISTING: counts_listing,
            PAGE_APP_TECH: counts_detail,
            PAGE_OWNER: counts_owners,
        }
        summary["warnings"].extend(fetched_warnings)

        scraped_at = utc_now()
        dataset_warnings = list(summary["warnings"]) + derive_record_warnings(records)
        export_page_records(
            connection, PAGE_APP_TECH, output_dir / "app_tech.json", PAGE_APP_TECH,
            run_id, scraped_at, dataset_warnings,
        )
        stats_envelope = base_envelope("statistics", run_id, scraped_at, [])
        stats_envelope["reconciliation"] = reconciliation
        stats_envelope["data"] = stats
        write_output(output_dir / "statistics.json", stats_envelope)
        export_page_records(
            connection, PAGE_OWNER, output_dir / "owner_profiles.json", PAGE_OWNER,
            run_id, scraped_at, summary["warnings"],
        )

        summary["status"] = "success"
        finish_run(connection, run_id, "success", summary)
        report(
            f"success: {counts_detail['new']} new, {counts_detail['updated']} updated, "
            f"{counts_detail['unchanged']} unchanged app_tech records; "
            f"{counts_owners['new']} new owner profiles"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - run-level failure boundary
        summary["status"] = "failed"
        summary["error"] = str(exc)
        finish_run(connection, run_id, "failed", summary)
        report(f"FAILED: {exc}")
        return 1


def finish_run(connection: sqlite3.Connection, run_id: str, status: str, summary: dict[str, Any]) -> None:
    connection.execute(
        "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE run_id = ?",
        (utc_now(), status, json.dumps(summary, ensure_ascii=False), run_id),
    )
    connection.commit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape the public RinMP AppTech registry.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Smoke-test: process only the first N details; does not update state or outputs.",
    )
    parser.add_argument("--full-details", action="store_true", help="Re-fetch every detail page.")
    parser.add_argument(
        "--progress-every", type=int, default=25, help="Report progress every Nth detail fetch."
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--allow-large-removal", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.delay < 0 or args.limit < 0 or args.progress_every < 1:
        raise SystemExit("--delay and --limit must be non-negative; --progress-every must be >= 1")
    return run_scrape(args)


if __name__ == "__main__":
    sys.exit(main())
