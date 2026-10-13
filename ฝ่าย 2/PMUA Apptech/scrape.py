"""Incremental scraper for the public PMUA AppTech platform (pmua-apptech.com).

The national บพท./PMU-A platform catalogs university technology-transfer
centers (ศูนย์), their innovations (เทคโนโลยี / นวัตกรรม), expert researchers,
partner universities, and three public dashboards. Everything relevant is
server-rendered HTML or embedded inline JSON — no login, no JavaScript
rendering. Each run is compared with the previous JSON export in output/,
which is the only state kept.

Verified source facts this scraper relies on (2026-08-23):
- sitemap.xml is stale (827 of 1,172 products) and homepage stat cards are
  stale (54 universities / 596 researchers vs real 66 / 539); neither is
  used as a data source.
- invalid product IDs return HTTP 302, so IDs come from search pagination.
- `/search?q=&page=N` returns the full catalog: 16 rows/page, last page holds
  the remainder; the page itself reports the total ("พบข้อมูล N รายการ").
- `/welcome/researchers` and `/welcome/universities` paginate through a
  misleadingly named `per_page` parameter that actually acts as a page
  offset (0, 12, 24, …).
- product detail pages carry deployment areas as hidden numeric codes
  (csv-provinces/csv-districts/csv-tumbons) resolvable via the public JSON
  dictionaries under /api/form/.
- the three dashboards embed pre-computed aggregates as inline JSON
  (`const provData = {...}` / `const provinceData = {...}`); the family
  dashboard's economic totals are separate server-rendered HTML cards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

BASE_URL = "https://pmua-apptech.com/"
SEARCH_URL = BASE_URL + "search"
CENTERS_URL = BASE_URL + "station/centers"
PRODUCT_DETAIL_URL = BASE_URL + "product/show/{product_id}"
STATION_DETAIL_URL = BASE_URL + "station/public_profile/{station_id}"
RESEARCHERS_URL = BASE_URL + "welcome/researchers"
UNIVERSITIES_URL = BASE_URL + "welcome/universities"
AREA_API_BASE = BASE_URL + "api/form"

DIRECTORY_PAGE_SIZE = 12  # Both directories render 12 cards per offset page.
DEFAULT_DELAY_SECONDS = 0.0  # Pause after each request, per worker.
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_USER_AGENT = "PmuaApptechCollector"
PLACEHOLDER_TEXT = {"-", "ไม่มี", "-ไม่มี-", "N/A"}
REMOVAL_SAFETY_RATIO = 0.25
REMOVAL_SAFETY_MINIMUM = (
    10  # Small catalogs fluctuate legitimately; guard only established ones.
)
DEFAULT_SCHEMA_VERSION = 1

# Documented anchors used as validation targets with tolerance, not equality:
# the platform grows over time, but sudden large drops mean something broke.
ANCHOR_COUNTS = {
    "product": 1172,
    "station": 31,
    "researcher": 539,
    "university": 66,
}
COUNT_WARNING_RATIO = 0.1

ProgressReporter = Callable[[str], None]


# ---------------------------------------------------------------------------
# Shared infrastructure (ported from the sibling scrapers)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def no_progress(_: str) -> None:
    """Default reporter for direct function calls and offline tests."""


def create_progress_reporter(quiet: bool) -> ProgressReporter:
    """Return a timestamped stderr reporter without affecting JSON stdout."""
    if quiet:
        return no_progress

    def report(message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)

    return report


def report_detail_progress(
    report: ProgressReporter,
    dataset_id: str,
    index: int,
    total: int,
    record_id: str,
    progress_every: int,
) -> None:
    if index == 1 or index == total or index % progress_every == 0:
        report(f"{dataset_id}: {index:,}/{total:,} — fetching {record_id}")


def create_session(user_agent: str) -> requests.Session:
    """Create one polite session with retries for temporary failures."""
    retry_policy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Referer": BASE_URL})
    adapter = HTTPAdapter(max_retries=retry_policy)
    session.mount("https://", adapter)
    return session


def get_html(session: requests.Session, url: str, timeout: int = 45) -> str:
    """HTTP only: fetch one page as text. Safe to run in a worker thread."""
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


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


def clean_text(value: Any) -> Any:
    """Normalize textual source values without rewriting meaningful content."""
    if not isinstance(value, str):
        return value
    normalized = (
        unicodedata.normalize("NFC", value).replace("\ufeff", "").replace("\u00a0", " ")
    )
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


def semantic_hash(value: Any) -> str:
    """Hash meaningful content while ignoring volatile envelope fields."""

    def remove_volatile_fields(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: remove_volatile_fields(child)
                for key, child in item.items()
                if key not in {"scraped_at", "run_id"}
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


def to_int(value: str | None) -> int | None:
    """Convert digit text (optionally comma-grouped) to int; None when unparseable."""
    if value is None:
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None


def parse_thb_amount(value: str | None) -> int | None:
    """Parse an unambiguous Thai-baht amount such as '60,000.00 ฿'."""
    if value is None:
        return None
    match = re.fullmatch(r"([\d,]+)(?:\.\d+)?\s*฿?", value.strip())
    if not match:
        return None
    return to_int(match.group(1))


def build_record(
    record_type: str,
    record_id: str,
    title: str,
    data: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    if not record_id or not title:
        raise ValueError("Record is missing a stable ID or title")
    return {
        "record_type": record_type,
        "record_id": record_id,
        "title": title,
        **data,
        "validation_warnings": sorted(set(warnings)),
    }


def warn_count_drift(dataset_id: str, count: int, warnings: list[str]) -> None:
    """Flag a dataset whose size drifted far from its documented anchor."""
    anchor = ANCHOR_COUNTS.get(dataset_id)
    if anchor and abs(count - anchor) > anchor * COUNT_WARNING_RATIO:
        warnings.append(
            f"{dataset_id} count {count} drifted more than "
            f"{COUNT_WARNING_RATIO:.0%} from the documented anchor {anchor}"
        )


# ---------------------------------------------------------------------------
# Product discovery: search pagination


def parse_search_total(html: str) -> int | None:
    match = re.search(r"พบข้อมูล\s*<span[^>]*>([\d,]+)</span>\s*รายการ", html)
    return to_int(match.group(1)) if match else None


def discover_products(
    session: requests.Session,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
    workers: int = 1,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[list[dict[str, Any]], int | None]:
    """Page through /search?q=&page=N collecting unique product IDs.

    Guards: when unlimited, collected unique IDs must equal the total the
    site reports on the results page; a page that yields only already-seen
    IDs means pagination is stuck and fails the run.

    With workers > 1, once page 1 reports the total, the remaining pages are
    fetched concurrently and then checked here in page order, exactly as the
    sequential loop would check them. Pages are fetched with one worker
    session per thread; the main session only fetches page 1 and any pages
    the estimate did not cover.
    """
    ids: list[str] = []
    seen: set[str] = set()
    reported_total: int | None = None

    def search_url(page: int) -> str:
        return SEARCH_URL + (f"?q=&page={page}" if page > 1 else "?q=")

    def consume(page: int, html: str) -> bool:
        """Apply the per-page guards; True when pagination is finished."""
        nonlocal reported_total
        page_total = parse_search_total(html)
        if reported_total is None:
            reported_total = page_total
            report(f"product: search reports total={reported_total}")
        elif page_total is not None and page_total != reported_total:
            raise ValueError(
                f"product: reported total changed mid-pagination: "
                f"{reported_total} -> {page_total}"
            )

        found = re.findall(
            r'href="' + re.escape(BASE_URL) + r'product/show/(\d+)"', html
        )
        new_on_page = 0
        for product_id in found:
            if product_id in seen:
                continue
            seen.add(product_id)
            ids.append(product_id)
            new_on_page += 1

        if not found:
            return True
        if found and new_on_page == 0:
            raise ValueError(
                f"product: page {page} returned only duplicate IDs; pagination is stuck"
            )
        if reported_total and len(ids) >= reported_total:
            return True
        if limit and len(ids) >= limit:
            del ids[limit:]
            return True
        report(
            f"product: page {page:,} yielded {new_on_page} new IDs ({len(ids):,} collected)"
        )
        return False

    done = consume(1, get_html(session, search_url(1)))
    page = 2
    if not done and workers > 1 and reported_total and ids:
        per_page = len(ids)
        last_page = math.ceil(reported_total / per_page)
        if limit:
            last_page = min(last_page, math.ceil(limit / per_page))
        pages = list(range(2, last_page + 1))
        worker_session = thread_session_provider(user_agent)

        def fetch_page(page_number: int) -> str:
            html = get_html(worker_session(), search_url(page_number))
            time.sleep(delay)
            return html

        with closing(fetch_ordered(fetch_page, pages, workers)) as htmls:
            for page_number, html in zip(pages, htmls, strict=True):
                page = page_number + 1
                if consume(page_number, html):
                    done = True
                    break
    while not done:
        time.sleep(delay)
        done = consume(page, get_html(session, search_url(page)))
        page += 1

    if not limit and reported_total is not None and len(ids) != reported_total:
        raise ValueError(
            f"product: collected {len(ids)} unique IDs but search reports {reported_total}"
        )
    rows = [{"product_id": product_id} for product_id in ids]
    return rows, reported_total


# ---------------------------------------------------------------------------
# Station discovery: single centers page


def discover_stations(
    session: requests.Session, delay: float, report: ProgressReporter, limit: int = 0
) -> list[dict[str, Any]]:
    del delay, report  # single request; signature kept uniform
    response = session.get(CENTERS_URL, timeout=45)
    response.raise_for_status()
    response.encoding = "utf-8"
    ids = sorted(set(re.findall(r"station/public_profile/(\d+)", response.text)))
    if not ids:
        raise ValueError("station: centers page yielded no station links")
    if limit:
        ids = ids[:limit]
    return [{"station_id": station_id} for station_id in ids]


# ---------------------------------------------------------------------------
# Directory discovery: researchers and universities (offset pagination).


def _discover_directory(
    session: requests.Session,
    base_url: str,
    dataset_id: str,
    link_pattern: str,
    id_field: str,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Page an offset-based directory until a short page ends it.

    The `per_page` query parameter actually acts as a page offset
    (0, 12, 24, ...); a page rendering fewer than DIRECTORY_PAGE_SIZE cards
    is the last one. Duplicate IDs fail the run.
    """
    del report  # discovery is quick; detail-level progress is reported by callers
    ids: list[str] = []
    seen: set[str] = set()
    offset = 0
    while True:
        url = base_url if offset == 0 else f"{base_url}?per_page={offset}"
        response = session.get(url, timeout=45)
        response.raise_for_status()
        response.encoding = "utf-8"
        found = re.findall(link_pattern, response.text)
        if not found:
            break
        if len(found) - len(set(found)):
            raise ValueError(
                f"{dataset_id}: directory page at offset {offset} contains "
                f"duplicate cards"
            )
        for item_id in found:
            if item_id in seen:
                raise ValueError(
                    f"{dataset_id}: directory returned duplicate ID {item_id}"
                )
            seen.add(item_id)
            ids.append(item_id)
        if len(found) < DIRECTORY_PAGE_SIZE or (limit and len(ids) >= limit):
            break
        offset += DIRECTORY_PAGE_SIZE
        time.sleep(delay)

    if limit:
        ids = ids[:limit]
    if not ids:
        raise ValueError(f"{dataset_id}: directory pagination yielded no records")
    return [{id_field: item_id} for item_id in ids]


def discover_researchers(
    session: requests.Session, delay: float, report: ProgressReporter, limit: int = 0
) -> list[dict[str, Any]]:
    return _discover_directory(
        session,
        RESEARCHERS_URL,
        "researcher",
        r'product/agent/(\d+)" class="text-decoration-none',
        "agent_id",
        delay,
        report,
        limit=limit,
    )


def discover_universities(
    session: requests.Session, delay: float, report: ProgressReporter, limit: int = 0
) -> list[dict[str, Any]]:
    return _discover_directory(
        session,
        UNIVERSITIES_URL,
        "university",
        r'university/index/(\d+)" class="univ-card"',
        "university_id",
        delay,
        report,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Area-code resolution dictionaries (/api/form/*)


class AreaNameResolver:
    """Lazily loads the Thai administrative dictionaries from /api/form/*,
    fetching district/tambon pages on demand, then resolves area codes to
    names locally. Failures degrade gracefully to code-only areas."""

    def __init__(self, session: requests.Session, report: ProgressReporter):
        self._session = session
        self._report = report
        self._provinces: dict[str, str] | None = None
        self._districts: dict[str, str] = {}
        self._tambons: dict[str, str] = {}
        self.failed = False

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[Any, Any]:
        response = self._session.get(AREA_API_BASE + path, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            raise ValueError(f"area API {path} returned an unexpected envelope")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"area API {path} returned no data mapping")
        return data

    def province_name(self, province_id: str) -> str | None:
        if self._provinces is None and not self.failed:
            try:
                self._provinces = {
                    str(key): clean_text(value)
                    for key, value in self._get("/get_province").items()
                }
            except Exception as error:  # noqa: BLE001 - degrade to code-only areas
                self.failed = True
                self._report(
                    f"areas: dictionary fetch failed ({error}); storing codes only"
                )
        if self._provinces is None:
            return None
        return self._provinces.get(province_id)

    def district_name(self, district_id: str) -> str | None:
        if district_id not in self._districts and not self.failed:
            parent = district_id[:2]
            try:
                self._districts.update(
                    {
                        str(key): clean_text(value)
                        for key, value in self._get(
                            "/get_district", {"province": parent}
                        ).items()
                    }
                )
            except Exception as error:  # noqa: BLE001
                self.failed = True
                self._report(
                    f"areas: district fetch failed ({error}); storing codes only"
                )
        return self._districts.get(district_id)

    def tambon_name(self, tambon_id: str) -> str | None:
        if tambon_id not in self._tambons and not self.failed:
            parent = tambon_id[:4]
            try:
                self._tambons.update(
                    {
                        str(key): clean_text(value)
                        for key, value in self._get(
                            "/get_tumbon", {"district": parent}
                        ).items()
                    }
                )
            except Exception as error:  # noqa: BLE001
                self.failed = True
                self._report(
                    f"areas: tambon fetch failed ({error}); storing codes only"
                )
        return self._tambons.get(tambon_id)

    def resolve_areas(
        self, province_ids: list[str], district_ids: list[str], tambon_ids: list[str]
    ) -> list[dict[str, Any]]:
        areas: list[dict[str, Any]] = []
        for province_id in province_ids:
            related_districts = [
                district_id
                for district_id in district_ids
                if district_id.startswith(province_id)
            ]
            related_tambons = [
                tambon_id
                for tambon_id in tambon_ids
                if tambon_id.startswith(province_id)
            ]
            areas.append(
                {
                    "province_id": province_id,
                    "province": self.province_name(province_id),
                    "district_ids": related_districts,
                    "districts": [self.district_name(d) for d in related_districts],
                    "tambon_ids": related_tambons,
                    "tambons": [self.tambon_name(t) for t in related_tambons],
                }
            )
        return areas


# ---------------------------------------------------------------------------
# Product detail parsing


def _content_sections(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Collect the main-column description blocks by their known heading labels.

    The source renders most headings as h5.content-heading but some (e.g.
    เงื่อนไขการใช้งาน) use unrelated classes, so matching on the label set is
    more robust than matching on CSS classes — and footer/sidebar headings
    never collide with these labels.
    """
    known_labels = {
        "ความรู้ / เทคโนโลยี",
        "ความรู้ / เทคโนโลยีที่ศูนย์ให้บริการ",
        "ที่มา / จุดมุ่งหมาย / ความต้องการ",
        "จุดเด่น",
        "รายละเอียดเพิ่มเติม",
        "เงื่อนไขการใช้งาน",
        "ปัญหา (Pain Points)",
        "ประโยชน์ (Gain Points)",
    }
    sections: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    stop = {"h1", "h2", "h3", "h4", "h5", "h6"}
    for heading in soup.find_all("h5"):
        label = optional_text(heading.get_text(" ", strip=True))
        if not label or label not in known_labels or label in seen_labels:
            continue
        seen_labels.add(label)
        text_parts: list[str] = []
        items: list[str] = []
        for sibling in heading.find_next_siblings():
            if sibling.name in stop:
                break
            for li in sibling.find_all("li"):
                li_text = optional_text(li.get_text(" ", strip=True))
                if li_text:
                    items.append(li_text)
            sibling_text = optional_text(sibling.get_text(" ", strip=True))
            if sibling_text:
                text_parts.append(sibling_text)
        sections.append(
            {"label": label, "text": " ".join(text_parts) or None, "items": items}
        )
    return sections


def _sidebar_label_value(soup: BeautifulSoup, label: str) -> str | None:
    """Read '<span>label:</span> <span|strong>value</span>' sidebar pairs."""
    for element in soup.find_all(string=re.compile(re.escape(label + ":"))):
        container = element.find_parent("li")
        if container is None:
            container = element.find_parent("div")
        if container is None:
            continue
        full = clean_text(container.get_text(" ", strip=True)) or ""
        if not full.startswith(label + ":"):
            continue
        after = full.split(label + ":", 1)[1].strip()
        value = optional_text(after)
        if value:
            return value
    return None


def _sidebar_list_badge(soup: BeautifulSoup, label: str) -> str | None:
    """Read '<li>label:</li><span class=badge>value</span>' sidebar items.

    Scoped to <li> containers so table headers like the funding history's
    ประเภท column cannot produce false matches."""
    for li in soup.find_all("li"):
        full = clean_text(li.get_text(" ", strip=True)) or ""
        if not full.startswith(label + ":"):
            continue
        badge = li.find("span", class_="badge")
        if badge is not None:
            return optional_text(badge.get_text(" ", strip=True))
    return None


def _section_block(soup: BeautifulSoup, heading_text: str) -> dict[str, Any] | None:
    """Return {text, items} for the block following a heading whose text matches.

    Walks the heading's subsequent siblings until the next heading, mirroring
    the sibling-walk used by the sibling scrapers.
    """
    target = None
    for candidate in soup.find_all(["h5", "h6"]):
        label = clean_text(candidate.get_text(" ", strip=True)) or ""
        if label == heading_text:
            target = candidate
            break
    if target is None:
        return None
    text_parts: list[str] = []
    items: list[str] = []
    stop = {"h1", "h2", "h3", "h4", "h5", "h6"}
    for sibling in target.find_next_siblings():
        if sibling.name in stop:
            break
        for li in sibling.find_all("li"):
            li_text = optional_text(li.get_text(" ", strip=True))
            if li_text:
                items.append(li_text)
        sibling_text = optional_text(sibling.get_text(" ", strip=True))
        if sibling_text:
            text_parts.append(sibling_text)
    return {"text": " ".join(text_parts) or None, "items": items}


def _parse_product_trl(soup: BeautifulSoup) -> dict[str, Any] | None:
    heading = None
    for candidate in soup.find_all("h6"):
        text = candidate.get_text(" ", strip=True)
        if "ระดับความพร้อม" in text and "(TRL)" in text:
            heading = candidate
            break
    if heading is None:
        return None
    level = None
    label = None
    explanation = None
    stop = {"h1", "h2", "h3", "h4", "h5", "h6"}
    for sibling in heading.find_next_siblings():
        if sibling.name in stop:
            break
        raw_text = sibling.get_text(" ", strip=True)
        if level is None:
            level_match = re.search(r"ระดับ\s*([0-9])", raw_text)
            if level_match:
                level = to_int(level_match.group(1))
        if label is None:
            dark_span = sibling.select_one("span.text-dark")
            if dark_span is not None:
                label = optional_text(dark_span.get_text(" ", strip=True))
        if explanation is None:
            em = sibling.find("em")
            if em is not None:
                explanation = optional_text(em.get_text(" ", strip=True))
    if level is None:
        return None
    return {"level": level, "label_raw": label, "explanation_raw": explanation}


def _parse_product_funding(soup: BeautifulSoup) -> list[dict[str, Any]]:
    funding: list[dict[str, Any]] = []
    marker = soup.find(string=re.compile("ประวัติการได้รับทุนสนับสนุน"))
    if marker is None:
        return funding
    header_div = marker.find_parent("div")
    if header_div is None:
        return funding
    table_wrapper = header_div.find_next_sibling("div")
    table = table_wrapper.find("table") if table_wrapper is not None else None
    if table is None:
        return funding
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) != 3:
            continue
        funding.append(
            {
                "year_raw": optional_text(cells[0].get_text(" ", strip=True)),
                "source_raw": optional_text(cells[1].get_text(" ", strip=True)),
                "type_raw": optional_text(cells[2].get_text(" ", strip=True)),
            }
        )
    return funding


def _parse_product_target_groups(soup: BeautifulSoup) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for card in soup.select(".p-3.rounded.bg-light.border"):
        badge = card.select_one("span.badge")
        if badge is None:
            continue
        group = optional_text(badge.get_text(" ", strip=True))
        detail_element = card.find("div", class_="small")
        detail = (
            optional_text(detail_element.get_text(" ", strip=True))
            if detail_element
            else None
        )
        if group or detail:
            groups.append({"group_raw": group, "detail_raw": detail})
    return groups


def _parse_product_impact_list(soup: BeautifulSoup, heading: str) -> list[str]:
    section = _section_block(soup, heading)
    if section is None:
        return []
    return section["items"]


def _parse_product_roi_card(soup: BeautifulSoup, heading: str) -> dict[str, Any]:
    indicator = None
    quantity = None
    for strong in soup.find_all("strong"):
        if clean_text(strong.get_text(" ", strip=True)) != "ตัวชี้วัด:":
            continue
        card: Any = strong.parent
        while card is not None:
            headings = [
                clean_text(h.get_text(" ", strip=True)) for h in card.find_all("h5")
            ]
            if heading in headings:
                break
            card = card.parent
        if card is None:
            continue
        value_span = strong.find_next_sibling("span")
        if value_span is not None:
            indicator = optional_text(value_span.get_text(" ", strip=True))
        for other in card.find_all("strong"):
            if clean_text(other.get_text(" ", strip=True)) == "ปริมาณ:":
                quantity_span = other.find_next_sibling("span")
                if quantity_span is not None:
                    quantity = optional_text(quantity_span.get_text(" ", strip=True))
                break
        break
    return {"indicator_raw": indicator, "quantity_raw": quantity}


def _parse_product_ip(soup: BeautifulSoup) -> list[dict[str, Any]] | None:
    ip_heading = soup.find(string=re.compile("ทรัพย์สินทางปัญญา"))
    if ip_heading is None:
        return None
    panel = ip_heading.find_parent("h6")
    if panel is None:
        return None
    panel = panel.find_parent("div")
    if panel is None:
        return None
    entries: list[dict[str, Any]] = []
    for row in panel.select("div.small"):
        text = clean_text(row.get_text(" ", strip=True)) or ""
        if ":" not in text:
            continue
        label, _, value = text.partition(":")
        entries.append(
            {
                "label_raw": optional_text(label),
                "value_raw": optional_text(value),
            }
        )
    return entries or None


def parse_product_detail(
    html: str,
    listing_row: dict[str, Any],
    area_resolver: AreaNameResolver | None = None,
) -> dict[str, Any]:
    product_id = listing_row["product_id"]
    detail_url = PRODUCT_DETAIL_URL.format(product_id=product_id)
    soup = BeautifulSoup(html, "html.parser")
    warnings: list[str] = []

    title_element = soup.find("h1")
    title = (
        optional_text(title_element.get_text(" ", strip=True))
        if title_element
        else None
    )
    if not title:
        raise ValueError(f"Product {product_id} detail page has no h1 title")

    researcher_agent_id = None
    researcher_name = None
    researcher_anchor = soup.select_one('a[href*="/product/agent/"]')
    if researcher_anchor is not None:
        match = re.search(r"/product/agent/(\d+)", str(researcher_anchor.get("href")))
        researcher_agent_id = match.group(1) if match else None
        researcher_name = optional_text(researcher_anchor.get_text(" ", strip=True))

    university_id = None
    university_name = None
    university_badge = soup.select_one('a[href*="/university/index/"]')
    if university_badge is not None:
        match = re.search(r"/university/index/(\d+)", str(university_badge.get("href")))
        university_id = match.group(1) if match else None
        university_name = optional_text(university_badge.get_text(" ", strip=True))

    price_raw = _sidebar_label_value(soup, "ราคาโดยประมาณ")
    capacity_raw = _sidebar_label_value(soup, "กำลังการผลิต")
    technology_type = _sidebar_list_badge(soup, "ประเภท")

    category = None
    for element in soup.find_all(string=re.compile("หมวดหมู่:")):
        container = element.find_parent("li")
        if container is None:
            continue
        bold = container.find("span", class_="font-weight-bold")
        if bold is not None:
            category = optional_text(bold.get_text(" ", strip=True))
            break

    tags: list[str] = []
    tag_cloud = soup.select_one(".tag-cloud")
    if tag_cloud is not None:
        for anchor in tag_cloud.find_all("a"):
            tag = optional_text(anchor.get_text(" ", strip=True))
            if tag:
                tags.append(tag.lstrip("#").strip())

    provinces: list[str] = []
    districts: list[str] = []
    tambons: list[str] = []
    for input_id, target in (
        ("csv-provinces", "provinces"),
        ("csv-districts", "districts"),
        ("csv-tumbons", "tambons"),
    ):
        codes: list[str] = []
        element = soup.find("input", id=input_id)
        raw_values = str(element.get("value", "")) if element is not None else ""
        for code in raw_values.split(","):
            code = code.strip()
            if code and code not in codes:
                codes.append(code)
        if target == "provinces":
            provinces = codes
        elif target == "districts":
            districts = codes
        else:
            tambons = codes

    latlng = None
    latlng_element = soup.find(id="viewMapLatLngText")
    if latlng_element is not None:
        latlng = optional_text(latlng_element.get_text(" ", strip=True))

    areas: list[dict[str, Any]] | None = None
    if provinces:
        if area_resolver is not None:
            areas = area_resolver.resolve_areas(provinces, districts, tambons)
        else:
            areas = [
                {
                    "province_id": province_id,
                    "province": None,
                    "district_ids": [],
                    "districts": [],
                    "tambon_ids": [],
                    "tambons": [],
                }
                for province_id in provinces
            ]
        if any(area["province"] is None for area in areas):
            warnings.append("Product areas could not be resolved to names")

    data = {
        "detail_url": detail_url,
        "description_sections": _content_sections(soup),
        "technology_type": technology_type,
        "category": category,
        "trl": _parse_product_trl(soup),
        "ip": _parse_product_ip(soup),
        "price_raw": price_raw,
        "price_thb": parse_thb_amount(price_raw),
        "production_capacity_raw": capacity_raw,
        "target_groups": _parse_product_target_groups(soup),
        "funding_history": _parse_product_funding(soup),
        "roi": _parse_product_roi_card(soup, "ROI (Economic)"),
        "sroi": _parse_product_roi_card(soup, "SROI (Social)"),
        "outcomes": _parse_product_impact_list(soup, "ผลลัพธ์ (Outcomes)"),
        "impacts": _parse_product_impact_list(soup, "ผลกระทบ (Impacts)"),
        "researcher": {"agent_id": researcher_agent_id, "name_raw": researcher_name},
        "university": {"university_id": university_id, "name_raw": university_name},
        "tags": tags or None,
        "areas": areas,
        "location_latlng_raw": latlng,
    }

    if data["trl"] is None:
        warnings.append("Product page has no TRL block")
    if not data["description_sections"]:
        warnings.append("Product page has no content-heading description sections")
    if researcher_agent_id is None:
        warnings.append("Product page has no researcher profile link")

    return build_record("product", product_id, title, data, warnings)


# ---------------------------------------------------------------------------
# Station detail parsing


def parse_station_detail(html: str, listing_row: dict[str, Any]) -> dict[str, Any]:
    station_id = listing_row["station_id"]
    detail_url = STATION_DETAIL_URL.format(station_id=station_id)
    soup = BeautifulSoup(html, "html.parser")
    warnings: list[str] = []

    title_element = soup.find("h1")
    title = (
        optional_text(title_element.get_text(" ", strip=True))
        if title_element
        else None
    )
    if not title:
        raise ValueError(f"Station {station_id} detail page has no h1 title")

    info: dict[str, str | None] = {}
    for bold in soup.find_all("b"):
        label = clean_text(bold.get_text(" ", strip=True)) or ""
        if not label.endswith(":"):
            continue
        value_node = bold.next_sibling
        value_text = getattr(value_node, "text", "") or ""
        info[label.rstrip(":")] = optional_text(value_text)

    contact: dict[str, str | None] = {}
    for row in soup.select(".contact-item-row"):
        label_element = row.select_one(".contact-label")
        value_element = row.select_one(".contact-value")
        if label_element is None or value_element is None:
            continue
        label = optional_text(label_element.get_text(" ", strip=True))
        value = optional_text(value_element.get_text(" ", strip=True))
        if label:
            contact[label] = value

    goals = mission = None
    for block in soup.find_all("div", class_="mb-3"):
        first_bold = block.find("b")
        if first_bold is None:
            continue
        label = clean_text(first_bold.get_text(" ", strip=True)) or ""
        text = optional_text(block.get_text(" ", strip=True))
        if label == "เป้าหมาย" and text:
            goals = text
        elif label == "พันธกิจ" and text:
            mission = text

    product_ids: list[str] = []
    slider = soup.select_one("#tech-slider")
    if slider is not None:
        for link in slider.find_all("a", href=True):
            match = re.search(r"/product/show/(\d+)", str(link.get("href")))
            if match:
                product_ids.append(match.group(1))
    product_ids = sorted(set(product_ids))

    data = {
        "detail_url": detail_url,
        "organization_raw": info.get("หน่วยงาน"),
        "director_raw": info.get("ชื่อหัวหน้าศูนย์"),
        "start_year_raw": info.get("ปีที่เริ่มดำเนินการ (พ.ศ.)"),
        "location_note_raw": info.get("ที่ตั้ง / หมายเหตุ"),
        "goals_raw": goals,
        "mission_raw": mission,
        "contact": contact,
        "product_ids": product_ids,
    }
    if data["director_raw"] is None:
        warnings.append("Station page has no director field")
    if not product_ids:
        warnings.append("Station page lists no technologies")

    return build_record("station", station_id, title, data, warnings)


# ---------------------------------------------------------------------------
# Researcher / university directory parsing (cards only, no detail pages)


def parse_researchers_page(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []
    for anchor in soup.select("a[href*='/product/agent/']"):
        card = anchor.select_one(".res-card")
        if card is None:
            continue
        match = re.search(r"/product/agent/(\d+)", str(anchor.get("href")))
        if not match:
            continue
        agent_id = match.group(1)
        name_element = card.select_one(".res-name")
        name = (
            optional_text(name_element.get_text(" ", strip=True))
            if name_element is not None
            else None
        )
        if name is None:
            raise ValueError(f"Researcher card {agent_id} has no name")
        univ_element = card.select_one(".res-univ span")
        university = (
            optional_text(univ_element.get_text(" ", strip=True))
            if univ_element is not None
            else None
        )
        expertise_element = card.select_one(".res-expert-badge")
        expertise = (
            optional_text(expertise_element.get_text(" ", strip=True))
            if expertise_element is not None
            else None
        )
        innovation_count = None
        footer = card.select_one(".res-footer b")
        if footer is not None:
            innovation_count = to_int(clean_text(footer.get_text(strip=True)))
        records.append(
            build_record(
                "researcher",
                agent_id,
                name,
                {
                    "profile_url": BASE_URL + f"product/agent/{agent_id}",
                    "university_raw": university,
                    "expertise_raw": expertise,
                    "innovation_count": innovation_count,
                },
                [],
            )
        )
    return records


def collect_researchers(
    session: requests.Session,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
    workers: int = DEFAULT_WORKERS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    return _collect_directory(
        session,
        RESEARCHERS_URL,
        "researcher",
        parse_researchers_page,
        delay,
        report,
        limit=limit,
        workers=workers,
        user_agent=user_agent,
    )


def parse_universities_page(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []
    for anchor in soup.select("a.univ-card"):
        match = re.search(r"/university/index/(\d+)", str(anchor.get("href")))
        if not match:
            continue
        university_id = match.group(1)
        name_element = anchor.select_one(".univ-name")
        name = (
            optional_text(name_element.get_text(" ", strip=True))
            if name_element is not None
            else None
        )
        if name is None:
            raise ValueError(f"University card {university_id} has no name")
        stats: dict[str, int | None] = {}
        for item in anchor.select(".univ-stat-item"):
            value = item.select_one(".univ-stat-val")
            label = item.select_one(".univ-stat-label")
            if value is None or label is None:
                continue
            label_text = optional_text(label.get_text(" ", strip=True))
            if label_text:
                stats[label_text] = to_int(clean_text(value.get_text(strip=True)))
        records.append(
            build_record(
                "university",
                university_id,
                name,
                {
                    "profile_url": BASE_URL + f"university/index/{university_id}",
                    "stats": stats or None,
                },
                [],
            )
        )
    return records


def collect_universities(
    session: requests.Session,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
    workers: int = DEFAULT_WORKERS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    return _collect_directory(
        session,
        UNIVERSITIES_URL,
        "university",
        parse_universities_page,
        delay,
        report,
        limit=limit,
        workers=workers,
        user_agent=user_agent,
    )


def _collect_directory(
    session: requests.Session,
    base_url: str,
    dataset_id: str,
    parse_page: Callable[[str], list[dict[str, Any]]],
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
    workers: int = 1,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    """Single-pass directory collection: parse card records while paginating.

    The `per_page` query parameter actually acts as a page offset
    (0, 12, 24, ...); a page rendering fewer than DIRECTORY_PAGE_SIZE cards
    (or an empty one past the end) terminates pagination. Discovery and
    parsing share one request sequence because these endpoints are slow
    (~9 s/request on the researcher directory). Duplicate IDs fail the run.

    Offsets are fetched in waves of `workers` pages and parsed here in offset
    order; pages after the first short or empty one are ignored.
    """
    del session  # directory pages are fetched through per-thread worker sessions
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    worker_session = thread_session_provider(user_agent)

    def fetch_offset(offset: int) -> str:
        url = base_url if offset == 0 else f"{base_url}?per_page={offset}"
        html = get_html(worker_session(), url, timeout=60)
        time.sleep(delay)
        return html

    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        offset = 0
        done = False
        while not done:
            wave = [offset + index * DIRECTORY_PAGE_SIZE for index in range(workers)]
            futures = [executor.submit(fetch_offset, wave_offset) for wave_offset in wave]
            for wave_offset, future in zip(wave, futures, strict=True):
                page_records = parse_page(future.result())
                if not page_records:
                    done = True
                    break
                new_here = 0
                for record in page_records:
                    record_id = record["record_id"]
                    if record_id in seen_ids:
                        raise ValueError(
                            f"{dataset_id}: directory returned duplicate ID {record_id}"
                        )
                    seen_ids.add(record_id)
                    records.append(record)
                    new_here += 1
                report(
                    f"{dataset_id}: offset {wave_offset} rendered {new_here} records "
                    f"({len(records):,} collected)"
                )
                if len(page_records) < DIRECTORY_PAGE_SIZE or (
                    limit and len(records) >= limit
                ):
                    done = True
                    break
            offset = wave[-1] + DIRECTORY_PAGE_SIZE
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if limit:
        records = records[:limit]
    if not records:
        raise ValueError(f"{dataset_id}: directory pagination yielded no records")
    return records


# ---------------------------------------------------------------------------
# Dashboards: inline JSON extraction


@dataclass(frozen=True)
class DashboardSpec:
    dashboard_id: str
    output_name: str
    url: str
    json_var: str
    year_filters: tuple[str | None, ...] = (None,)
    headline_pattern: str | None = None
    sum_keys: tuple[str, ...] = ()


FAMILY_ECONOMIC_METRICS = {
    "มูลค่าต้นทุนที่ลดลงรวม": "cost_reduction",
    "มูลค่ารายได้ที่เพิ่มขึ้นรวม": "income_increase",
    "มูลค่ารายได้สุทธิเพิ่มขึ้น (Net)": "net_income_increase",
}


DASHBOARD_SPECS = [
    DashboardSpec(
        dashboard_id="innovation_map",
        output_name="innovation_map.json",
        url=BASE_URL + "dashboard",
        json_var="provinceData",
    ),
    DashboardSpec(
        dashboard_id="innovators",
        output_name="innovators.json",
        url=BASE_URL + "dashboard/innovatordashboard",
        json_var="provData",
        year_filters=(None, "2025", "2026"),
        headline_pattern=r"นวัตกรชุมชนทั้งหมด[^0-9]*([\d,]+)",
        sum_keys=("total_inno",),
    ),
    DashboardSpec(
        dashboard_id="family",
        output_name="family.json",
        url=BASE_URL + "dashboard/familydashboard",
        json_var="provData",
        year_filters=(None, "2025", "2026"),
        headline_pattern=r"ครัวเรือนเป้าหมายทั้งหมด[^0-9]*([\d,]+)",
        sum_keys=("total_hh",),
    ),
]


def extract_inline_json(html: str, var_name: str) -> dict[str, Any]:
    """Extract `const VAR = {...};` using JSONDecoder.raw_decode, which walks
    braces correctly instead of trusting a regex end boundary."""
    match = re.search(rf"const {var_name} =", html)
    if match is None:
        raise ValueError(f"inline variable {var_name} not found in page")
    decoder = json.JSONDecoder()
    start = html.index("{", match.end())
    value, _ = decoder.raw_decode(html[start:])
    if not isinstance(value, dict):
        raise ValueError(f"inline variable {var_name} is not a JSON object")
    return value


def extract_family_economic_metrics(html: str) -> dict[str, Any]:
    """Extract the three server-rendered economic cards from family dashboard HTML.

    These values are not part of ``provData`` and are not loaded through a
    public JSON endpoint. The backend renders them directly into ``.eco-card``
    elements, so parse the label/value pair from the response HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    metrics: dict[str, Any] = {}
    for card in soup.select(".eco-card"):
        label_node = card.select_one(".small")
        value_node = card.select_one(".h3")
        if label_node is None or value_node is None:
            continue
        label = clean_text(label_node.get_text(" ", strip=True))
        key = FAMILY_ECONOMIC_METRICS.get(label)
        if key is None:
            continue
        raw_value = clean_text(value_node.get_text(" ", strip=True))
        amount = parse_thb_amount(raw_value)
        if amount is None:
            raise ValueError(
                f"family dashboard economic metric {label!r} is not a THB amount: "
                f"{raw_value!r}"
            )
        if key in metrics:
            raise ValueError(f"family dashboard economic metric duplicated: {label}")
        metrics[key] = {"thb": amount, "raw": raw_value}

    missing = set(FAMILY_ECONOMIC_METRICS.values()) - metrics.keys()
    if missing:
        raise ValueError(
            "family dashboard economic metrics missing: " + ", ".join(sorted(missing))
        )

    component_sum = (
        metrics["cost_reduction"]["thb"] + metrics["income_increase"]["thb"]
    )
    metrics["net_formula"] = {
        "component_sum_thb": component_sum,
        "displayed_net_thb": metrics["net_income_increase"]["thb"],
        "difference_thb": metrics["net_income_increase"]["thb"] - component_sum,
        "matches_displayed_net": component_sum
        == metrics["net_income_increase"]["thb"],
    }
    return metrics


def collect_dashboard_artifact(
    session: requests.Session,
    spec: DashboardSpec,
    year_filter: str | None,
) -> dict[str, Any]:
    url = spec.url if year_filter is None else f"{spec.url}?year_filter={year_filter}"
    response = session.get(url, timeout=60)
    response.raise_for_status()
    response.encoding = "utf-8"

    prov_data = extract_inline_json(response.text, spec.json_var)
    integrity: dict[str, Any] = {"provinces": len(prov_data)}
    for key in spec.sum_keys:
        total = 0
        for entry in prov_data.values():
            if isinstance(entry, dict):
                raw = entry.get(key)
                if isinstance(raw, (int, float)):
                    total += raw
        integrity[f"sum_{key}"] = total
    if spec.dashboard_id == "innovation_map":
        item_counts = [len(items) for items in prov_data.values()]
        integrity["total_items"] = sum(item_counts)
        integrity["unique_product_ids"] = len(
            {item.get("prod_id") for items in prov_data.values() for item in items}
        )

    economic_metrics = None
    if spec.dashboard_id == "family":
        economic_metrics = extract_family_economic_metrics(response.text)
        integrity["economic_net_matches_components"] = economic_metrics[
            "net_formula"
        ]["matches_displayed_net"]

    headline = None
    if spec.headline_pattern:
        page_text = BeautifulSoup(response.text, "html.parser").get_text(
            " ", strip=True
        )
        headline_match = re.search(spec.headline_pattern, page_text)
        if headline_match:
            headline = to_int(headline_match.group(1))
            for key in spec.sum_keys:
                integrity[f"headline_matches_sum_{key}"] = headline == integrity.get(
                    f"sum_{key}"
                )

    warnings: list[str] = []
    if economic_metrics and not economic_metrics["net_formula"][
        "matches_displayed_net"
    ]:
        difference = economic_metrics["net_formula"]["difference_thb"]
        warnings.append(
            f"family[{year_filter or 'all'}]: displayed Net differs from the "
            f"component sum by {difference:,} THB"
        )
    for key, value in integrity.items():
        if key.startswith("headline_matches_") and not value:
            warnings.append(
                f"{spec.dashboard_id}[{year_filter}]: headline does not match {key}"
            )

    record_id = f"{spec.dashboard_id}:{year_filter or 'all'}"
    return build_record(
        "dashboard",
        record_id,
        f"{spec.dashboard_id} ({year_filter or 'all years'})",
        {
            "artifact": spec.output_name.replace(
                ".json", f"_{year_filter or 'all'}.json"
            ),
            "dashboard": spec.dashboard_id,
            "year_filter": year_filter,
            "source_url": url,
            "headline": headline,
            "integrity_checks": integrity,
            "prov_data": prov_data,
            **({"economic_metrics": economic_metrics} if economic_metrics else {}),
        },
        warnings,
    )


def collect_dashboards(
    session: requests.Session,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
    workers: int = DEFAULT_WORKERS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    del workers, user_agent  # seven page loads stay sequential
    records: list[dict[str, Any]] = []
    for spec in DASHBOARD_SPECS:
        for year_filter in spec.year_filters:
            report(
                f"dashboard: fetching {spec.dashboard_id} filter={year_filter or 'all'}"
            )
            records.append(collect_dashboard_artifact(session, spec, year_filter))
            time.sleep(delay)
            if limit and len(records) >= limit:
                return records
    return records


# ---------------------------------------------------------------------------
# Dataset definitions


@dataclass(frozen=True)
class DatasetDefinition:
    dataset_id: str
    output_name: str | None  # None => per-record export under per_record_export_dir
    minimum_records: int
    collect: Callable[..., list[dict[str, Any]]] | None
    per_record_export_dir: str | None = None


DATASETS: dict[str, DatasetDefinition] = {
    "products": DatasetDefinition(
        dataset_id="product",
        output_name="products.json",
        minimum_records=1000,
        collect=None,  # needs the shared AreaNameResolver; see run_scrape
    ),
    "stations": DatasetDefinition(
        dataset_id="station",
        output_name="stations.json",
        minimum_records=25,
        collect=None,  # defined after parse_station_detail wiring below
    ),
    "researchers": DatasetDefinition(
        dataset_id="researcher",
        output_name="researchers.json",
        minimum_records=400,
        collect=collect_researchers,
    ),
    "universities": DatasetDefinition(
        dataset_id="university",
        output_name="universities.json",
        minimum_records=50,
        collect=collect_universities,
    ),
    "dashboards": DatasetDefinition(
        dataset_id="dashboard",
        output_name=None,
        minimum_records=7,
        collect=collect_dashboards,
        per_record_export_dir="dashboards",
    ),
}


def collect_products(
    session: requests.Session,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
    area_resolver: AreaNameResolver | None = None,
    workers: int = DEFAULT_WORKERS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    rows, _ = discover_products(
        session, delay, report, limit=limit, workers=workers, user_agent=user_agent
    )
    records: list[dict[str, Any]] = []
    total = len(rows)
    progress_every = max(1, total // 40)
    worker_session = thread_session_provider(user_agent)

    def fetch_page(row: dict[str, Any]) -> str:
        html = get_html(
            worker_session(), PRODUCT_DETAIL_URL.format(product_id=row["product_id"])
        )
        time.sleep(delay)
        return html

    with closing(fetch_ordered(fetch_page, rows, workers)) as pages:
        for index, (row, html) in enumerate(zip(rows, pages, strict=True), start=1):
            product_id = row["product_id"]
            detail_url = PRODUCT_DETAIL_URL.format(product_id=product_id)
            last_error: Exception | None = None
            for attempt in range(2):
                if attempt:  # a parse failure re-fetches once, here, sequentially
                    time.sleep(delay)
                    html = get_html(session, detail_url)
                try:
                    records.append(parse_product_detail(html, row, area_resolver))
                    break
                except ValueError as error:
                    last_error = error
            else:
                raise ValueError(f"Failed to parse {detail_url}: {last_error}")
            report_detail_progress(
                report, "product", index, total, product_id, progress_every
            )
    return records


def collect_stations(
    session: requests.Session,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
    workers: int = DEFAULT_WORKERS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    rows = discover_stations(session, delay, report, limit=limit)
    records: list[dict[str, Any]] = []
    total = len(rows)
    worker_session = thread_session_provider(user_agent)

    def fetch_page(row: dict[str, Any]) -> str:
        html = get_html(
            worker_session(), STATION_DETAIL_URL.format(station_id=row["station_id"])
        )
        time.sleep(delay)
        return html

    with closing(fetch_ordered(fetch_page, rows, workers)) as pages:
        for index, (row, html) in enumerate(zip(rows, pages, strict=True), start=1):
            records.append(parse_station_detail(html, row))
            report_detail_progress(report, "station", index, total, row["station_id"], 5)
    return records


DATASETS["stations"] = DatasetDefinition(
    dataset_id="station",
    output_name="stations.json",
    minimum_records=25,
    collect=collect_stations,
)


def select_datasets(name: str) -> list[DatasetDefinition]:
    if name == "all":
        return list(DATASETS.values())
    return [DATASETS[name]]


ENVELOPE_KEYS = frozenset(
    {"schema_version", "dataset", "source_base_url", "scraped_at", "run_id", "warnings"}
)


def read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("top level is not an object")
    return payload


def load_previous_records(
    dataset: DatasetDefinition, output_dir: Path
) -> dict[str, dict[str, Any]]:
    """Return the previous output's records by ID; empty on a first run.

    An existing file that cannot be read fails the dataset so the removal guard
    is never skipped silently. Per-record datasets (dashboards) keep one file
    per record: the record is the file minus the envelope, plus its artifact name.
    """
    previous: dict[str, dict[str, Any]] = {}
    path = output_dir / (dataset.output_name or "")
    try:
        if dataset.per_record_export_dir:
            directory = output_dir / dataset.per_record_export_dir
            items = []
            for artifact_path in sorted(directory.glob("*.json")):
                payload = read_json_file(artifact_path)
                record = {
                    key: value
                    for key, value in payload.items()
                    if key not in ENVELOPE_KEYS
                }
                record["artifact"] = artifact_path.name
                items.append(record)
        elif path.exists():
            items = read_json_file(path)["data"]
            if not isinstance(items, list):
                raise TypeError("'data' is not a list")
        else:
            items = []
        for item in items:
            record_id = item["record_id"]
            if record_id in previous:
                raise ValueError(f"duplicate record ID {record_id}")
            previous[record_id] = item
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(
            f"{dataset.dataset_id} cannot read previous output: {error!r}"
        ) from error
    return previous


def compare_with_previous(
    dataset: DatasetDefinition,
    records: list[dict[str, Any]],
    output_dir: Path,
    allow_large_removal: bool = False,
) -> dict[str, int]:
    if len(records) < dataset.minimum_records:
        raise ValueError(
            f"{dataset.dataset_id} scrape produced {len(records)} records; "
            f"expected at least {dataset.minimum_records}"
        )
    by_id = {record["record_id"]: record for record in records}
    if len(by_id) != len(records):
        raise ValueError(f"{dataset.dataset_id} produced duplicate record IDs")

    existing = load_previous_records(dataset, output_dir)
    removed_ids = set(existing) - set(by_id)
    if (
        existing
        and len(existing) >= REMOVAL_SAFETY_MINIMUM
        and len(removed_ids) / len(existing) > REMOVAL_SAFETY_RATIO
        and not allow_large_removal
    ):
        raise ValueError(f"{dataset.dataset_id} removal exceeds the 25% safety limit")

    counts = {"new": 0, "updated": 0, "unchanged": 0, "removed": len(removed_ids)}
    for record_id, record in by_id.items():
        previous = existing.get(record_id)
        if previous is None:
            counts["new"] += 1
        elif semantic_hash(previous) != semantic_hash(record):
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
    return counts


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        except BaseException:
            temporary.close()
            temporary_path.unlink(missing_ok=True)
            raise
    temporary_path.replace(path)


def record_id_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    record_id = record["record_id"]
    numeric = to_int(record_id)
    return (numeric if numeric is not None else 0, record_id)


def write_dataset(
    output_dir: Path,
    dataset: DatasetDefinition,
    records: list[dict[str, Any]],
    run_id: str,
    scraped_at: str,
    warnings: list[str],
) -> None:
    """Write a dataset: records in ID order with keys sorted recursively."""
    exported = [
        json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True))
        for record in records
    ]
    if dataset.per_record_export_dir:
        for record in exported:
            artifact_name = record.get("artifact") or f"{record['record_id']}.json"
            write_json(
                output_dir / dataset.per_record_export_dir / artifact_name,
                {
                    "schema_version": DEFAULT_SCHEMA_VERSION,
                    "dataset": dataset.dataset_id,
                    "source_base_url": BASE_URL,
                    "scraped_at": scraped_at,
                    "run_id": run_id,
                    "warnings": sorted(set(warnings)),
                    **{
                        key: value for key, value in record.items() if key != "artifact"
                    },
                },
            )
        return
    assert dataset.output_name is not None
    exported.sort(key=record_id_sort_key)
    write_json(
        output_dir / dataset.output_name,
        {
            "schema_version": DEFAULT_SCHEMA_VERSION,
            "dataset": dataset.dataset_id,
            "source_base_url": BASE_URL,
            "scraped_at": scraped_at,
            "run_id": run_id,
            "record_count": len(exported),
            "warnings": sorted(set(warnings)),
            "data": exported,
        },
    )


# ---------------------------------------------------------------------------
# Run orchestration


def run_scrape(args: argparse.Namespace) -> int:
    selected = select_datasets(args.dataset)
    report = create_progress_reporter(args.quiet)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session = create_session(args.user_agent)
    summary: dict[str, Any] = {"run_id": run_id, "datasets": {}, "warnings": []}

    area_resolver = AreaNameResolver(session, report)

    has_failures = False
    for dataset in selected:
        report(f"{dataset.dataset_id}: starting")
        try:
            if dataset.dataset_id == "product":
                records = collect_products(
                    session,
                    args.delay,
                    report,
                    args.limit,
                    area_resolver,
                    args.workers,
                    args.user_agent,
                )
            else:
                assert dataset.collect is not None
                records = dataset.collect(
                    session,
                    args.delay,
                    report,
                    args.limit,
                    args.workers,
                    args.user_agent,
                )
            warn_count_drift(dataset.dataset_id, len(records), summary["warnings"])
            if args.limit:
                summary["datasets"][dataset.dataset_id] = {
                    "status": "smoke_test",
                    "discovered": len(records),
                    "output_written": False,
                }
                report(
                    f"{dataset.dataset_id}: smoke test completed with {len(records):,} records"
                )
                continue
            counts = compare_with_previous(
                dataset,
                records,
                OUTPUT_DIR,
                allow_large_removal=args.allow_large_removal,
            )
            record_warnings = [
                warning
                for record in records
                for warning in record.get("validation_warnings", [])
            ]
            write_dataset(
                OUTPUT_DIR,
                dataset,
                records,
                run_id,
                utc_now(),
                summary["warnings"] + record_warnings,
            )
            summary["datasets"][dataset.dataset_id] = {
                "status": "completed",
                "discovered": len(records),
                "output_written": True,
                **counts,
            }
            report(
                f"{dataset.dataset_id}: validated and saved {len(records):,} records "
                f"(new={counts['new']}, updated={counts['updated']}, unchanged={counts['unchanged']})"
            )
        except Exception as error:  # noqa: BLE001 - each dataset reports its own failure.
            has_failures = True
            summary["datasets"][dataset.dataset_id] = {
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "output_written": False,
            }
            report(f"{dataset.dataset_id}: failed — {type(error).__name__}: {error}")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if has_failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape public pmua-apptech.com content."
    )
    parser.add_argument("--dataset", choices=("all", *DATASETS), default="all")
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Seconds each worker pauses after every request.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Concurrent page fetches, 1 to {MAX_WORKERS}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Smoke-test record limit; does not write JSON outputs.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress before the first, final, and every Nth detail-page fetch.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages written to stderr.",
    )
    parser.add_argument(
        "--allow-large-removal",
        action="store_true",
        help="Allow an update that would remove more than 25%% of an established dataset.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.delay < 0 or args.limit < 0 or args.progress_every < 1:
        raise SystemExit(
            "--delay and --limit must be non-negative; --progress-every must be at least 1"
        )
    if not 1 <= args.workers <= MAX_WORKERS:
        raise SystemExit(f"--workers must be between 1 and {MAX_WORKERS}")
    return run_scrape(args)


if __name__ == "__main__":
    sys.exit(main())
