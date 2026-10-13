#!/usr/bin/env python3
"""Incremental scraper for the public RinMP AppTech registry (app.rinmp.com).

Collects three datasets from public GET JSON endpoints:

- app_tech          technology catalog: list discovery + per-ID detail pass
- statistics        platform counters, used both as export and as the
                    reconciliation gate for the app_tech dataset
- owner_profiles    owner dimension (email/phone stripped at ingest)

Each run is compared with the previous JSON exports in output/, which are the
only state kept. On any failure the previous good exports are left untouched.

Only https://app.rinmp.com is contacted. www.rinmp.com is untrusted
(self-signed fallback certificate) and never requested.
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "output"
HQ_PROVINCES_PATH = ROOT / "references" / "institute_hq_provinces.json"

BASE_URL = "https://app.rinmp.com/"
LIST_URL = BASE_URL + "api/appTechPublic"
DETAIL_URL = BASE_URL + "api/appTechPublic/{id}"
STATS_URL = BASE_URL + "api/custom/appTechPublic/listAppTechStatisticData"
OWNERS_URL = BASE_URL + "api/custom/appTechPublic/listAppTechOwnerProfile"

PAGE_APP_TECH = "app_tech"
PAGE_OWNER = "owner_profile"

SCHEMA_VERSION = 1
LIST_PAGE_SIZE = 99
DEFAULT_DELAY_SECONDS = 0.0  # Pause after each request, per worker.
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_USER_AGENT = "RinMPAppTechCollector"

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
# The API returns tags in a different order on each request; order is not content.
ORDER_INSENSITIVE_HASH_FIELDS = {"tags"}
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


def thread_session_provider(user_agent: str) -> Callable[[], requests.Session]:
    """Return a function giving each calling thread its own session."""
    local = threading.local()

    def get_session() -> requests.Session:
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = create_session(user_agent)
        return session

    return get_session


def fetch_ordered(
    fetch: Callable[[Any], Any], items: list[Any], workers: int
) -> Generator[Any, None, None]:
    """Yield fetch(item) for each item in input order, fetching concurrently.

    The first error raises here and cancels work that has not started. Callers
    should wrap the iterator in contextlib.closing so that an error in their
    own loop body also stops the pool.
    """
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [executor.submit(fetch, item) for item in items]
        for future in futures:
            yield future.result()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


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
                key: (
                    sorted(child)
                    if key in ORDER_INSENSITIVE_HASH_FIELDS and isinstance(child, list)
                    else remove_volatile(child)
                )
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


def fetch_detail_payload(session: requests.Session, external_id: int) -> dict[str, Any]:
    """HTTP only: fetch one detail payload. Safe to run in a worker thread."""
    payload = fetch_json(session, DETAIL_URL.format(id=external_id))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ScrapeError(f"detail {external_id} returned no data object")
    return data


def collect_details(
    external_ids: list[int],
    listings: dict[int, dict[str, Any]],
    hq_provinces: dict[str, str],
    *,
    user_agent: str,
    workers: int,
    delay: float,
    progress_every: int,
    report: ProgressReporter,
) -> tuple[dict[int, dict[str, Any]], list[str], int]:
    """Fetch details concurrently; normalize them here, in ID order.

    A failed detail is counted and warned about, not raised, as before.
    """
    worker_session = thread_session_provider(user_agent)

    def fetch_one(external_id: int) -> dict[str, Any] | ScrapeError:
        try:
            return fetch_detail_payload(worker_session(), external_id)
        except ScrapeError as exc:
            return exc
        finally:
            time.sleep(delay)

    records: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    failures = 0
    with closing(fetch_ordered(fetch_one, external_ids, workers)) as payloads:
        for index, (external_id, payload) in enumerate(zip(external_ids, payloads, strict=True)):
            try:
                if isinstance(payload, ScrapeError):
                    raise payload
                record, record_warnings = normalize_detail(
                    strip_pii(payload), listings[external_id], hq_provinces
                )
                records[external_id] = record
                warnings.extend(f"id {external_id}: {warning}" for warning in record_warnings)
            except ScrapeError as exc:
                failures += 1
                warnings.append(f"id {external_id}: detail fetch failed ({exc})")
            if (index + 1) % max(progress_every, 1) == 0 or index + 1 == len(external_ids):
                report(f"details: {index + 1}/{len(external_ids)} fetched, {failures} failed")
    return records, warnings, failures


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
# Comparison with the previous export


def record_external_id(page_id: str, record: dict[str, Any]) -> str:
    return str(record.get("id") if page_id != PAGE_OWNER else record.get("owner_key"))


def load_previous_records(page_id: str, path: Path) -> dict[str, dict[str, Any]]:
    """Return the previous export's records by ID; empty on a first run.

    An existing file that cannot be read fails the run so the removal guard is
    never skipped silently.
    """
    if not path.exists():
        return {}
    previous: dict[str, dict[str, Any]] = {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))["data"]["records"]
        if not isinstance(records, list):
            raise TypeError("'data.records' is not a list")
        for record in records:
            key = record_external_id(page_id, record)
            if key in previous:
                raise ValueError(f"duplicate external id {key}")
            previous[key] = record
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ScrapeError(f"{page_id}: cannot read previous output {path.name}: {exc!r}") from exc
    return previous


def keep_previous_for_failed(
    catalog_ids: Iterable[int],
    new_records: dict[int, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> list[str]:
    """Fill catalog IDs whose detail fetch failed from the previous export.

    Mutates new_records and returns one warning per kept record. IDs with no
    previous record stay missing so the caller can fail the run.
    """
    warnings = []
    for external_id in sorted(set(catalog_ids) - set(new_records)):
        old = previous.get(str(external_id))
        if old is not None:
            new_records[external_id] = old
            warnings.append(f"id {external_id}: kept previous record because the detail fetch failed")
    return warnings


def compare_with_previous(
    page_id: str,
    records: list[dict[str, Any]],
    previous_path: Path,
    allow_large_removal: bool = False,
) -> dict[str, int]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        external_id = record_external_id(page_id, record)
        if external_id in by_id:
            raise ScrapeError(f"{page_id}: duplicate external id {external_id}")
        by_id[external_id] = record

    existing = load_previous_records(page_id, previous_path)
    removed_ids = set(existing) - set(by_id)
    if existing and len(removed_ids) / len(existing) > REMOVAL_LIMIT and not allow_large_removal:
        raise ScrapeError(
            f"{page_id}: removing {len(removed_ids)}/{len(existing)} exceeds "
            f"the {REMOVAL_LIMIT:.0%} safety limit"
        )

    counts = {"new": 0, "updated": 0, "unchanged": 0, "removed": len(removed_ids)}
    for external_id, record in by_id.items():
        previous = existing.get(external_id)
        if previous is None:
            counts["new"] += 1
        elif semantic_hash(previous) != semantic_hash(record):
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
    return counts


# ---------------------------------------------------------------------------
# Exports


def write_output(path: Path, envelope: dict[str, Any]) -> None:
    # Write to a temp file and rename, so a crash never leaves a half-written export.
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
        try:
            json.dump(envelope, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        except BaseException:
            handle.close()
            temporary_path.unlink(missing_ok=True)
            raise
    temporary_path.replace(path)


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
    page_id: str,
    records: list[dict[str, Any]],
    output_path: Path,
    dataset: str,
    run_id: str,
    scraped_at: str,
    warnings: list[str],
) -> int:
    """Write one export: records in ID order, keys sorted recursively."""
    exported = [json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True)) for record in records]
    if page_id == PAGE_APP_TECH:
        exported.sort(key=lambda record: record["id"])
    elif page_id == PAGE_OWNER:
        exported.sort(key=lambda record: record["owner_key"])

    envelope = base_envelope(dataset, run_id, scraped_at, warnings)
    envelope["record_count"] = len(exported)
    envelope["data"] = {"records": exported}
    write_output(output_path, envelope)
    return len(exported)


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
    output_dir = Path(args.output_dir)
    summary: dict[str, Any] = {"run_id": run_id, "warnings": []}

    try:
        session = create_session(args.user_agent)
        hq_provinces = load_hq_provinces()

        listings, total = collect_catalog(session, report)
        report(f"catalog complete: {total} unique ids")

        # Hash and persist the same stripped shape so delta detection is stable.
        listing_rows = [strip_pii(dict(row)) for row in listings.values()]
        stripped_listings = {row["id"]: row for row in listing_rows}

        # Every detail is re-fetched on every run.
        needed = sorted(stripped_listings)
        report(f"details to fetch: {len(needed)} of {total}")

        new_records, fetched_warnings, _ = collect_details(
            needed[: args.limit] if args.limit else needed,
            stripped_listings,
            hq_provinces,
            user_agent=args.user_agent,
            workers=args.workers,
            delay=args.delay,
            progress_every=args.progress_every,
            report=report,
        )

        if args.limit:
            summary["status"] = f"smoke_test: processed first {min(len(needed), args.limit)} details"
            report(summary["status"])
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        fetched_warnings.extend(
            keep_previous_for_failed(
                listings, new_records, load_previous_records(PAGE_APP_TECH, output_dir / "app_tech.json")
            )
        )
        missing = set(listings) - set(new_records)
        if missing:
            sample = ", ".join(str(value) for value in sorted(missing)[:5])
            raise ScrapeError(
                f"{len(missing)} catalog ids have no usable record (sample: {sample})"
            )

        records = [new_records[external_id] for external_id in sorted(new_records)]

        stats = collect_statistics(session)
        reconciliation = reconcile_statistics(stats, records)
        summary["reconciliation"] = reconciliation

        counts_detail = compare_with_previous(
            PAGE_APP_TECH, records, output_dir / "app_tech.json", args.allow_large_removal
        )
        title_to_ids = build_title_index(listings)
        owners = collect_owner_profiles(session, title_to_ids, summary["warnings"])
        counts_owners = compare_with_previous(
            PAGE_OWNER, owners, output_dir / "owner_profiles.json", args.allow_large_removal
        )

        summary["counts"] = {
            PAGE_APP_TECH: counts_detail,
            PAGE_OWNER: counts_owners,
        }
        summary["warnings"].extend(fetched_warnings)

        scraped_at = utc_now()
        dataset_warnings = list(summary["warnings"]) + derive_record_warnings(records)
        export_page_records(
            PAGE_APP_TECH, records, output_dir / "app_tech.json", PAGE_APP_TECH,
            run_id, scraped_at, dataset_warnings,
        )
        stats_envelope = base_envelope("statistics", run_id, scraped_at, [])
        stats_envelope["reconciliation"] = reconciliation
        stats_envelope["data"] = stats
        write_output(output_dir / "statistics.json", stats_envelope)
        export_page_records(
            PAGE_OWNER, owners, output_dir / "owner_profiles.json", PAGE_OWNER,
            run_id, scraped_at, summary["warnings"],
        )

        summary["status"] = "success"
        report(
            f"success: {counts_detail['new']} new, {counts_detail['updated']} updated, "
            f"{counts_detail['unchanged']} unchanged app_tech records; "
            f"{counts_owners['new']} new owner profiles"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - run-level failure boundary
        summary["status"] = "failed"
        summary["error"] = str(exc)
        report(f"FAILED: {exc}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape the public RinMP AppTech registry.")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Seconds each worker pauses after every request."
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, help=f"Concurrent detail requests, 1 to {MAX_WORKERS}."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Smoke-test: process only the first N details; writes nothing.",
    )
    parser.add_argument(
        "--progress-every", type=int, default=25, help="Report progress every Nth detail fetch."
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--allow-large-removal", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.delay < 0 or args.limit < 0 or args.progress_every < 1:
        raise SystemExit("--delay and --limit must be non-negative; --progress-every must be >= 1")
    if not 1 <= args.workers <= MAX_WORKERS:
        raise SystemExit(f"--workers must be between 1 and {MAX_WORKERS}")
    return run_scrape(args)


if __name__ == "__main__":
    sys.exit(main())
