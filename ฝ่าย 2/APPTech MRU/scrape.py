"""Incremental scraper for the public APPTech MRU platform (38rat.nstru.ac.th).

The platform publishes two catalogs through its own frontend API
(POST /backend/ajax/public/{innovation,requirement}.php, 12 rows per call),
while all rich attributes are server-rendered on the detail pages. The
scraper is therefore a hybrid: the API provides discovery and completeness
counts, detail-page HTML provides the full record. Each run is compared with
the previous JSON file in output/, which is the only state kept.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
from urllib.parse import urljoin, urlparse

import requests
from browser_access import (
    BrowserVerification,
    VerifiedSession,
    reject_browser_challenge,
)
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

BASE_URL = "https://38rat.nstru.ac.th/"
API_BASE_URL = "https://38rat.nstru.ac.th/backend/ajax/public/"
INNOVATION_API_URL = API_BASE_URL + "innovation.php"
REQUIREMENT_API_URL = API_BASE_URL + "requirement.php"
INNOVATION_DETAIL_URL = (
    BASE_URL + "page/innovationdetail.php?innovationid={innovationid}"
)
REQUIREMENT_DETAIL_URL = (
    BASE_URL + "page/requirementdetail.php?requirementid={requirementid}"
)

API_PAGE_SIZE = 12  # Server hard-caps the window; a larger endlimit is ignored.
DEFAULT_DELAY_SECONDS = 0.0  # Pause after each request, per worker.
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_USER_AGENT = "ApptechMruCollector"
PLACEHOLDER_TEXT = {"-", "ไม่มี", "-ไม่มี-", "N/A"}
REMOVAL_SAFETY_RATIO = 0.25
REMOVAL_SAFETY_MINIMUM = (
    10  # Small catalogs fluctuate legitimately; guard only established ones.
)
DEFAULT_SCHEMA_VERSION = 1
ProgressReporter = Callable[[str], None]


@dataclass(frozen=True)
class DatasetDefinition:
    dataset_id: str
    output_name: str
    api_url: str
    api_action: str
    id_field: str
    detail_url_template: str
    detail_title_selector: str
    minimum_records: int
    parse_detail: Callable[[str, dict[str, Any]], dict[str, Any]]


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


def create_session(
    user_agent: str, verification: BrowserVerification | None = None
) -> requests.Session:
    """Create one independent session, optionally sharing browser verification."""
    retry_policy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    session = VerifiedSession(verification) if verification else requests.Session()
    session.headers.update(
        {"User-Agent": user_agent, "Origin": BASE_URL.rstrip("/"), "Referer": BASE_URL}
    )
    adapter = HTTPAdapter(max_retries=retry_policy)
    session.mount("https://", adapter)
    return session


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


def absolute_url(value: str | None, page_url: str) -> str | None:
    if not value:
        return None
    url = urljoin(page_url, value.strip())
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


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


def clean_list_text(value: str) -> str:
    """Normalize text while preserving the wide runs the source uses to
    separate inline list entries (inventor names, expertise fields)."""
    normalized = (
        unicodedata.normalize("NFC", value).replace("\ufeff", "").replace("\u00a0", " ")
    )
    return normalized.strip()


def split_multi_space(value: str | None) -> list[str]:
    """Split a rendered inline list; the source separates entries with wide spacing."""
    if value is None:
        return []
    return [
        part
        for part in (clean_text(piece) for piece in re.split(r"\s{2,}", value))
        if part
    ]


def to_int(value: str) -> int | None:
    """Convert digit text (optionally comma-grouped) to int; None when unparseable."""
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


def parse_thb_amount(value: str | None) -> int | None:
    """Parse an unambiguous Thai-baht amount such as '550,000 บาท'."""
    if value is None:
        return None
    match = re.fullmatch(r"([\d,]+)\s*บาท", value)
    if not match:
        return None
    return to_int(match.group(1))


def parse_trl_srl_level(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?:TR|SR)L([1-9])", value)
    return to_int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Catalog API client (discovery + completeness)


def fetch_catalog(
    session: requests.Session,
    dataset: DatasetDefinition,
    delay: float,
    report: ProgressReporter,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Page through the public catalog API and return all listing rows.

    Guards: JSON shape, status flag, stable totaldata, no duplicate IDs, and
    unique IDs collected must equal totaldata once pagination is exhausted.
    """
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    totaldata: int | None = None
    offset = 0
    while True:
        body = {
            "action": dataset.api_action,
            "filter": {
                "innovationgroup": "All",
                "affiliationid": "All",
                "innovationtype": "All",
                "orderby": 1,
                "cus_search1": "",
                "startlimit": offset,
                "endlimit": offset + API_PAGE_SIZE,
                "maxpage": 0,
                "targetpagenumber": 1,
            },
        }
        response = session.post(dataset.api_url, json=body, timeout=45)
        response.raise_for_status()
        response_text = getattr(response, "text", "")
        if isinstance(response_text, str):
            reject_browser_challenge(response_text)
        try:
            envelope = response.json()
        except ValueError as error:
            raise ValueError(
                f"{dataset.dataset_id} API returned non-JSON at offset {offset}"
            ) from error
        status_value = envelope.get("status") if isinstance(envelope, dict) else None
        if not isinstance(status_value, bool) or not status_value:
            raise ValueError(
                f"{dataset.dataset_id} API returned status failure at offset {offset}"
            )
        inner = envelope.get("data")
        page_rows = inner.get("data") if isinstance(inner, dict) else None
        if not isinstance(page_rows, list) or any(
            not isinstance(row, dict) for row in page_rows
        ):
            raise ValueError(
                f"{dataset.dataset_id} API returned an unexpected envelope at offset {offset}"
            )

        # bool is a subclass of int, so exclude it explicitly: totaldata must
        # be a real integer count, not a JSON true/false.
        raw_total = inner.get("totaldata") if isinstance(inner, dict) else None
        page_total = (
            raw_total
            if isinstance(raw_total, int) and not isinstance(raw_total, bool)
            else None
        )
        if page_total is None:
            raise ValueError(
                f"{dataset.dataset_id} API returned a non-integer totaldata"
            )
        if totaldata is None:
            totaldata = page_total
            report(f"{dataset.dataset_id}: catalog reports totaldata={totaldata}")
        elif page_total != totaldata:
            raise ValueError(
                f"{dataset.dataset_id} totaldata changed mid-pagination: {totaldata} -> {page_total}"
            )

        if not page_rows:
            break
        for row in page_rows:
            record_id = optional_text(row.get(dataset.id_field))
            if not record_id:
                raise ValueError(
                    f"{dataset.dataset_id} API row is missing {dataset.id_field}: {row}"
                )
            if record_id in seen_ids:
                raise ValueError(
                    f"{dataset.dataset_id} API returned duplicate {dataset.id_field} {record_id}"
                )
            seen_ids.add(record_id)
            rows.append(row)

        offset += API_PAGE_SIZE
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break
        if len(rows) >= totaldata:
            break
        time.sleep(delay)

    if not limit and len(rows) != totaldata:
        raise ValueError(
            f"{dataset.dataset_id} collected {len(rows)} unique IDs but API reports totaldata={totaldata}"
        )
    return rows


# ---------------------------------------------------------------------------
# Detail-page parsing


def detail_page_sections(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    """Collect h5-labelled sections from the record page.

    Each section keeps the flattened text and, when the source uses a list,
    the individual list items. Modal and footer headings are skipped; sidebar
    badge headings (category/type/level/video) carry no section text and are
    read separately by the badge helpers.
    """
    sections: dict[str, dict[str, Any]] = {}
    if soup.select_one("h1.card-title") is None:
        return sections
    for candidate in soup.find_all("h5"):
        if candidate.find_parent("footer") or candidate.find_parent(
            attrs={"class": "modal-content"}
        ):
            continue
        label = optional_text(candidate.get_text(" ", strip=True))
        if not label:
            continue
        text_parts: list[str] = []
        raw_parts: list[str] = []
        items: list[str] = []
        for sibling in candidate.find_next_siblings():
            if sibling.name in {"h1", "h2", "h3", "h4", "h5"}:
                break
            for li in sibling.find_all("li"):
                li_text = optional_text(li.get_text(" ", strip=True))
                if li_text:
                    items.append(li_text)
            sibling_text = optional_text(sibling.get_text(" ", strip=True))
            if sibling_text:
                text_parts.append(sibling_text)
            raw_parts.append(clean_list_text(sibling.get_text()))
        if label not in sections:
            raw = " ".join(part for part in raw_parts if part)
            sections[label] = {
                "text": " ".join(text_parts) or None,
                "raw": raw or None,
                "items": items,
            }
    return sections


def section_text(sections: dict[str, dict[str, Any]], label: str) -> str | None:
    section = sections.get(label)
    if not section:
        return None
    return optional_text(section.get("text"))


def section_items(sections: dict[str, dict[str, Any]], label: str) -> list[str]:
    section = sections.get(label)
    if not section:
        return []
    return [
        item
        for item in (optional_text(value) for value in section.get("items", []))
        if item
    ]


def section_list_text(sections: dict[str, dict[str, Any]], label: str) -> list[str]:
    """Split a section whose source renders an inline list with wide spacing."""
    section = sections.get(label)
    if not section:
        return []
    return split_multi_space(section.get("raw"))


def sidebar_badge_values(soup: BeautifulSoup) -> dict[str, str]:
    """Read the right-sidebar h5 badges (category/type/SRL/video) as raw label text."""
    values: dict[str, str] = {}
    for heading in soup.find_all("h5"):
        label = clean_text(heading.get_text(" ", strip=True))
        if not label or heading.find_parent(attrs={"class": "modal-content"}):
            continue
        for prefix in ("ประเภทนวัตกรรม", "หมวดหมู่:", "ระดับผลงาน:", "Tag:", "ประเภท:"):
            if label.startswith(prefix):
                value = optional_text(label[len(prefix) :])
                if value:
                    values.setdefault(prefix, value)
    return values


def sidebar_innovation_type(soup: BeautifulSoup) -> str | None:
    for heading in soup.find_all("h5"):
        label = clean_text(heading.get_text(" ", strip=True))
        if label.startswith("ประเภทนวัตกรรม"):
            badge = heading.find("span", class_="badge")
            if badge is not None:
                return optional_text(badge.get_text(" ", strip=True))
    return None


def trl_from_image(soup: BeautifulSoup, page_url: str) -> dict[str, Any] | None:
    """TRL is published as a level image (TRL-9Lv/TRL-N.png); the commented
    'ระดับผลงาน: TRL7' heading is stale template text, identical on every page."""
    for img in soup.find_all("img"):
        url = absolute_url(str(img.get("src", "")), page_url)
        if not url or "TRL-9Lv" not in url:
            continue
        match = re.search(r"TRL-(\d)\.(?:png|jpg|jpeg|gif|webp)", url, re.IGNORECASE)
        if match:
            return {"level": to_int(match.group(1)), "image_url": url}
    return None


def srl_from_heading(soup: BeautifulSoup) -> dict[str, Any] | None:
    for heading in soup.find_all("h5"):
        label = clean_text(heading.get_text(" ", strip=True))
        if label.startswith("ระดับผลงาน: SRL"):
            value = optional_text(label.split(":", 1)[1])
            if value:
                return {"level": parse_trl_srl_level(value), "label_raw": value}
    return None


def video_link(soup: BeautifulSoup, page_url: str) -> str | None:
    for heading in soup.find_all("h5"):
        label = clean_text(heading.get_text(" ", strip=True))
        if label.startswith("ลิงค์วิดีโอ"):
            anchor = heading.find("a", href=True)
            if anchor is not None:
                href = optional_text(anchor.get("href"))
                return absolute_url(href, page_url) if href else None
            span = heading.find("span")
            if span is not None:
                text = optional_text(span.get_text(" ", strip=True))
                return absolute_url(text, page_url) if text else None
    return None


def view_count(soup: BeautifulSoup) -> int | None:
    for span in soup.select("span"):
        match = re.fullmatch(
            r"(\d[\d,]*)\s*Views", clean_text(span.get_text(" ", strip=True)) or ""
        )
        if match:
            return to_int(match.group(1))
    return None


def researcher_entries(
    sections: dict[str, dict[str, Any]], label: str, role: str
) -> list[dict[str, Any]]:
    """Parse lead/co-researcher list items shaped as 'name คณะ X สถาบัน Y'."""
    section = sections.get(label)
    if not section:
        return []
    entries: list[dict[str, Any]] = []
    for item in section.get("items", []):
        parts = re.split(r"คณะ|สถาบัน", item)
        name = optional_text(parts[0]) if parts else None
        faculty = optional_text(parts[1]) if len(parts) > 1 else None
        university = optional_text(parts[2]) if len(parts) > 2 else None
        if name or faculty or university:
            entries.append(
                {
                    "role": role,
                    "name": name,
                    "faculty": faculty,
                    "university": university,
                }
            )
    return entries


def funding_entries(
    sections: dict[str, dict[str, Any]], label: str
) -> list[dict[str, Any]]:
    """Parse funding list items 'แหล่งเงินทุนที่ใช้วิจัย/นวัตกรรม X รายละเอียดเพิ่มเติม Y เป็นจำนวนเงิน Z'."""
    section = sections.get(label)
    if not section:
        return []
    entries: list[dict[str, Any]] = []
    for item in section.get("items", []):
        match = re.search(
            r"แหล่งเงินทุนที่ใช้วิจัย/นวัตกรรม\s*(?P<source>.*?)\s*รายละเอียดเพิ่มเติม\s*(?P<detail>.*?)"
            r"(?:\s*เป็นจำนวนเงิน\s*(?P<amount>.*))?$",
            item,
        )
        if match is None:
            raw = optional_text(item)
            if raw:
                entries.append(
                    {
                        "source": None,
                        "details_raw": raw,
                        "amount_raw": None,
                        "amount_thb": None,
                    }
                )
            continue
        amount_raw = optional_text(match.group("amount"))
        entries.append(
            {
                "source": optional_text(match.group("source")),
                "details_raw": optional_text(match.group("detail")),
                "amount_raw": amount_raw,
                "amount_thb": parse_thb_amount(amount_raw),
            }
        )
    return entries


def ip_entry(sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The IP type line reads 'ทรัพย์สินอุตสาหกรรม : อนุสิทธิบัตร'."""
    raw = section_text(sections, "ทรัพย์สินทางปัญญา")
    ip_type = None
    if raw and ":" in raw:
        ip_type = optional_text(raw.split(":", 1)[1])
    return {
        "property_name": section_text(sections, "ชื่อทรัพย์สินทางปัญญา"),
        "type": ip_type,
        "type_raw": raw,
        "request_no_raw": section_text(sections, "เลขที่คำขอ"),
        "patent_no_raw": section_text(sections, "เลขที่สิทธิบัตร"),
    }


def build_record(
    dataset_id: str,
    record_id: str,
    title: str,
    detail_url: str,
    data: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    if not record_id or not title or not detail_url:
        raise ValueError("Record is missing a stable ID, title, or detail URL")
    return {
        "record_type": dataset_id,
        "record_id": record_id,
        "title": title,
        "detail_url": detail_url,
        "data": clean_value(data),
        "validation_warnings": sorted(set(warnings)),
    }


INNOVATION_DESCRIPTION_LABELS = ("รายละเอียด", "ความรู้/เทคโนโลยี")
REQUIREMENT_DESCRIPTION_LABELS = ("รายละเอียด",)


def missing_description_warning(
    sections: dict[str, dict[str, Any]],
    labels: tuple[str, ...],
    page_kind: str,
) -> str:
    """Classify why a record has no description text.

    A source data gap (section headings exist but their content is empty or
    a placeholder like '-') is normal and only needs visibility. Absent
    headings instead suggest the page layout changed and the parser no
    longer matches it, which is worth investigating before trusting the run.
    """
    if any(label in sections for label in labels):
        return (
            f"{page_kind} page published no description text "
            "(section heading present; content empty or placeholder)"
        )
    return (
        f"{page_kind} page has no description section heading "
        "(possible page-layout change; none of the expected labels matched)"
    )


def parse_innovation_detail(html: str, listing_row: dict[str, Any]) -> dict[str, Any]:
    detail_url = INNOVATION_DETAIL_URL.format(innovationid=listing_row["innovationid"])
    soup = BeautifulSoup(html, "html.parser")
    warnings: list[str] = []
    title_element = soup.select_one("h1.card-title")
    title = (
        optional_text(title_element.get_text(" ", strip=True))
        if title_element
        else None
    )
    if not title:
        raise ValueError("Innovation detail page has no title heading")

    sections = detail_page_sections(soup)
    badges = sidebar_badge_values(soup)

    description_sections = [
        {"label": label, "text": section_text(sections, label)}
        for label in ("รายละเอียด", "ความรู้/เทคโนโลยี")
        if section_text(sections, label) is not None
    ]

    inventors_raw = section_text(sections, "ชื่อผู้ที่ประดิษฐ์")
    trl = trl_from_image(soup, detail_url)
    srl = srl_from_heading(soup)
    data = {
        "innovationid": listing_row["innovationid"],
        "listing": {
            "owner_user_id": optional_text(listing_row.get("userid")),
            "owner_name": optional_text(listing_row.get("userfullname")),
            "affiliation": optional_text(listing_row.get("affiliationname")),
        },
        "innovation_type": sidebar_innovation_type(soup),
        "category": badges.get("หมวดหมู่:"),
        "trl": trl,
        "srl": srl,
        "description_sections": description_sections,
        "rights_holder": section_text(sections, "เจ้าของสิทธิ์"),
        "ip": ip_entry(sections),
        "inventors_raw": inventors_raw,
        "inventors": section_list_text(sections, "ชื่อผู้ที่ประดิษฐ์"),
        "researchers": researcher_entries(sections, "ผู้วิจัยหลัก", "lead")
        + researcher_entries(sections, "ผู้ร่วมวิจัย", "co"),
        "target_groups": section_items(sections, "กลุ่มเป้าหมาย"),
        "highlights": section_items(sections, "จุดเด่น"),
        "funding": funding_entries(sections, "แหล่งเงินทุนที่ใช้วิจัย"),
        "roi": {
            "indicator_raw": section_text(sections, "ตัวชี้วัดผลลัพธ์ ROI"),
            "value_raw": section_text(sections, "ปริมาณหน่วยนับ ROI"),
        },
        "sroi": {
            "indicator_raw": section_text(sections, "ตัวชี้วัดผลลัพธ์ SROI"),
            "value_raw": section_text(sections, "ปริมาณหน่วยนับ SROI"),
        },
        "innovation_value_raw": section_text(sections, "มูลค่านวัตกรรม"),
        "video_url": video_link(soup, detail_url),
        "view_count": view_count(soup),
    }

    if not description_sections:
        warnings.append(
            missing_description_warning(
                sections, INNOVATION_DESCRIPTION_LABELS, "Innovation"
            )
        )
    if data["category"] is None:
        warnings.append("Innovation page has no category badge")
    if trl is None:
        warnings.append("Innovation page has no TRL image")

    return build_record(
        "innovation", listing_row["innovationid"], title, detail_url, data, warnings
    )


def parse_requirement_detail(html: str, listing_row: dict[str, Any]) -> dict[str, Any]:
    detail_url = REQUIREMENT_DETAIL_URL.format(
        requirementid=listing_row["requirementid"]
    )
    soup = BeautifulSoup(html, "html.parser")
    warnings: list[str] = []
    title_element = soup.select_one("h1.card-title")
    title = (
        optional_text(title_element.get_text(" ", strip=True))
        if title_element
        else None
    )
    if not title:
        raise ValueError("Requirement detail page has no title heading")

    sections = detail_page_sections(soup)
    badges = sidebar_badge_values(soup)
    tags_raw = badges.get("Tag:")

    data = {
        "requirementid": listing_row["requirementid"],
        "listing": {
            "owner_user_id": optional_text(listing_row.get("userid")),
            "owner_name": optional_text(listing_row.get("userfullname")),
            "affiliation": optional_text(listing_row.get("affiliationname")),
        },
        "short_description": section_text(sections, "คำอธิบายสั้น"),
        "description": section_text(sections, "รายละเอียด"),
        "need_statement": section_text(sections, "ความต้องการ"),
        "hired_external_expert_raw": section_text(
            sections, "เคยจัดจ้างผู้เชี่ยวชาญภายนอกเพื่อให้คำปรึกษา"
        ),
        "required_expertise_raw": section_text(sections, "สาขาความเชี่ยวชาญที่ต้องการ"),
        "required_expertise": section_list_text(sections, "สาขาความเชี่ยวชาญที่ต้องการ"),
        "impact": {
            "economic_raw": section_text(sections, "ความเสียหายทางเศรษฐกิจ"),
            "environmental_raw": section_text(sections, "ความเสียหายทางสิ่งแวดล้อม"),
            "social_raw": section_text(sections, "ความเสียหายทางสังคม"),
        },
        "funding": funding_entries(sections, "แหล่งเงินทุนที่ใช้วิจัย"),
        "type_raw": badges.get("ประเภท:"),
        "category": badges.get("หมวดหมู่:"),
        "tags": [
            tag for tag in (part.strip() for part in (tags_raw or "").split(",")) if tag
        ],
        "tags_raw": tags_raw,
        "video_url": video_link(soup, detail_url),
        "view_count": view_count(soup),
    }

    if data["description"] is None:
        warnings.append(
            missing_description_warning(
                sections, REQUIREMENT_DESCRIPTION_LABELS, "Requirement"
            )
        )
    if data["category"] is None:
        warnings.append("Requirement page has no category badge")

    return build_record(
        "requirement", listing_row["requirementid"], title, detail_url, data, warnings
    )


def thread_session_provider(
    user_agent: str, verification: BrowserVerification | None = None
) -> Callable[[], requests.Session]:
    """Give each thread its own session while sharing verification, not connections."""
    local = threading.local()

    def get_session() -> requests.Session:
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = create_session(user_agent, verification)
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


def fetch_detail_html(
    session: requests.Session, dataset: DatasetDefinition, listing_row: dict[str, Any]
) -> str:
    """HTTP only: fetch one detail page. Safe to run in a worker thread."""
    detail_url = dataset.detail_url_template.format(
        **{dataset.id_field: listing_row[dataset.id_field]}
    )
    response = session.get(detail_url, timeout=45)
    response.raise_for_status()
    response.encoding = "utf-8"
    reject_browser_challenge(response.text)
    return response.text


def parse_detail_page(
    session: requests.Session,
    dataset: DatasetDefinition,
    listing_row: dict[str, Any],
    html: str,
    delay: float,
) -> dict[str, Any]:
    """Parse a fetched detail page; on a parse failure re-fetch once, then fail."""
    last_error: Exception | None = None
    for attempt in range(2):
        if attempt:
            time.sleep(delay)
            html = fetch_detail_html(session, dataset, listing_row)
        try:
            return dataset.parse_detail(html, listing_row)
        except ValueError as error:
            last_error = error
    detail_url = dataset.detail_url_template.format(
        **{dataset.id_field: listing_row[dataset.id_field]}
    )
    raise ValueError(f"Failed to parse {detail_url}: {last_error}")


def collect_dataset(
    session: requests.Session,
    dataset: DatasetDefinition,
    delay: float,
    limit: int,
    report: ProgressReporter,
    progress_every: int,
    workers: int = DEFAULT_WORKERS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    rows = fetch_catalog(session, dataset, delay, report, limit=limit)
    verification = session.verification if isinstance(session, VerifiedSession) else None
    worker_session = thread_session_provider(user_agent, verification)

    def fetch_page(row: dict[str, Any]) -> str:
        html = fetch_detail_html(worker_session(), dataset, row)
        time.sleep(delay)
        return html

    records: list[dict[str, Any]] = []
    with closing(fetch_ordered(fetch_page, rows, workers)) as pages:
        for index, (row, html) in enumerate(zip(rows, pages, strict=True), start=1):
            report_detail_progress(
                report,
                dataset.dataset_id,
                index,
                len(rows),
                row[dataset.id_field],
                progress_every,
            )
            records.append(parse_detail_page(session, dataset, row, html, delay))
    return records


# ---------------------------------------------------------------------------
# Comparison with the previous output file


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def record_id_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    record_id = record["record_id"]
    numeric = to_int(record_id) if record_id else None
    return (numeric if numeric is not None else 0, record_id)


def load_previous_records(
    dataset: DatasetDefinition, previous_path: Path
) -> dict[str, dict[str, Any]]:
    """Return the previous output's records by ID; empty on a first run.

    An existing file that cannot be read fails the dataset so the removal guard
    is never skipped silently.
    """
    if not previous_path.exists():
        return {}
    previous: dict[str, dict[str, Any]] = {}
    try:
        payload = json.loads(previous_path.read_text(encoding="utf-8"))
        data = payload["data"]
        if not isinstance(data, list):
            raise TypeError("'data' is not a list")
        for item in data:
            record_id = item["record_id"]
            if record_id in previous:
                raise ValueError(f"duplicate record ID {record_id}")
            previous[record_id] = item
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(
            f"{dataset.dataset_id} cannot read previous output {previous_path.name}: {error!r}"
        ) from error
    return previous


def compare_with_previous(
    dataset: DatasetDefinition,
    records: list[dict[str, Any]],
    previous_path: Path,
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

    existing = load_previous_records(dataset, previous_path)
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


def write_dataset(
    path: Path,
    dataset: DatasetDefinition,
    records: list[dict[str, Any]],
    run_id: str,
    scraped_at: str,
    warnings: list[str],
) -> None:
    """Write one dataset envelope: records in ID order, keys sorted recursively."""
    exported = [
        json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True))
        for record in sorted(records, key=record_id_sort_key)
    ]
    write_json(
        path,
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


DATASETS: dict[str, DatasetDefinition] = {
    "innovations": DatasetDefinition(
        dataset_id="innovation",
        output_name="innovations.json",
        api_url=INNOVATION_API_URL,
        api_action="fetch_innovationAll_JSON",
        id_field="innovationid",
        detail_url_template=INNOVATION_DETAIL_URL,
        detail_title_selector="h1.card-title",
        minimum_records=400,
        parse_detail=parse_innovation_detail,
    ),
    "requirements": DatasetDefinition(
        dataset_id="requirement",
        output_name="requirements.json",
        api_url=REQUIREMENT_API_URL,
        api_action="fetch_requirementAll_JSON",
        id_field="requirementid",
        detail_url_template=REQUIREMENT_DETAIL_URL,
        detail_title_selector="h1.card-title",
        minimum_records=1,
        parse_detail=parse_requirement_detail,
    ),
}


def select_datasets(name: str) -> list[DatasetDefinition]:
    if name == "all":
        return list(DATASETS.values())
    return [DATASETS[name]]


def run_scrape(args: argparse.Namespace) -> int:
    selected = select_datasets(args.dataset)
    report = create_progress_reporter(args.quiet)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    verification = (
        BrowserVerification(args.chrome_path, args.browser_timeout, report)
        if args.browser
        else None
    )
    session = create_session(args.user_agent, verification)
    summary: dict[str, Any] = {"run_id": run_id, "datasets": {}, "warnings": []}

    has_failures = False
    for dataset in selected:
        report(f"{dataset.dataset_id}: starting")
        try:
            records = collect_dataset(
                session,
                dataset,
                args.delay,
                args.limit,
                report,
                args.progress_every,
                args.workers,
                args.user_agent,
            )
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
            output_path = OUTPUT_DIR / dataset.output_name
            counts = compare_with_previous(
                dataset,
                records,
                output_path,
                allow_large_removal=args.allow_large_removal,
            )
            record_warnings = [
                warning
                for record in records
                for warning in record.get("validation_warnings", [])
            ]
            write_dataset(
                output_path,
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
        except Exception as error:  # noqa: BLE001 - each dataset must report its own failure.
            has_failures = True
            summary["datasets"][dataset.dataset_id] = {
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "output_written": False,
            }
            report(f"{dataset.dataset_id}: failed — {type(error).__name__}: {error}")

    session.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if has_failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape public 38rat.nstru.ac.th APPTech content."
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
        help=f"Concurrent detail-page fetches, 1 to {MAX_WORKERS}.",
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
    parser.add_argument(
        "--chrome-path",
        help="Chrome/Chromium executable for browser verification (auto-detected by default; used with --browser).",
    )
    parser.add_argument(
        "--browser-timeout",
        type=float,
        default=40,
        help="Seconds allowed for each Chrome verification, greater than 0 and at most 300.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "If the site shows its browser check, open visible Chrome to pass it and reuse "
            "the verified session. Off by default: a blocked request fails clearly instead."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.delay < 0 or args.limit < 0 or args.progress_every < 1:
        raise SystemExit(
            "--delay and --limit must be non-negative; --progress-every must be at least 1"
        )
    if not 1 <= args.workers <= MAX_WORKERS:
        raise SystemExit(f"--workers must be between 1 and {MAX_WORKERS}")
    if not 0 < args.browser_timeout <= 300:
        raise SystemExit("--browser-timeout must be greater than 0 and at most 300")
    return run_scrape(args)


if __name__ == "__main__":
    sys.exit(main())
