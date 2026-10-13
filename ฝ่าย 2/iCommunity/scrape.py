#!/usr/bin/env python3
"""Incremental scraper for public iCommunity and AppTech PMUA records.

The script intentionally exports clean source records rather than a full raw
HTTP archive. Each run is compared with the previous export in the output
directory, which is the only state kept.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import math
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
APPTECH_BASE = "https://apptechpmua.com"
ICOMMUNITY_BASE = "https://www.icommunity.in.th"
DEFAULT_DELAY = 0.0  # Pause after each request, per worker.
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_PAGE_SIZE = 250
DEFAULT_USER_AGENT = "iCommunityCollector"
PLACEHOLDERS = {"", "-", "ไม่มี", "null", "undefined", "n/a"}
Reporter = Callable[[str], None]

DATASETS = (
    "apptech_group_catalog",
    "apptech_innovators",
    "apptech_innovations",
    "apptech_dashboard_summaries",
    "programme_background",
    "community_products",
    "activity_posts",
)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    output_name: str
    source_urls: tuple[str, ...]
    minimum_records: int


SPECS = {
    "apptech_group_catalog": DatasetSpec(
        "apptech_group_catalog",
        "apptech_group_catalog.json",
        (f"{APPTECH_BASE}/innovator/dashboard", f"{APPTECH_BASE}/innovation/dashboard"),
        2,
    ),
    "apptech_innovators": DatasetSpec(
        "apptech_innovators",
        "apptech_innovators.json",
        (f"{APPTECH_BASE}/innovatorExport/fetchExportList",),
        1,
    ),
    "apptech_innovations": DatasetSpec(
        "apptech_innovations",
        "apptech_innovations.json",
        (f"{APPTECH_BASE}/innovation/fetchExportList",),
        1,
    ),
    "apptech_dashboard_summaries": DatasetSpec(
        "apptech_dashboard_summaries",
        "apptech_dashboard_summaries.json",
        (
            f"{APPTECH_BASE}/innovatorExport/fetchExportList_dashboard",
            f"{APPTECH_BASE}/innovation/fetchExportList_dashboard",
        ),
        2,
    ),
    "programme_background": DatasetSpec(
        "programme_background",
        "programme_background.json",
        (f"{ICOMMUNITY_BASE}/index.php?rest_route=/wp/v2/pages/911",),
        1,
    ),
    "community_products": DatasetSpec(
        "community_products",
        "community_products.json",
        (f"{ICOMMUNITY_BASE}/index.php?rest_route=/wc/store/v1/products",),
        1,
    ),
    "activity_posts": DatasetSpec(
        "activity_posts",
        "activity_posts.json",
        (
            f"{ICOMMUNITY_BASE}/index.php?rest_route=/wp/v2/posts",
            f"{ICOMMUNITY_BASE}/?page_id=916",
        ),
        1,
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def report_factory(quiet: bool) -> Reporter:
    if quiet:
        return lambda _: None

    def report(message: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"[{stamp}] {message}", file=sys.stderr, flush=True)

    return report


def create_session(user_agent: str) -> requests.Session:
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
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


def fetch_ordered(fetch: Callable[[Any], Any], items: list[Any], workers: int) -> Generator[Any, None, None]:
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


def fetch_response(
    session: requests.Session,
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 60,
) -> requests.Response:
    response = session.request(method, url, data=data, timeout=timeout)
    response.raise_for_status()
    return response


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 60,
) -> tuple[Any, requests.structures.CaseInsensitiveDict[str]]:
    response = fetch_response(session, url, method=method, data=data, timeout=timeout)
    return response.json(), response.headers


def fetch_text(session: requests.Session, url: str, timeout: int = 60) -> str:
    response = fetch_response(session, url, timeout=timeout)
    response.encoding = response.encoding or "utf-8"
    return response.text


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = (
        unicodedata.normalize("NFC", str(value))
        .replace("\ufeff", "")
        .replace("\u00a0", " ")
    )
    text = re.sub(r"[\t\r\n ]+", " ", text).strip()
    return None if text.casefold() in PLACEHOLDERS else text


def clean_html(value: Any) -> str | None:
    text = clean_text(value)
    return text if text else None


def html_text(value: Any) -> str | None:
    markup = clean_html(value)
    if not markup:
        return None
    return clean_text(BeautifulSoup(markup, "html.parser").get_text(" ", strip=True))


def parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def semantic_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def comparison_hash(record: dict[str, Any]) -> str:
    """Hash used only to compare runs: retrieved_at changes every run and is not content."""
    return semantic_hash({key: value for key, value in record.items() if key != "retrieved_at"})


def redact_contacts(value: Any) -> str | None:
    """Keep useful prose while removing common phone, email, and social handles."""
    markup = clean_html(value)
    if not markup:
        return None
    markup = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[email redacted]", markup, flags=re.I)
    markup = re.sub(r"(?<!\d)(?:\+66|0)\d(?:[\s-]?\d){7,9}(?!\d)", "[phone redacted]", markup)
    markup = re.sub(r"(?i)(line\s*(?:id)?\s*[:：]\s*)[^<\s]+", r"\1[redacted]", markup)
    markup = re.sub(r"(?i)(facebook\s*[:：]\s*)[^<\s]+", r"\1[redacted]", markup)
    return markup


def maybe_sleep(delay: float) -> None:
    if delay > 0:
        time.sleep(delay)


def apptech_common_fields(group_id: str, page_size: int, start: int) -> dict[str, str]:
    return {
        "length": str(page_size),
        "start": str(start),
        "searchYear": "",
        "searchGroup": group_id,
        "searchInstitute": "",
        "searchResearcher": "",
        "searchgeography": "",
        "searchProvince": "",
        "searchAmphor": "",
        "searchTambon": "",
        "searchProject": "",
        "typePage": "dashboard",
        "action": "SEARCH",
    }


def dashboard_groups(html: str, dashboard_entity: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", attrs={"name": "searchGroup"})
    if not isinstance(select, Tag):
        raise ValueError(f"{dashboard_entity}: missing searchGroup selector")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in select.find_all("option"):
        group_id = clean_text(option.get("value"))
        label = clean_text(option.get_text(" ", strip=True))
        if group_id is None or label is None or group_id in seen:
            continue
        seen.add(group_id)
        if not group_id.isdigit():
            continue
        records.append(
            {
                "record_type": "apptech_group",
                "source_key": f"{dashboard_entity}:{group_id}",
                "dashboard_entity": dashboard_entity,
                "group_id": group_id,
                "group_label_th": label,
                "source_url": source_url,
                "source_payload_hash": semantic_hash({"group_id": group_id, "label": label}),
            }
        )
    if not records:
        raise ValueError(f"{dashboard_entity}: no numeric AppTech groups found")
    return records


def collect_group_catalog(
    session: requests.Session, delay: float, timeout: int, reporter: Reporter
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    records: list[dict[str, Any]] = []
    groups_by_entity: dict[str, dict[str, str]] = {}
    for entity, url in (
        ("innovator", f"{APPTECH_BASE}/innovator/dashboard"),
        ("innovation", f"{APPTECH_BASE}/innovation/dashboard"),
    ):
        reporter(f"Fetching AppTech {entity} group catalogue")
        html = fetch_text(session, url, timeout)
        parsed = dashboard_groups(html, entity, url)
        records.extend(parsed)
        groups_by_entity[entity] = {r["group_id"]: r["group_label_th"] for r in parsed}
        maybe_sleep(delay)
    return records, groups_by_entity


def section_answer(soup: BeautifulSoup, label_fragment: str) -> tuple[str | None, str | None]:
    """Return the first answer paragraph associated with a detail-page heading."""
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        label = clean_text(heading.get_text(" ", strip=True)) or ""
        if label_fragment not in label:
            continue
        container = heading.find_parent("div", class_=lambda classes: classes and "col-" in " ".join(classes))
        if not isinstance(container, Tag):
            container = heading.parent
        if isinstance(container, Tag):
            answer = container.find("p", class_=lambda classes: classes and "ans" in classes)
            if isinstance(answer, Tag):
                return clean_html(str(answer)), html_text(str(answer))
        answer = heading.find_next("p", class_=lambda classes: classes and "ans" in classes)
        if isinstance(answer, Tag):
            return clean_html(str(answer)), html_text(str(answer))
    return None, None


def readiness_values(soup: BeautifulSoup, label_fragment: str) -> dict[str, int | None]:
    for heading in soup.find_all("h4"):
        label = clean_text(heading.get_text(" ", strip=True)) or ""
        if label_fragment not in label:
            continue
        container = heading.find_parent("div", class_=lambda classes: classes and "progress-bar-style-03" in classes)
        if not isinstance(container, Tag):
            continue
        values = [parse_int(node.get("aria-valuenow")) for node in container.select(".progress-bar[aria-valuenow]")]
        return {
            "start_level": values[0] if len(values) > 0 else None,
            "current_level": values[1] if len(values) > 1 else None,
        }
    return {"start_level": None, "current_level": None}


def parse_innovator_detail(html: str, source_url: str) -> dict[str, Any]:
    begin_match = re.search(r"var\s+beginData\s*=\s*\[([^\]]*)\]", html)
    last_match = re.search(r"var\s+lastData\s*=\s*\[([^\]]*)\]", html)

    def values(match: re.Match[str] | None) -> list[int | None]:
        if not match:
            return []
        return [parse_int(item.strip().strip("'\"")) for item in match.group(1).split(",")]

    begin, last = values(begin_match), values(last_match)
    if len(begin) != 3 or len(last) != 3:
        raise ValueError("innovator detail: expected three begin/current score values")
    return {
        "source_url": source_url,
        "collaboration_network": {"start_level": begin[0], "current_level": last[0]},
        "technology_knowledge": {"start_level": begin[1], "current_level": last[1]},
        "learning_process_management": {"start_level": begin[2], "current_level": last[2]},
    }


def parse_innovation_detail(html: str, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    type_html, type_text = section_answer(soup, "ประเภทของนวัตกรรม")
    description_html, description_text = section_answer(soup, "องค์ความรู้/เทคโนโลยี")
    cost_html, cost_text = section_answer(soup, "ต้นทุนการใช้นวัตกรรม")
    if type_html is None and description_html is None:
        raise ValueError("innovation detail: expected detail sections were absent")
    return {
        "source_url": source_url,
        "innovation_type": type_text,
        "technology_readiness": readiness_values(soup, "ระดับความพร้อมของนวัตกรรม"),
        "social_readiness": readiness_values(soup, "ระดับความพร้อมของสังคม"),
        "usage_cost_thb_raw": cost_text,
        "short_description_html": description_html,
        "short_description_text": description_text,
    }


def normal_location(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "geocode": clean_text(row.get("area_geocode")),
        "tambon_id": clean_text(row.get("area_tambonID") or row.get("area_tambon_id") or row.get("tambonID")),
        "tambon": clean_text(row.get("area_tambon") or row.get("tambonName")),
        "amphor_id": clean_text(row.get("area_amphorID") or row.get("amphorID")),
        "amphor": clean_text(row.get("area_amphor") or row.get("amphorName")),
        "province_id": clean_text(row.get("provinceID")),
        "province": clean_text(row.get("provinceName") or row.get("area_province")),
        "geography_id": clean_text(row.get("geographyID")),
        "geography": clean_text(row.get("geographyName")),
        "latitude": parse_float(row.get("area_latitude")),
        "longitude": parse_float(row.get("area_longitude")),
    }


def normal_project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": clean_text(row.get("projectID")),
        "code": clean_text(row.get("projectCode")),
        "family_code": clean_text(row.get("projectFamilyCode")),
        "reference_project_id": clean_text(row.get("projectRefProjectID")),
        "name": clean_text(row.get("projectName")),
        "year_be": clean_text(row.get("projectYear")),
    }


def normal_innovator_row(
    row: dict[str, Any], group_id: str, group_label: str, detail: dict[str, Any] | None
) -> dict[str, Any]:
    innovator_id = clean_text(row.get("innovatorID"))
    if not innovator_id:
        raise ValueError("innovator list: missing innovatorID")
    name = clean_text(row.get("innovatorName"))
    if not name:
        raise ValueError(f"innovator list: {innovator_id} missing innovatorName")
    return {
        "record_type": "apptech_innovator",
        "source_key": f"{group_id}:{innovator_id}",
        "scope": {"group_id": group_id, "group_label_th": group_label},
        "innovator_id": innovator_id,
        "innovator_name": name,
        "community_role_raw": clean_text(row.get("innovatorCommunityRole")),
        "profile_token": clean_text(row.get("innovatorOutstandingID")),
        "linked_innovation_name": clean_text(row.get("innovationName")),
        "project": normal_project(row),
        "researcher": {"user_id": clean_text(row.get("userID")), "full_name": clean_text(row.get("fullName"))},
        "institute": {"id": clean_text(row.get("instituteID")), "name": clean_text(row.get("instituteName"))},
        "location": normal_location(row),
        "detail": detail,
        "source_url": f"{APPTECH_BASE}/innovatorExport/fetchExportList",
        "source_payload_hash": semantic_hash(row),
    }


def normal_innovation_row(
    row: dict[str, Any], group_id: str, group_label: str, detail: dict[str, Any] | None
) -> dict[str, Any]:
    innovation_id = clean_text(row.get("innovationID"))
    if not innovation_id:
        raise ValueError("innovation list: missing innovationID")
    name = clean_text(row.get("innovationName"))
    if not name:
        raise ValueError(f"innovation list: {innovation_id} missing innovationName")
    return {
        "record_type": "apptech_innovation",
        "source_key": f"{group_id}:{innovation_id}",
        "scope": {"group_id": group_id, "group_label_th": group_label},
        "innovation_id": innovation_id,
        "innovation_name": name,
        "cover_image_path": clean_text(row.get("innovationCover")),
        "technology_group_raw": clean_text(row.get("group_technology_text")),
        "latest_readiness_level_raw": clean_text(row.get("latestReadinessLevel")),
        "latest_social_readiness_level_raw": clean_text(row.get("latestReadinessLevelSRL")),
        "project": normal_project(row),
        "researcher": {"user_id": clean_text(row.get("userID")), "full_name": clean_text(row.get("fullName"))},
        "institute": {"id": clean_text(row.get("instituteID")), "name": clean_text(row.get("instituteName"))},
        "location": normal_location(row),
        "detail": detail,
        "source_url": f"{APPTECH_BASE}/innovation/fetchExportList",
        "source_payload_hash": semantic_hash(row),
    }


def collect_apptech_records(
    session: requests.Session,
    *,
    entity: str,
    groups: dict[str, str],
    group_filter: set[str] | None,
    page_size: int,
    limit: int | None,
    delay: float,
    timeout: int,
    skip_details: bool,
    reporter: Reporter,
    workers: int = DEFAULT_WORKERS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    if entity not in {"innovator", "innovation"}:
        raise ValueError(f"Unknown AppTech entity: {entity}")
    endpoint = "/innovatorExport/fetchExportList" if entity == "innovator" else "/innovation/fetchExportList"
    requested_groups = [(group_id, label) for group_id, label in groups.items() if group_filter is None or group_id in group_filter]
    if not requested_groups:
        raise ValueError(f"{entity}: requested groups are not present in dashboard catalogue")

    all_rows: list[tuple[dict[str, Any], str, str]] = []
    warnings: list[str] = []
    partial = group_filter is not None or limit is not None
    for group_id, label in requested_groups:
        if limit is not None and len(all_rows) >= limit:
            break
        reporter(f"Fetching AppTech {entity} group {group_id}: {label}")
        expected: int | None = None
        seen: set[str] = set()
        start = 0
        while True:
            fields = apptech_common_fields(group_id, page_size, start)
            fields["searchInnovator" if entity == "innovator" else "searchInnovation"] = ""
            if entity == "innovation":
                fields["searchGroupTech"] = ""
            payload, _ = fetch_json(session, f"{APPTECH_BASE}{endpoint}", method="POST", data=fields, timeout=timeout)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError(f"{entity} group {group_id}: invalid list response")
            reported = parse_int(payload.get("recordsFiltered"))
            if reported is None:
                raise ValueError(f"{entity} group {group_id}: missing recordsFiltered")
            if expected is None:
                expected = reported
            elif expected != reported:
                raise ValueError(f"{entity} group {group_id}: recordsFiltered changed from {expected} to {reported}")
            page_rows = payload["data"]
            if not page_rows and start < expected:
                raise ValueError(f"{entity} group {group_id}: empty page before expected total")
            for row in page_rows:
                if not isinstance(row, dict):
                    raise ValueError(f"{entity} group {group_id}: non-object row")
                source_id = clean_text(row.get("innovatorID" if entity == "innovator" else "innovationID"))
                if not source_id:
                    raise ValueError(f"{entity} group {group_id}: missing source ID")
                if source_id in seen:
                    raise ValueError(f"{entity} group {group_id}: duplicate source ID {source_id}")
                seen.add(source_id)
                all_rows.append((row, group_id, label))
                if limit is not None and len(all_rows) >= limit:
                    break
            if limit is not None and len(all_rows) >= limit:
                break
            start += len(page_rows)
            if start >= expected:
                break
            maybe_sleep(delay)
        if limit is None and len(seen) != expected:
            raise ValueError(f"{entity} group {group_id}: collected {len(seen)} of {expected} expected rows")
        maybe_sleep(delay)

    details: dict[str, dict[str, Any] | None] = {}
    if not skip_details:
        tokens = {
            clean_text(row.get("innovatorOutstandingID" if entity == "innovator" else "innovationID"))
            for row, _, _ in all_rows
        }
        ordered_tokens = sorted(token for token in tokens if token)
        worker_session = thread_session_provider(user_agent)

        def detail_url(token: str) -> str:
            return f"{APPTECH_BASE}/{'innovator' if entity == 'innovator' else 'innovation'}/content/{token}"

        def fetch_page(token: str) -> str | requests.RequestException:
            """HTTP only. A failed request is returned so the main thread can warn, as before."""
            try:
                return fetch_text(worker_session(), detail_url(token), timeout)
            except requests.RequestException as exc:
                return exc
            finally:
                maybe_sleep(delay)

        with closing(fetch_ordered(fetch_page, ordered_tokens, workers)) as pages:
            for token, page in zip(ordered_tokens, pages, strict=True):
                reporter(f"Fetching AppTech {entity} detail {token[:12]}...")
                if isinstance(page, requests.RequestException):
                    details[token] = None
                    warnings.append(f"{entity} detail {token[:16]} request failed: {page}")
                    continue
                url = detail_url(token)
                try:
                    details[token] = parse_innovator_detail(page, url) if entity == "innovator" else parse_innovation_detail(page, url)
                except ValueError as exc:
                    details[token] = None
                    warnings.append(f"{entity} detail {token[:16]} parse failed: {exc}")

    records: list[dict[str, Any]] = []
    for row, group_id, label in all_rows:
        token = clean_text(row.get("innovatorOutstandingID" if entity == "innovator" else "innovationID"))
        detail = details.get(token) if token else None
        records.append(
            normal_innovator_row(row, group_id, label, detail)
            if entity == "innovator"
            else normal_innovation_row(row, group_id, label, detail)
        )
    return records, warnings, partial


def summary_fields(entity: str, group_id: str) -> dict[str, str]:
    fields = apptech_common_fields(group_id, -1, 0)
    fields.pop("action", None)
    fields.pop("typePage", None)
    fields["searchInnovator" if entity == "innovator" else "searchInnovation"] = ""
    if entity == "innovation":
        fields["searchGroupTech"] = ""
    return fields


def collect_summaries(
    session: requests.Session,
    groups_by_entity: dict[str, dict[str, str]],
    group_filter: set[str] | None,
    delay: float,
    timeout: int,
    reporter: Reporter,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    partial = group_filter is not None
    for entity in ("innovator", "innovation"):
        endpoint = f"{APPTECH_BASE}/{'innovatorExport' if entity == 'innovator' else 'innovation'}/fetchExportList_dashboard"
        scopes = [("", "ทั้งหมด")] + [
            (group_id, label)
            for group_id, label in groups_by_entity[entity].items()
            if group_filter is None or group_id in group_filter
        ]
        for group_id, label in scopes:
            reporter(f"Fetching AppTech {entity} dashboard summary: {label}")
            payload, _ = fetch_json(session, endpoint, method="POST", data=summary_fields(entity, group_id), timeout=timeout)
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise ValueError(f"{entity} summary {label}: invalid response")
            geo_rows = payload.get("innovator")
            if not isinstance(geo_rows, list):
                raise ValueError(f"{entity} summary {label}: geographic rows missing")
            records.append(
                {
                    "record_type": "apptech_dashboard_summary",
                    "source_key": f"{entity}:{group_id or 'all'}",
                    "dashboard_entity": entity,
                    "scope": {"group_id": group_id or None, "label": label},
                    "reported_count_raw": clean_text(payload.get("count")),
                    "geographic_rows": geo_rows,
                    "source_url": endpoint,
                    "retrieved_at": utc_now(),
                    "source_payload_hash": semantic_hash(payload),
                }
            )
            maybe_sleep(delay)
    return records, warnings, partial


def wp_url(route: str, **params: Any) -> str:
    query = {"rest_route": route, **{key: value for key, value in params.items() if value is not None}}
    return f"{ICOMMUNITY_BASE}/index.php?{urlencode(query)}"


def collect_programme_background(session: requests.Session, timeout: int) -> tuple[list[dict[str, Any]], list[str], bool]:
    url = wp_url("/wp/v2/pages/911")
    payload, _ = fetch_json(session, url, timeout=timeout)
    if not isinstance(payload, dict) or parse_int(payload.get("id")) != 911:
        raise ValueError("programme background: unexpected WordPress page")
    content_html = redact_contacts(payload.get("content", {}).get("rendered") if isinstance(payload.get("content"), dict) else None)
    record = {
        "record_type": "wordpress_page",
        "source_key": "911",
        "page_id": 911,
        "published_at": clean_text(payload.get("date")),
        "modified_at": clean_text(payload.get("modified")),
        "title_html": redact_contacts(payload.get("title", {}).get("rendered") if isinstance(payload.get("title"), dict) else None),
        "content_html": content_html,
        "content_text": html_text(content_html),
        "source_url": clean_text(payload.get("link")) or f"{ICOMMUNITY_BASE}/?page_id=911",
        "source_payload_hash": semantic_hash(payload),
    }
    return [record], [], False


def collect_paginated(
    session: requests.Session,
    route: str,
    timeout: int,
    delay: float,
    reporter: Reporter,
) -> list[dict[str, Any]]:
    first_url = wp_url(route, per_page=100, page=1)
    first, headers = fetch_json(session, first_url, timeout=timeout)
    if not isinstance(first, list):
        raise ValueError(f"{route}: expected a JSON array")
    total_pages = parse_int(headers.get("X-WP-TotalPages"))
    total_records = parse_int(headers.get("X-WP-Total"))
    if total_pages is None or total_pages < 1 or total_records is None:
        raise ValueError(f"{route}: pagination headers are missing")
    records = list(first)
    for page in range(2, total_pages + 1):
        reporter(f"Fetching {route} page {page}/{total_pages}")
        payload, page_headers = fetch_json(session, wp_url(route, per_page=100, page=page), timeout=timeout)
        if not isinstance(payload, list):
            raise ValueError(f"{route}: page {page} is not an array")
        if parse_int(page_headers.get("X-WP-TotalPages")) != total_pages:
            raise ValueError(f"{route}: page count changed during crawl")
        records.extend(payload)
        maybe_sleep(delay)
    if len(records) != total_records:
        raise ValueError(f"{route}: collected {len(records)} records, expected {total_records}")
    return records


def collect_products(
    session: requests.Session, timeout: int, delay: float, reporter: Reporter
) -> tuple[list[dict[str, Any]], list[str], bool]:
    payloads = collect_paginated(session, "/wc/store/v1/products", timeout, delay, reporter)
    records: list[dict[str, Any]] = []
    for product in payloads:
        if not isinstance(product, dict):
            raise ValueError("products: non-object result")
        product_id = parse_int(product.get("id"))
        name = clean_text(product.get("name"))
        if product_id is None or name is None:
            raise ValueError("products: product missing ID or name")
        short_html = redact_contacts(product.get("short_description"))
        description_html = redact_contacts(product.get("description"))
        records.append(
            {
                "record_type": "community_product",
                "source_key": str(product_id),
                "product_id": product_id,
                "name": name,
                "slug": clean_text(product.get("slug")),
                "permalink": clean_text(product.get("permalink")),
                "sku": clean_text(product.get("sku")),
                "short_description_html": short_html,
                "description_html": description_html,
                "prices": product.get("prices") if isinstance(product.get("prices"), dict) else {},
                "price_range": product.get("price_range") if isinstance(product.get("price_range"), dict) else None,
                "on_sale": bool(product.get("on_sale")),
                "images": product.get("images") if isinstance(product.get("images"), list) else [],
                "categories": product.get("categories") if isinstance(product.get("categories"), list) else [],
                "tags": product.get("tags") if isinstance(product.get("tags"), list) else [],
                "attributes": product.get("attributes") if isinstance(product.get("attributes"), list) else [],
                "stock_status": clean_text(product.get("stock_status")),
                "is_in_stock": bool(product.get("is_in_stock")),
                "is_purchasable": bool(product.get("is_purchasable")),
                "source_payload_hash": semantic_hash(product),
            }
        )
    return records, [], False


def collect_posts(
    session: requests.Session, timeout: int, delay: float, reporter: Reporter
) -> tuple[list[dict[str, Any]], list[str], bool]:
    payloads = collect_paginated(session, "/wp/v2/posts", timeout, delay, reporter)
    records: list[dict[str, Any]] = []
    for post in payloads:
        if not isinstance(post, dict):
            raise ValueError("posts: non-object result")
        post_id = parse_int(post.get("id"))
        title = post.get("title", {}).get("rendered") if isinstance(post.get("title"), dict) else None
        if post_id is None or clean_text(title) is None:
            raise ValueError("posts: post missing ID or title")
        content_html = redact_contacts(post.get("content", {}).get("rendered") if isinstance(post.get("content"), dict) else None)
        records.append(
            {
                "record_type": "activity_post",
                "source_key": str(post_id),
                "post_id": post_id,
                "published_at": clean_text(post.get("date")),
                "modified_at": clean_text(post.get("modified")),
                "slug": clean_text(post.get("slug")),
                "title_html": redact_contacts(title),
                "content_html": content_html,
                "content_text": html_text(content_html),
                "excerpt_html": redact_contacts(post.get("excerpt", {}).get("rendered") if isinstance(post.get("excerpt"), dict) else None),
                "author_id": parse_int(post.get("author")),
                "featured_media_id": parse_int(post.get("featured_media")),
                "category_ids": [value for value in (parse_int(item) for item in post.get("categories", [])) if value is not None] if isinstance(post.get("categories"), list) else [],
                "tag_ids": [value for value in (parse_int(item) for item in post.get("tags", [])) if value is not None] if isinstance(post.get("tags"), list) else [],
                "source_url": clean_text(post.get("link")),
                "source_payload_hash": semantic_hash(post),
            }
        )
    return records, [], False


def validate_records(dataset: str, records: list[dict[str, Any]]) -> None:
    if len(records) < SPECS[dataset].minimum_records:
        raise ValueError(f"{dataset}: found {len(records)} records, below safety floor")
    seen: set[str] = set()
    for record in records:
        key = clean_text(record.get("source_key"))
        if not key:
            raise ValueError(f"{dataset}: record missing source_key")
        if key in seen:
            raise ValueError(f"{dataset}: duplicate source_key {key}")
        seen.add(key)


def load_previous_records(dataset: str, path: Path) -> dict[str, dict[str, Any]]:
    """Return the previous export's records by source_key; empty on a first run.

    An existing file that cannot be read fails the dataset so the removal guard
    is never skipped silently.
    """
    if not path.exists():
        return {}
    previous: dict[str, dict[str, Any]] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))["data"]
        if not isinstance(data, list):
            raise TypeError("'data' is not a list")
        for item in data:
            key = str(item["source_key"])
            if key in previous:
                raise ValueError(f"duplicate source_key {key}")
            previous[key] = item
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{dataset}: cannot read previous output {path.name}: {exc!r}") from exc
    return previous


def compare_with_previous(
    dataset: str,
    records: list[dict[str, Any]],
    previous_path: Path,
    *,
    allow_large_removal: bool,
) -> dict[str, int]:
    if records:
        validate_records(dataset, records)
    previous = load_previous_records(dataset, previous_path)
    incoming = {str(record["source_key"]): record for record in records}
    removed = set(previous) - set(incoming)
    if previous and removed and not allow_large_removal:
        threshold = max(10, math.ceil(len(previous) * 0.5))
        if len(removed) >= threshold:
            raise ValueError(f"{dataset}: refusing to retire {len(removed)} of {len(previous)} records")
    result = {"new": 0, "changed": 0, "unchanged": 0, "retired": len(removed)}
    for key, record in incoming.items():
        if key not in previous:
            result["new"] += 1
        elif comparison_hash(previous[key]) != comparison_hash(record):
            result["changed"] += 1
        else:
            result["unchanged"] += 1
    return result


def retain_known_details(
    previous: dict[str, dict[str, Any]], dataset: str, records: list[dict[str, Any]]
) -> None:
    """Keep previously collected profile details when --skip-details is used.

    A run with --skip-details may refresh list-level fields, but it must not
    turn a successfully collected nested detail object into null merely because
    detail pages were intentionally skipped.
    """
    if dataset not in {"apptech_innovators", "apptech_innovations"}:
        return
    for record in records:
        prior_detail = previous.get(str(record["source_key"]), {}).get("detail")
        if record.get("detail") is None and prior_detail is not None:
            record["detail"] = prior_detail


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        except BaseException:
            handle.close()
            temp_path.unlink(missing_ok=True)
            raise
    temp_path.replace(path)


def write_export(
    output_dir: Path, dataset: str, records: list[dict[str, Any]], warnings: list[str], scraped_at: str
) -> None:
    """Write one export: records in source_key order, keys sorted recursively."""
    spec = SPECS[dataset]
    exported = [
        json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for record in sorted(records, key=lambda record: str(record["source_key"]))
    ]
    write_json(
        output_dir / spec.output_name,
        {
            "schema_version": 1,
            "dataset": dataset,
            "source_urls": list(spec.source_urls),
            "scraped_at": scraped_at,
            "record_count": len(exported),
            "warnings": warnings,
            "data": exported,
        },
    )


def parse_groups_arg(value: str | None) -> set[str] | None:
    if not value:
        return None
    groups = {item.strip() for item in value.split(",") if item.strip()}
    if not groups or any(not item.isdigit() for item in groups):
        raise ValueError("--groups must be comma-separated numeric group IDs")
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", action="append", choices=DATASETS, help="Collect only this dataset. Repeatable.")
    parser.add_argument("--groups", help="Comma-separated AppTech group IDs for a smoke test. Writes nothing.")
    parser.add_argument("--limit", type=int, help="Limit AppTech list rows for a smoke test. Writes nothing.")
    parser.add_argument("--skip-details", action="store_true", help="Do not fetch AppTech detail pages.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Seconds each worker pauses after every request.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Concurrent AppTech detail-page fetches, 1 to {MAX_WORKERS}.")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--allow-large-removal", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.delay < 0 or args.page_size < 1 or args.limit is not None and args.limit < 1:
        parser.error("delay must be non-negative; page size and limit must be positive")
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    selected = tuple(args.dataset or DATASETS)
    group_filter = parse_groups_arg(args.groups)
    reporter = report_factory(args.quiet)
    session = create_session(args.user_agent)
    groups_by_entity: dict[str, dict[str, str]] | None = None
    group_records: list[dict[str, Any]] | None = None

    def ensure_groups() -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
        nonlocal group_records, groups_by_entity
        if group_records is None or groups_by_entity is None:
            group_records, groups_by_entity = collect_group_catalog(session, args.delay, args.timeout, reporter)
        return group_records, groups_by_entity

    collectors: dict[str, Callable[[], tuple[list[dict[str, Any]], list[str], bool]]] = {
        "apptech_group_catalog": lambda: (ensure_groups()[0], [], False),
        "apptech_innovators": lambda: collect_apptech_records(
            session, entity="innovator", groups=ensure_groups()[1]["innovator"], group_filter=group_filter,
            page_size=args.page_size, limit=args.limit, delay=args.delay, timeout=args.timeout,
            skip_details=args.skip_details, reporter=reporter, workers=args.workers, user_agent=args.user_agent,
        ),
        "apptech_innovations": lambda: collect_apptech_records(
            session, entity="innovation", groups=ensure_groups()[1]["innovation"], group_filter=group_filter,
            page_size=args.page_size, limit=args.limit, delay=args.delay, timeout=args.timeout,
            skip_details=args.skip_details, reporter=reporter, workers=args.workers, user_agent=args.user_agent,
        ),
        "apptech_dashboard_summaries": lambda: collect_summaries(
            session, ensure_groups()[1], group_filter, args.delay, args.timeout, reporter
        ),
        "programme_background": lambda: collect_programme_background(session, args.timeout),
        "community_products": lambda: collect_products(session, args.timeout, args.delay, reporter),
        "activity_posts": lambda: collect_posts(session, args.timeout, args.delay, reporter),
    }

    smoke = group_filter is not None or args.limit is not None
    summary: dict[str, Any] = {"run_id": utc_now(), "datasets": {}, "failures": []}
    for dataset in selected:
        try:
            reporter(f"Collecting {dataset}")
            records, warnings, _ = collectors[dataset]()
            if smoke:
                summary["datasets"][dataset] = {"status": "smoke_test", "record_count": len(records), "warnings": warnings}
                reporter(f"{dataset}: smoke test collected {len(records)} records")
                continue
            previous_path = args.output_dir / SPECS[dataset].output_name
            if args.skip_details:
                retain_known_details(load_previous_records(dataset, previous_path), dataset, records)
            stats = compare_with_previous(
                dataset, records, previous_path, allow_large_removal=args.allow_large_removal
            )
            warnings = [*warnings, f"sync: {stats}"]
            write_export(args.output_dir, dataset, records, warnings, utc_now())
            summary["datasets"][dataset] = {"status": "success", "record_count": len(records), **stats, "warnings": warnings}
            reporter(f"{dataset}: {len(records)} records")
        except Exception as exc:  # Preserve other datasets and prior good exports.
            summary["failures"].append(f"{dataset}: {exc}")
            summary["datasets"][dataset] = {"status": "failed", "error": str(exc)}
            print(f"{dataset}: {exc}", file=sys.stderr)
    summary["status"] = "failed" if summary["failures"] else "success"
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
