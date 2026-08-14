"""Incremental scraper for the public www.culturalmapthailand.info pages.

The scraper intentionally stays on the www host. It collects the Map/Inspiration
JSON feed, Product category pages, Activity and Re-Creation archives, and Team.
SQLite stores the latest clean record state; five JSON files are human-readable
exports of that active state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
STATE_PATH = ROOT / "state" / "culturalmap_www.sqlite3"
MAP_LOOKUP_PATH = ROOT / "references" / "map_inspiration_lookups_v1.json"
BASE_URL = "https://www.culturalmapthailand.info/"
MAP_FEED_URL = (
    "https://www.culturalmapthailand.info/"
    "json-mark.php?key=&CatId=&CulTypeId=&CulProvince=&UniCode="
)
DP_MEDIA_BASE_URL = "https://dp.culturalmapthailand.info/file-upload/"
DEFAULT_SCHEMA_VERSION = 1
MAP_SCHEMA_VERSION = 2
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_USER_AGENT = "CulturalMapWWWCollector/1.0 (contact: replace-with-your-email)"
PLACEHOLDER_TEXT = {"-", "ไม่มี", "-ไม่มี-"}


@dataclass(frozen=True)
class SourceDefinition:
    page_id: str
    output_name: str
    schema_version: int
    source_urls: tuple[str, ...]
    minimum_records: int
    collect: Callable[[requests.Session, float, int], list[dict[str, Any]]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(user_agent: str) -> requests.Session:
    """Create one polite GET-only session with retries for temporary failures."""
    retry_policy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    adapter = HTTPAdapter(max_retries=retry_policy)
    session.mount("https://", adapter)
    return session


def fetch_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=30)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def fetch_json(session: requests.Session, url: str) -> Any:
    response = session.get(url, timeout=45)
    response.raise_for_status()
    response.encoding = "utf-8-sig"
    return json.loads(response.text.lstrip("\ufeff"))


def clean_text(value: Any) -> Any:
    """Normalize textual source values without rewriting meaningful content."""
    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFC", value).replace("\ufeff", "").replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", normalized).strip()


def optional_text(value: Any) -> str | None:
    cleaned = clean_text(value)
    if not isinstance(cleaned, str) or not cleaned or cleaned in PLACEHOLDER_TEXT:
        return None
    return cleaned


def clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    return clean_text(value)


def absolute_url(value: str | None, page_url: str) -> str | None:
    if not value:
        return None
    url = urljoin(page_url, value.strip())
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def is_www_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == "www.culturalmapthailand.info"


def external_id_from_url(url: str, prefix: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    match = re.search(rf"/{re.escape(prefix)}-(\d+)$", path, flags=re.IGNORECASE)
    return f"{prefix}-{match.group(1)}" if match else None


def natural_id_key(record: dict[str, Any]) -> tuple[str, int]:
    prefix, _, number = record["external_id"].rpartition("-")
    return prefix, int(number) if number.isdigit() else 0


def semantic_hash(value: Any) -> str:
    """Hash meaningful content while ignoring changing public view counters."""

    def remove_volatile_fields(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: remove_volatile_fields(child)
                for key, child in item.items()
                if key not in {"view_count", "scraped_at", "run_id"}
            }
        if isinstance(item, list):
            return [remove_volatile_fields(child) for child in item]
        return item

    canonical = json.dumps(
        remove_volatile_fields(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stable_team_id(group: str, name: str) -> str:
    seed = f"{group}|{name}".encode()
    return "TEAM-" + hashlib.sha256(seed).hexdigest()[:16]


def parse_integer(value: Any, warnings: list[str], field: str) -> int | None:
    text = optional_text(value)
    if text is None:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        warnings.append(f"Invalid integer in {field}: {text}")
        return None


def parse_coordinate(value: Any, warnings: list[str], field: str) -> float | None:
    text = optional_text(value)
    if text is None:
        return None
    try:
        coordinate = float(text.strip("()[]"))
    except ValueError:
        warnings.append(f"Invalid coordinate in {field}: {text}")
        return None
    maximum = 90 if field == "latitude" else 180
    if not -maximum <= coordinate <= maximum:
        warnings.append(f"Out-of-range coordinate in {field}: {text}")
        return None
    return coordinate


def clean_links(soup: Tag, page_url: str) -> list[dict[str, str]]:
    """Keep published external links as metadata without following them."""
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = absolute_url(str(anchor.get("href", "")), page_url)
        if not url or is_www_url(url) or url in seen:
            continue
        seen.add(url)
        links.append({"label": optional_text(anchor.get_text(" ", strip=True)) or url, "url": url})
    return links


def gallery_images(soup: Tag, page_url: str) -> list[dict[str, str | None]]:
    images: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[data-fancybox]"):
        url = absolute_url(str(anchor.get("href") or anchor.get("data-src") or ""), page_url)
        if not url or url in seen:
            continue
        seen.add(url)
        images.append({"url": url, "caption": optional_text(anchor.get("data-caption"))})
    return images


def text_between(text: str, start: str, stop_labels: Iterable[str]) -> str | None:
    start_at = text.find(start)
    if start_at < 0:
        return None
    value = text[start_at + len(start) :]
    stops = [position for label in stop_labels if (position := value.find(label)) >= 0]
    return optional_text(value[: min(stops)] if stops else value)


def find_first_text(soup: Tag, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            value = optional_text(element.get_text(" ", strip=True))
            if value:
                return value
    return None


def build_record(
    external_id: str,
    title: str,
    source_url: str,
    discovered_from: list[str],
    data: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    if not external_id or not title or not is_www_url(source_url):
        raise ValueError("Record is missing a stable ID, title, or canonical www source URL")
    return {
        "external_id": external_id,
        "title": title,
        "source_url": source_url,
        "discovered_from": discovered_from,
        "data": clean_value(data),
        "validation_warnings": sorted(set(warnings)),
    }


@lru_cache(maxsize=1)
def load_map_lookups() -> dict[str, Any]:
    """Load the pinned DOPA and Cultural Map lookup data once per run."""
    with MAP_LOOKUP_PATH.open(encoding="utf-8") as file:
        lookup = json.load(file)
    required_keys = {"dopa", "cultural_taxonomy", "local_administrative_crosswalk"}
    if not isinstance(lookup, dict) or not required_keys <= set(lookup):
        raise ValueError(f"Invalid Map/Inspiration lookup file: {MAP_LOOKUP_PATH}")
    return lookup


def normalize_source_date(value: Any, field: str, warnings: list[str]) -> str | None:
    """Turn published Gregorian or Buddhist Era dates into ISO text."""
    text = optional_text(value)
    if text is None or re.fullmatch(r"0+(?:-0+){2}(?:[ T]0+(?::0+){2})?", text):
        return None
    text = text.rstrip(".")
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?", text)
    slash_match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if slash_match is not None:
        day, month, year = (int(slash_match.group(index)) for index in (1, 2, 3))
        hour = minute = second = 0
    elif match is not None:
        year, month, day = (int(match.group(index)) for index in (1, 2, 3))
        hour, minute, second = (int(match.group(index) or 0) for index in (4, 5, 6))
    else:
        warnings.append(f"Invalid date in {field}: {text}")
        return None
    if year >= 2400:
        year -= 543
    try:
        parsed = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        warnings.append(f"Invalid date in {field}: {text}")
        return None
    return parsed.replace(tzinfo=None).isoformat(timespec="seconds") if match and match.group(4) else parsed.date().isoformat()


def normalize_keywords(value: Any) -> list[str]:
    raw = optional_text(value)
    if raw is None:
        return []
    return list(dict.fromkeys(part.strip() for part in re.split(r"[,;|\n]+", raw) if part.strip()))


def normalize_published_urls(value: Any, field: str, unparsed: dict[str, str]) -> list[dict[str, str]]:
    raw = optional_text(value)
    if raw is None:
        return []
    candidates = re.findall(r"(?:https?://|www\.)[^\s,;]+", raw, flags=re.IGNORECASE)
    urls: list[dict[str, str]] = []
    for candidate in candidates:
        url = candidate.rstrip(".);,")
        if url.lower().startswith("www."):
            url = f"https://{url}"
        if absolute_url(url, BASE_URL):
            urls.append({"url": url})
    if not urls:
        unparsed[field] = raw
    return urls


def map_images(source: dict[str, Any]) -> list[dict[str, str | None | bool]]:
    images: list[dict[str, str | None | bool]] = []
    university_code = optional_text(source.get("UniCode")) or ""
    record_id = optional_text(source.get("CId")) or ""
    for index in range(1, 6):
        filename = optional_text(source.get(f"Culimg{index}"))
        if not filename:
            continue
        images.append(
            {
                "url": f"{DP_MEDIA_BASE_URL}{university_code}/{record_id}/{filename}",
                "filename": filename,
                "caption": optional_text(source.get(f"CulimgDetail{index}")),
                "fetched": False,
            }
        )
    return images


def labelled_value(
    code: str | None,
    table: dict[str, Any],
    label: str,
    warnings: list[str],
) -> dict[str, str | None]:
    value = table.get(code or "")
    if code and not isinstance(value, dict):
        warnings.append(f"Unknown {label} code: {code}")
    return {
        "code": code,
        "name_th": optional_text(value.get("name_th")) if isinstance(value, dict) else None,
        "name_en": optional_text(value.get("name_en")) if isinstance(value, dict) else None,
    }


def administrative_unit(
    code: str,
    table: dict[str, Any],
    mapping_method: str,
) -> dict[str, str | None] | None:
    value = table.get(code)
    if not isinstance(value, dict):
        return None
    name_th = optional_text(value.get("name_th"))
    return {
        "code": code,
        "name_th": name_th.rstrip("*").strip() if name_th else None,
        "name_en": optional_text(value.get("name_en")),
        "mapping_method": mapping_method,
    }


def validate_local_crosswalk(
    province_id: str | None,
    amphure_id: str | None,
    tambon_code: str,
    crosswalk: dict[str, Any],
    warnings: list[str],
) -> None:
    province = crosswalk["provinces"].get(province_id or "")
    if province and province["official_code"] != tambon_code[:2]:
        warnings.append(
            "Local Province ID conflicts with DOPA Tambon code: "
            f"{province_id} vs {tambon_code}"
        )
    if province_id and amphure_id:
        amphure = crosswalk["amphures"].get(f"{province_id}|{amphure_id}")
        if amphure and amphure["official_code"] != tambon_code[:4]:
            warnings.append(
                "Local Amphure ID conflicts with DOPA Tambon code: "
                f"{province_id}|{amphure_id} vs {tambon_code}"
            )


def administrative_location(
    source: dict[str, Any],
    lookups: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    dopa = lookups["dopa"]
    crosswalk = lookups["local_administrative_crosswalk"]
    province_id = optional_text(source.get("CulProvince"))
    amphure_id = optional_text(source.get("CulAmphure"))
    tambon_code = optional_text(source.get("CulDistrict"))
    if tambon_code and re.fullmatch(r"\d{6}", tambon_code):
        province = administrative_unit(tambon_code[:2], dopa["provinces"], "dopa_tambon_code")
        amphure = administrative_unit(tambon_code[:4], dopa["amphures"], "dopa_tambon_code")
        tambon = administrative_unit(tambon_code, dopa["tambons"], "dopa_tambon_code")
        if province is None or amphure is None or tambon is None:
            warnings.append(f"Unknown DOPA administrative code: {tambon_code}")
        validate_local_crosswalk(province_id, amphure_id, tambon_code, crosswalk, warnings)
        return {"province": province, "amphure": amphure, "tambon": tambon}

    if tambon_code:
        warnings.append(f"Invalid standard Tambon code: {tambon_code}")
    province_crosswalk = crosswalk["provinces"].get(province_id or "")
    province = (
        administrative_unit(
            province_crosswalk["official_code"],
            dopa["provinces"],
            "verified_local_province_crosswalk",
        )
        if province_crosswalk
        else None
    )
    amphure_crosswalk = crosswalk["amphures"].get(f"{province_id}|{amphure_id}") if amphure_id else None
    amphure = (
        administrative_unit(
            amphure_crosswalk["official_code"],
            dopa["amphures"],
            "verified_local_amphure_crosswalk",
        )
        if amphure_crosswalk
        else None
    )
    if province is None:
        warnings.append(f"Unknown local Province ID: {province_id}")
    if amphure_id and amphure is None:
        warnings.append(f"Unknown local Amphure ID: {province_id}|{amphure_id}")
    if province and amphure:
        warnings.append("Missing standard Tambon code; Province and Amphure resolved from verified local-ID crosswalk")
    elif province:
        warnings.append("Missing standard Tambon code; Province resolved from verified local-ID crosswalk")
    else:
        warnings.append("Missing standard Tambon code; administrative location could not be resolved")
    return {"province": province, "amphure": amphure, "tambon": None}


def map_classification(source: dict[str, Any], lookups: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    taxonomy = lookups["cultural_taxonomy"]
    type_code = (optional_text(source.get("CulTypeId")) or "").upper() or None
    category_code = (optional_text(source.get("CatId")) or "").upper() or None
    additional_codes = [
        code.upper()
        for code in re.split(r"[,;\s]+", optional_text(source.get("CatIdOther")) or "")
        if code
    ]
    return {
        "cultural_type": labelled_value(type_code, taxonomy["types"], "cultural type", warnings),
        "primary_category": labelled_value(
            category_code,
            taxonomy["primary_categories"],
            "primary category",
            warnings,
        ),
        "additional_categories": [{"code": code} for code in dict.fromkeys(additional_codes)],
        "additional_category_detail": optional_text(source.get("CatIdOtherDetail")),
    }


def map_funding(source: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    fund_type = optional_text(source.get("FundType"))
    if fund_type and fund_type.lower() == "y":
        funded: bool | None = True
        status = "funded"
    elif fund_type is None:
        funded = None
        status = "not_indicated"
    else:
        funded = None
        status = "unrecognized_source_value"
        warnings.append(f"Unknown FundType value: {fund_type}")
    return {
        "project_funded": funded,
        "status": status,
        "fund_type": fund_type,
        "program_id": optional_text(source.get("BudId")),
    }


def collect_map_inspiration(
    session: requests.Session, delay_seconds: float, limit: int = 0
) -> list[dict[str, Any]]:
    del delay_seconds
    payload = fetch_json(session, MAP_FEED_URL)
    if not isinstance(payload, list):
        raise TypeError("Map feed did not return a JSON array")

    lookups = load_map_lookups()
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for source in payload:
        if not isinstance(source, dict):
            continue
        source_id = optional_text(source.get("CId"))
        title = optional_text(source.get("CulTName"))
        if source_id is None or title is None:
            continue
        external_id = f"CD-{source_id}"
        if external_id in seen_ids:
            continue
        seen_ids.add(external_id)

        warnings: list[str] = []
        latitude = parse_coordinate(source.get("CulLocationLa"), warnings, "latitude")
        longitude = parse_coordinate(source.get("CulLocationLo"), warnings, "longitude")
        if (latitude is None) != (longitude is None):
            warnings.append("Only one coordinate was published")

        unparsed_media: dict[str, str] = {}
        account_identifier = optional_text(source.get("UserName"))
        institution_code = optional_text(source.get("UniCode"))
        institution = lookups["cultural_taxonomy"]["institutions"].get(institution_code or "")
        data = {
            "page_roles": ["map", "inspiration"],
            "identifiers": {
                "record_code": optional_text(source.get("CulCodeNew")),
                "legacy_record_code": optional_text(source.get("CulCode")),
            },
            "names": {"th": title, "en": optional_text(source.get("CulEName"))},
            "classification": map_classification(source, lookups, warnings),
            "description": {
                "history": optional_text(source.get("CulHistory")),
                "identity": optional_text(source.get("CulIdentility")),
                "potential": optional_text(source.get("CulPotentials")),
                "limitations": optional_text(source.get("CulLimitations")),
                "stakeholders": optional_text(source.get("CulStakeholders")),
            },
            "location": {
                "address_raw": optional_text(source.get("CulAddr")),
                "postal_code": optional_text(source.get("CulPostalCode")),
                "coordinates": {"latitude": latitude, "longitude": longitude},
                "administrative": administrative_location(source, lookups, warnings),
            },
            "people": {
                "informants_raw": optional_text(source.get("Culinformant")),
                "contact_raw": optional_text(source.get("CulinforContact")),
                "recorder": {
                    "name": optional_text(source.get("UserNameRecord")),
                    "account_identifier": account_identifier,
                    "account_identifier_type": (
                        "email"
                        if account_identifier and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", account_identifier)
                        else "username"
                        if account_identifier
                        else None
                    ),
                    "institution": {
                        "code": institution_code,
                        "name_th": (
                            optional_text(institution.get("name_th"))
                            if isinstance(institution, dict)
                            else optional_text(source.get("UniNameRecord"))
                        ),
                    },
                },
            },
            "funding": map_funding(source, warnings),
            "media": {
                "images": map_images(source),
                "links": normalize_published_urls(source.get("CulLink"), "link", unparsed_media),
                "clips": normalize_published_urls(source.get("CulClip"), "clip", unparsed_media),
                "narration_links": normalize_published_urls(source.get("CulSound"), "sound", unparsed_media),
                "documents": normalize_published_urls(source.get("CulDocument"), "document", unparsed_media),
                "unparsed_values": unparsed_media,
            },
            "assessment": {
                "risk": {
                    "status_code": parse_integer(source.get("CulStatusRisk"), warnings, "risk status"),
                    "reason": optional_text(source.get("CulReasonRisk")),
                },
                "swot": {
                    "strengths": optional_text(source.get("CulStreng")),
                    "weaknesses": optional_text(source.get("CulWeak")),
                    "opportunities": optional_text(source.get("CulOpport")),
                    "threats": optional_text(source.get("CulThreats")),
                },
                "research_features": optional_text(source.get("CulRsFeature")),
                "research_assessment": optional_text(source.get("CulRsAss")),
            },
            "keywords": normalize_keywords(source.get("CulKeywords")),
            "dates": {
                "recorded": normalize_source_date(source.get("DOR"), "DOR", warnings),
                "inserted": normalize_source_date(source.get("InsDate"), "InsDate", warnings),
                "updated": normalize_source_date(source.get("EditDate"), "EditDate", warnings),
            },
            "metrics": {
                "view_count": parse_integer(source.get("count_view"), warnings, "view_count"),
                "rating_scale": parse_integer(source.get("RatingScale"), warnings, "rating_scale"),
            },
        }
        records.append(
            build_record(
                external_id,
                title,
                f"{BASE_URL}CD-{source_id}",
                [MAP_FEED_URL],
                data,
                warnings,
            )
        )
    return records[:limit] if limit else records


def discover_links(html: str, page_url: str, prefix: str) -> dict[str, dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    results: dict[str, dict[str, Any]] = {}
    for anchor in soup.select("a[href]"):
        url = absolute_url(str(anchor.get("href", "")), page_url)
        if not url or not is_www_url(url):
            continue
        external_id = external_id_from_url(url, prefix)
        if external_id is None:
            continue
        item = results.setdefault(
            external_id,
            {
                "external_id": external_id,
                "source_url": url,
                "title": optional_text(anchor.get_text(" ", strip=True)),
                "discovered_from": [],
            },
        )
        if page_url not in item["discovered_from"]:
            item["discovered_from"].append(page_url)
        if not item["title"]:
            item["title"] = optional_text(anchor.get_text(" ", strip=True))
    return results


def product_category_urls(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    categories: dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        url = absolute_url(str(anchor.get("href", "")), page_url)
        if not url or not is_www_url(url):
            continue
        category_id = external_id_from_url(url, "P")
        if category_id:
            categories[category_id] = url
    return categories


def collect_products(
    session: requests.Session, delay_seconds: float, limit: int = 0
) -> list[dict[str, Any]]:
    root_url = f"{BASE_URL}culturalproduct"
    root_html = fetch_text(session, root_url)
    categories = product_category_urls(root_html, root_url)
    required_categories = {f"P-{number}" for number in range(1, 7)}
    missing_categories = required_categories - set(categories)
    if missing_categories:
        raise ValueError(f"Product discovery is missing required categories: {sorted(missing_categories)}")

    discovered = discover_links(root_html, root_url, "PD")
    for category_id in sorted(required_categories, key=lambda value: int(value.split("-")[1])):
        category_url = categories[category_id]
        category_html = fetch_text(session, category_url)
        for external_id, item in discover_links(category_html, category_url, "PD").items():
            if external_id not in discovered:
                discovered[external_id] = item
            else:
                discovered[external_id]["discovered_from"].extend(
                    url for url in item["discovered_from"] if url not in discovered[external_id]["discovered_from"]
                )

    records = []
    items = sorted(discovered.values(), key=natural_id_key)
    if limit:
        items = items[:limit]
    for item in items:
        detail_html = fetch_text(session, item["source_url"])
        records.append(parse_product_detail(detail_html, item))
        time.sleep(delay_seconds)
    return records


def parse_product_detail(html: str, item: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(".culturalproductinfo")
    if container is None:
        raise ValueError(f"Product detail {item['external_id']} is missing .culturalproductinfo")
    title = find_first_text(container, (".mycontainer2header", "h4.display-9")) or item.get("title")
    if not title:
        raise ValueError(f"Product detail {item['external_id']} is missing a title")
    text = clean_text(container.get_text(" ", strip=True))
    category = next(
        (
            optional_text(anchor.get_text(" ", strip=True))
            for anchor in container.select("a[href]")
            if external_id_from_url(absolute_url(str(anchor.get("href", "")), item["source_url"]) or "", "P")
        ),
        None,
    )
    related_cultural = next(
        (
            external_id_from_url(absolute_url(str(anchor.get("href", "")), item["source_url"]) or "", "CD")
            for anchor in container.select("a[href]")
            if external_id_from_url(absolute_url(str(anchor.get("href", "")), item["source_url"]) or "", "CD")
        ),
        None,
    )
    warnings: list[str] = []
    view_match = re.search(r"เข้าชม\s*:?\s*([\d,]+)\s*ครั้ง", text)
    price_match = re.search(r"ราคา\s*:?\s*(.+?)(?=\s*รายละเอียด\s*:|$)", text)
    address_match = re.search(r"เลขที่\s*:?\s*(.+)$", text)
    return build_record(
        item["external_id"],
        title,
        item["source_url"],
        item["discovered_from"],
        {
            "product_category": category,
            "price_text": optional_text(price_match.group(1)) if price_match else None,
            "description": text_between(text, "รายละเอียด :", ("ช่องทางการจำหน่าย", "เข้าชม", "ผลิตภัณฑ์จากวัฒนธรรม", "เลขที่ :")),
            "sales_channels": text_between(text, "ช่องทางการจำหน่าย", ("เข้าชม", "ผลิตภัณฑ์จากวัฒนธรรม", "เลขที่ :")),
            "address_text": optional_text(address_match.group(1)) if address_match else None,
            "related_cultural_record": related_cultural,
            "external_links": clean_links(container, item["source_url"]),
            "gallery_images": gallery_images(container, item["source_url"]),
            "view_count": parse_integer(view_match.group(1), warnings, "view_count") if view_match else None,
        },
        warnings,
    )


def collect_activities(
    session: requests.Session, delay_seconds: float, limit: int = 0
) -> list[dict[str, Any]]:
    listing_url = f"{BASE_URL}activity"
    discovered = discover_links(fetch_text(session, listing_url), listing_url, "G")
    records = []
    items = sorted(discovered.values(), key=natural_id_key)
    if limit:
        items = items[:limit]
    for item in items:
        detail_html = fetch_text(session, item["source_url"])
        records.append(parse_activity_detail(detail_html, item))
        time.sleep(delay_seconds)
    return records


def parse_activity_detail(html: str, item: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = find_first_text(soup, (".mycontainer2header.smallheaderfontsize", "h4.display-9")) or item.get("title")
    container = soup.select_one(".wrapword")
    if not title or container is None:
        raise ValueError(f"Activity detail {item['external_id']} is missing published record structure")
    text = clean_text(container.get_text(" ", strip=True))
    date_match = re.search(r"(วัน[^.]{0,100}?\d{4})", text)
    return build_record(
        item["external_id"],
        title,
        item["source_url"],
        item["discovered_from"],
        {
            "date_text": optional_text(date_match.group(1)) if date_match else None,
            "description": optional_text(text),
            "external_links": clean_links(container, item["source_url"]),
            "gallery_images": gallery_images(soup, item["source_url"]),
        },
        [],
    )


def collect_recreation(
    session: requests.Session, delay_seconds: float, limit: int = 0
) -> list[dict[str, Any]]:
    listing_url = f"{BASE_URL}ReAll"
    discovered = discover_links(fetch_text(session, listing_url), listing_url, "REDetail")
    records = []
    items = sorted(discovered.values(), key=natural_id_key)
    if limit:
        items = items[:limit]
    for item in items:
        detail_html = fetch_text(session, item["source_url"])
        records.append(parse_recreation_detail(detail_html, item))
        time.sleep(delay_seconds)
    return records


def parse_recreation_detail(html: str, item: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(".container-xxl") or soup.select_one(".mycontainer")
    title = find_first_text(container or soup, ("h4.display-9",)) or item.get("title")
    if container is None or not title:
        raise ValueError(f"Re-Creation detail {item['external_id']} is missing published record structure")
    text = clean_text(container.get_text(" ", strip=True))
    category = find_first_text(container, ("h5",))
    if category:
        category = re.sub(r"^ประเภท\s*:\s*", "", category).strip() or None
    members_text = text_between(text, "รายชื่อ :", ("Link Images :", "Link Vdo :", "ช่องทางการติดต่อ :", "ผู้บันทึกข้อมูล :", "เข้าชม"))
    members = []
    if members_text:
        members = [
            clean_text(re.sub(r"^\d+\s*[.)]\s*", "", part))
            for part in re.split(r"(?=\d+\s*[.)])", members_text)
        ]
        members = [member for member in members if member]
    view_match = re.search(r"เข้าชม\s*:?\s*([\d,]+)\s*ครั้ง", text)
    warnings: list[str] = []
    return build_record(
        item["external_id"],
        title,
        item["source_url"],
        item["discovered_from"],
        {
            "recreation_category": category,
            "recreation_title": text_between(text, "Re-creation :", ("รายละเอียด :", "การต่อยอด :", "ชื่อทีม")),
            "description": text_between(text, "รายละเอียด :", ("การต่อยอด :", "ชื่อทีม", "รายชื่อ :", "Link Images :", "เข้าชม")),
            "extension_text": text_between(text, "การต่อยอด :", ("ชื่อทีม", "รายชื่อ :", "Link Images :", "เข้าชม")),
            "team_name": text_between(text, "ชื่อทีม :", ("รายชื่อ :", "Link Images :", "เข้าชม")),
            "team_members": members,
            "external_image_link": text_between(text, "Link Images :", ("Link Vdo :", "ช่องทางการติดต่อ :", "ผู้บันทึกข้อมูล :", "เข้าชม")),
            "external_video_link": text_between(text, "Link Vdo :", ("ช่องทางการติดต่อ :", "ผู้บันทึกข้อมูล :", "เข้าชม")),
            "contact_text": text_between(text, "ช่องทางการติดต่อ :", ("ผู้บันทึกข้อมูล :", "เข้าชม")),
            "recorded_by": text_between(text, "ผู้บันทึกข้อมูล :", ("เข้าชม",)),
            "gallery_images": gallery_images(soup, item["source_url"]),
            "view_count": parse_integer(view_match.group(1), warnings, "view_count") if view_match else None,
        },
        warnings,
    )


def collect_team(
    session: requests.Session, delay_seconds: float, limit: int = 0
) -> list[dict[str, Any]]:
    del delay_seconds
    team_url = f"{BASE_URL}team"
    soup = BeautifulSoup(fetch_text(session, team_url), "html.parser")
    records: list[dict[str, Any]] = []
    current_group: str | None = None
    for heading in soup.find_all(["h2", "h6"]):
        if heading.name == "h2" and "display-6" in (heading.get("class") or []):
            current_group = optional_text(heading.get_text(" ", strip=True))
            continue
        if heading.name != "h6" or "mt-2" not in (heading.get("class") or []):
            continue
        name = optional_text(heading.get_text(" ", strip=True))
        if not current_group or not name:
            continue
        card = heading.find_parent(class_="team-item")
        image = card.select_one("img") if card else heading.find_previous("img", class_="img-thumbnail")
        image_url = absolute_url(str(image.get("src", "")), team_url) if image else None
        records.append(
            build_record(
                stable_team_id(current_group, name),
                name,
                team_url,
                [team_url],
                {"group": current_group, "profile_image_url": image_url},
                [],
            )
        )
    if not records:
        raise ValueError("Team page did not contain any expected team members")
    return records[:limit] if limit else records


SOURCES = {
    "map_inspiration": SourceDefinition(
        "map_inspiration",
        "map_inspiration.json",
        MAP_SCHEMA_VERSION,
        (f"{BASE_URL}map", MAP_FEED_URL),
        4_000,
        collect_map_inspiration,
    ),
    "products": SourceDefinition(
        "products",
        "products.json",
        DEFAULT_SCHEMA_VERSION,
        (f"{BASE_URL}culturalproduct",),
        150,
        collect_products,
    ),
    "activities": SourceDefinition(
        "activities",
        "activities.json",
        DEFAULT_SCHEMA_VERSION,
        (f"{BASE_URL}activity",),
        25,
        collect_activities,
    ),
    "recreation": SourceDefinition(
        "recreation",
        "recreation.json",
        DEFAULT_SCHEMA_VERSION,
        (f"{BASE_URL}ReAll",),
        50,
        collect_recreation,
    ),
    "team": SourceDefinition(
        "team",
        "team.json",
        DEFAULT_SCHEMA_VERSION,
        (f"{BASE_URL}team",),
        8,
        collect_team,
    ),
}


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
    return connection


def active_records(connection: sqlite3.Connection, page_id: str) -> dict[str, sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM records WHERE page_id = ? AND active = 1", (page_id,)
    ).fetchall()
    return {row["external_id"]: row for row in rows}


def sync_records(
    connection: sqlite3.Connection,
    source: SourceDefinition,
    records: list[dict[str, Any]],
    run_id: str,
    now: str,
    allow_large_removal: bool = False,
) -> dict[str, int]:
    if len(records) < source.minimum_records:
        raise ValueError(
            f"{source.page_id} discovery returned {len(records)} records; expected at least {source.minimum_records}"
        )
    by_id = {record["external_id"]: record for record in records}
    if len(by_id) != len(records):
        raise ValueError(f"{source.page_id} produced duplicate external IDs")

    existing = active_records(connection, source.page_id)
    removed_ids = set(existing) - set(by_id)
    if existing and len(removed_ids) / len(existing) > 0.25 and not allow_large_removal:
        raise ValueError(f"{source.page_id} removal exceeds the 25% safety limit")

    counts = {"new": 0, "updated": 0, "unchanged": 0, "removed": len(removed_ids)}
    with connection:
        for external_id, record in by_id.items():
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
            record_hash = semantic_hash(record)
            previous = existing.get(external_id)
            if previous is None:
                connection.execute(
                    """
                    INSERT INTO records (
                        page_id, external_id, payload_json, content_hash, first_seen_at,
                        last_seen_at, last_changed_at, last_run_id, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (source.page_id, external_id, payload, record_hash, now, now, now, run_id),
                )
                counts["new"] += 1
            elif previous["content_hash"] != record_hash:
                connection.execute(
                    """
                    UPDATE records
                    SET payload_json = ?, content_hash = ?, last_seen_at = ?,
                        last_changed_at = ?, last_run_id = ?, active = 1
                    WHERE page_id = ? AND external_id = ?
                    """,
                    (payload, record_hash, now, now, run_id, source.page_id, external_id),
                )
                counts["updated"] += 1
            else:
                connection.execute(
                    """
                    UPDATE records SET last_seen_at = ?, last_run_id = ?, active = 1
                    WHERE page_id = ? AND external_id = ?
                    """,
                    (now, run_id, source.page_id, external_id),
                )
                counts["unchanged"] += 1

        for external_id in removed_ids:
            connection.execute(
                """
                UPDATE records SET active = 0, last_seen_at = ?, last_run_id = ?
                WHERE page_id = ? AND external_id = ?
                """,
                (now, run_id, source.page_id, external_id),
            )
    return counts


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def export_page(connection: sqlite3.Connection, source: SourceDefinition, run_id: str, scraped_at: str) -> None:
    rows = connection.execute(
        "SELECT payload_json FROM records WHERE page_id = ? AND active = 1", (source.page_id,)
    ).fetchall()
    records = [json.loads(row["payload_json"]) for row in rows]
    records.sort(key=natural_id_key)
    write_json(
        OUTPUT_DIR / source.output_name,
        {
            "schema_version": source.schema_version,
            "page_id": source.page_id,
            "source_urls": list(source.source_urls),
            "scraped_at": scraped_at,
            "run_id": run_id,
            "warnings": [],
            "data": {"records": records},
        },
    )


def select_sources(name: str) -> list[SourceDefinition]:
    if name == "all":
        return list(SOURCES.values())
    return [SOURCES[name]]


def run_scrape(args: argparse.Namespace) -> int:
    selected_sources = select_sources(args.source)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    connection = open_database(STATE_PATH)
    session = create_session(args.user_agent)
    summary: dict[str, Any] = {"run_id": run_id, "sources": {}, "warnings": []}
    started_at = utc_now()
    connection.execute(
        "INSERT INTO runs (run_id, started_at, status, summary_json) VALUES (?, ?, ?, ?)",
        (run_id, started_at, "running", "{}"),
    )
    connection.commit()

    if "replace-with-your-email" in args.user_agent:
        summary["warnings"].append("Set --user-agent to an identifying value with a contact address before production use.")

    has_failures = False
    for source in selected_sources:
        try:
            records = source.collect(session, args.delay, args.limit)
            if args.limit:
                summary["sources"][source.page_id] = {
                    "status": "smoke_test",
                    "discovered": len(records),
                    "state_updated": False,
                }
                continue
            counts = sync_records(
                connection,
                source,
                records,
                run_id,
                utc_now(),
                allow_large_removal=args.allow_large_removal,
            )
            export_page(connection, source, run_id, utc_now())
            summary["sources"][source.page_id] = {
                "status": "completed",
                "discovered": len(records),
                "state_updated": True,
                **counts,
            }
        except Exception as error:  # noqa: BLE001 - each source must report its own failure.
            has_failures = True
            summary["sources"][source.page_id] = {
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "state_updated": False,
            }

    status = "failed" if has_failures else "completed"
    finished_at = utc_now()
    connection.execute(
        "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE run_id = ?",
        (finished_at, status, json.dumps(summary, ensure_ascii=False), run_id),
    )
    connection.commit()
    connection.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if has_failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape public www.culturalmapthailand.info content.")
    parser.add_argument("--source", choices=("all", *SOURCES), default="all")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--limit", type=int, default=0, help="Smoke-test record limit; does not update state or JSON outputs.")
    parser.add_argument(
        "--allow-large-removal",
        action="store_true",
        help="Allow an update that would deactivate more than 25% of an existing source.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.delay < 0 or args.limit < 0:
        raise SystemExit("--delay and --limit must be non-negative")
    return run_scrape(args)


if __name__ == "__main__":
    sys.exit(main())
