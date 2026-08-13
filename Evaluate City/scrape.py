"""Incremental scraper for the public Evaluate the City dashboard.

The site publishes its source data as JavaScript literals in one inline script.
This module downloads that public HTML, parses only the four data literals, and
stores normalized city/metric observations and their history in SQLite. It
never executes JavaScript from the site.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import math
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import chompjs

ROOT = Path(__file__).resolve().parent
SOURCE_URL = "https://evaluatethecity.netlify.app/"
DEFAULT_OUTPUT_DIR = ROOT / "output"
DEFAULT_STATE_PATH = ROOT / "state" / "evaluate_city.sqlite3"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_USER_AGENT = "EvaluateCityPublicScraper/1.0"
PARSER_VERSION = "evaluate-city-v2"

CATEGORIES = {
    "geoData": "environment",
    "infData": "infrastructure",
    "pplData": "society_economy",
}
DATA_VARIABLES = ("cityNames", *CATEGORIES)
SUCCESS_OUTCOMES = ("success", "unchanged_data")
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class MetricDefinition:
    """Metadata and validation policy for one source indicator."""

    category_id: str
    source_key: str
    label_th: str
    display_unit: str
    description_th: str
    hard_min: float | None = 0
    hard_max: float | None = None
    warning_min: float | None = None
    warning_max: float | None = None

    @property
    def metric_id(self) -> str:
        return f"{self.category_id}.{self.source_key}"


def metric(
    category_id: str,
    source_key: str,
    label_th: str,
    display_unit: str,
    description_th: str,
    *,
    hard_min: float | None = 0,
    hard_max: float | None = None,
    warning_min: float | None = None,
    warning_max: float | None = None,
) -> MetricDefinition:
    return MetricDefinition(
        category_id=category_id,
        source_key=source_key,
        label_th=label_th,
        display_unit=display_unit,
        description_th=description_th,
        hard_min=hard_min,
        hard_max=hard_max,
        warning_min=warning_min,
        warning_max=warning_max,
    )


# This is the one place to maintain source keys, labels, units, and basic
# validation policy when the dashboard changes.
METRIC_CATALOG: tuple[MetricDefinition, ...] = (
    metric("environment", "heatDays", "วันร้อนจัด", "วัน/ปี", ">38°C ต่อปี", warning_max=366),
    metric("environment", "pm25", "PM2.5", "µg/m³", "เฉลี่ยรายปี", warning_max=500),
    metric("environment", "canopy", "ร่มเงาไม้", "%", "ความครอบคลุม", hard_max=100),
    metric("environment", "flood", "เสี่ยงน้ำท่วม", "%", "ปชก.ในพื้นที่", hard_max=100),
    metric("environment", "hazard", "เสี่ยงภัยพิบัติ", "%", "พื้นที่เสี่ยง", hard_max=100),
    metric(
        "environment",
        "lst",
        "อุณหภูมิ",
        "°C",
        "ผิวพื้นเฉลี่ย",
        hard_min=-100,
        hard_max=100,
        warning_min=-20,
        warning_max=70,
    ),
    metric("environment", "green", "พื้นที่สีเขียว", "%", "ระยะเข้าถึง 300ม.", hard_max=100),
    metric("infrastructure", "electricity", "เข้าถึงไฟฟ้า", "%", "ครัวเรือน", hard_max=100),
    metric("infrastructure", "waterAccess", "น้ำประปา", "%", "เข้าถึงสะอาด", hard_max=100),
    metric("infrastructure", "internet", "อินเทอร์เน็ต", "%", "เข้าถึงในบ้าน", hard_max=100),
    metric("infrastructure", "wasteMgmt", "จัดการขยะ", "%", "กำจัดถูกต้อง", hard_max=100),
    metric("infrastructure", "transport", "ขนส่งมวลชน", "%", "ระยะ 500ม.", hard_max=100),
    metric(
        "infrastructure",
        "trafficSpeed",
        "ความเร็วรถ",
        "km/hr",
        "ชม.เร่งด่วน",
        warning_max=200,
    ),
    metric(
        "infrastructure", "roadDist", "ถนนหลัก", "เมตร", "ระยะห่าง", warning_max=10000
    ),
    metric("infrastructure", "markets", "ตลาด", "แห่ง", "ในพื้นที่"),
    metric("infrastructure", "financial", "ธนาคาร", "แห่ง", "ต่อแสนคน"),
    metric("infrastructure", "healthcare", "สถานพยาบาล", "แห่ง", "ในพื้นที่"),
    metric("infrastructure", "beds", "เตียงผู้ป่วย", "เตียง", "ต่อ 1,000 คน"),
    metric("infrastructure", "safety", "สถานีตำรวจ", "แห่ง", "ในพื้นที่"),
    metric("infrastructure", "vocational", "ศูนย์ฝึกอาชีพ", "แห่ง", "ในพื้นที่"),
    metric("infrastructure", "port", "ไปท่าเรือ", "นาที", "เวลาเดินทาง", warning_max=1440),
    metric(
        "infrastructure",
        "airportTime",
        "ไปสนามบิน",
        "นาที",
        "เวลาเดินทาง",
        warning_max=1440,
    ),
    metric("society_economy", "youth", "วัยเด็ก", "คน", "<18 ปี"),
    metric("society_economy", "working", "วัยแรงงาน", "คน", "18-60 ปี"),
    metric("society_economy", "elderly", "ผู้สูงอายุ", "คน", ">60 ปี"),
    metric("society_economy", "labor", "แรงงาน", "%", "การมีส่วนร่วม", hard_max=100),
    metric(
        "society_economy",
        "unemploymentClean",
        "ว่างงาน",
        "%",
        "อัตราว่างงาน",
        hard_max=100,
    ),
    metric("society_economy", "informal", "นอกระบบ", "%", "แรงงาน", hard_max=100),
    metric("society_economy", "wage", "ค่าจ้าง", "บาท", "ขั้นต่ำ/วัน"),
    metric("society_economy", "productivity", "ผลิตภาพ", "บาท", "ต่อคน/ปี"),
    metric("society_economy", "budget", "งบประมาณ", "บาท", "รายปี"),
    metric("society_economy", "revenue", "รายได้", "%", "ท้องถิ่นจัดเก็บ", hard_max=100),
    metric("society_economy", "establishments", "ธุรกิจ", "แห่ง", "สถานประกอบการ"),
    metric("society_economy", "debt", "หนี้สิน", "บาท", "ครัวเรือน"),
    metric("society_economy", "accidents", "อุบัติเหตุ", "ราย", "ตาย/แสนคน"),
    metric("society_economy", "itaScore", "ITA", "คะแนน", "คุณธรรม", hard_max=100),
    metric("society_economy", "cultural", "วัฒนธรรม", "แห่ง", "แหล่งเรียนรู้"),
    metric(
        "society_economy",
        "educationClean",
        "การศึกษา",
        "%",
        "แรงงานจบ ม.ปลาย",
        hard_max=100,
    ),
    metric("society_economy", "dependency", "พึ่งพิง", "เท่า", "อัตราส่วน"),
)

METRICS_BY_CATEGORY: dict[str, tuple[MetricDefinition, ...]] = {
    category_id: tuple(
        definition
        for definition in METRIC_CATALOG
        if definition.category_id == category_id
    )
    for category_id in CATEGORIES.values()
}
EXPECTED_KEYS = {
    category_id: {definition.source_key for definition in definitions}
    for category_id, definitions in METRICS_BY_CATEGORY.items()
}
VARIABLE_BY_CATEGORY = {
    category_id: variable_name for variable_name, category_id in CATEGORIES.items()
}


@dataclass(frozen=True)
class FetchResult:
    status: int
    html: str | None
    attempts: int


class ScrapeError(RuntimeError):
    """An expected scraper failure that should be recorded in SQLite."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> Any:
    """Normalize human-readable source text without changing non-text values."""

    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\u00a0", " ")
    return re.sub(r"[ \t\r\n]+", " ", normalized).strip()


def clean_dataset(raw_data: dict[str, Any]) -> dict[str, Any]:
    """Apply only lossless text cleanup to parsed source data."""

    cleaned = dict(raw_data)
    city_names = raw_data.get("cityNames")
    cleaned["cityNames"] = (
        [clean_text(name) for name in city_names]
        if isinstance(city_names, list)
        else city_names
    )
    for variable_name in CATEGORIES:
        category_data = raw_data.get(variable_name)
        cleaned[variable_name] = (
            {
                key: list(values) if isinstance(values, list) else values
                for key, values in category_data.items()
            }
            if isinstance(category_data, dict)
            else category_data
        )
    return cleaned


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f"{prefix}|{value}".encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def extract_js_literal(script: str, variable_name: str) -> str:
    """Extract one balanced array/object literal without executing JavaScript."""

    marker = re.search(rf"\b(?:const|let|var)\s+{re.escape(variable_name)}\s*=", script)
    if marker is None:
        raise ScrapeError(f"Missing JavaScript variable: {variable_name}")

    start = marker.end()
    while start < len(script) and script[start].isspace():
        start += 1
    if start >= len(script) or script[start] not in "[{":
        raise ScrapeError(f"{variable_name} does not start with an object or array")

    closing_for = {"[": "]", "{": "}"}
    stack = [closing_for[script[start]]]
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    position = start + 1

    while position < len(script):
        character = script[position]
        next_character = script[position + 1] if position + 1 < len(script) else ""

        if line_comment:
            if character in "\r\n":
                line_comment = False
            position += 1
            continue
        if block_comment:
            if character == "*" and next_character == "/":
                block_comment = False
                position += 2
            else:
                position += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            position += 1
            continue
        if character == "/" and next_character == "/":
            line_comment = True
            position += 2
            continue
        if character == "/" and next_character == "*":
            block_comment = True
            position += 2
            continue
        if character in "'\"`":
            quote = character
        elif character in closing_for:
            stack.append(closing_for[character])
        elif character in "]}":
            if not stack or character != stack[-1]:
                raise ScrapeError(
                    f"Mismatched JavaScript delimiters in {variable_name}"
                )
            stack.pop()
            if not stack:
                return script[start : position + 1]
        position += 1

    raise ScrapeError(f"Unterminated JavaScript literal: {variable_name}")


def find_data_script(page_html: str) -> str:
    """Find the inline script containing the published dashboard arrays."""

    script_pattern = re.compile(
        r"<script(?:\s[^>]*)?>(.*?)</script>", re.IGNORECASE | re.DOTALL
    )
    for match in script_pattern.finditer(page_html):
        script = html_module.unescape(match.group(1))
        if "cityNames" in script and "geoData" in script and "infData" in script:
            return script
    raise ScrapeError("Could not find the inline dashboard data script")


def parse_raw_data(page_html: str) -> dict[str, Any]:
    script = find_data_script(page_html)
    return {
        variable_name: chompjs.parse_js_object(
            extract_js_literal(script, variable_name)
        )
        for variable_name in DATA_VARIABLES
    }


def validate_dataset(
    raw_data: dict[str, Any], previous_data: dict[str, Any] | None = None
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) without changing source values."""

    errors: list[str] = []
    warnings: list[str] = []

    city_names = raw_data.get("cityNames")
    if not isinstance(city_names, list) or not city_names:
        return ["cityNames must be a non-empty array"], warnings

    normalized_names = [clean_text(name) for name in city_names]
    if any(not isinstance(name, str) or not name for name in normalized_names):
        errors.append("cityNames contains an empty or non-text value")
    valid_names = [name for name in normalized_names if isinstance(name, str)]
    if len(valid_names) == len(normalized_names) and len(set(valid_names)) != len(
        valid_names
    ):
        errors.append("cityNames contains duplicate names after normalization")

    if previous_data is not None and len(valid_names) == len(normalized_names):
        previous_names = set(previous_data.get("cityNames", []))
        current_names = set(valid_names)
        added = sorted(current_names - previous_names)
        removed = sorted(previous_names - current_names)
        if added:
            warnings.append(f"new cities detected: {', '.join(added)}")
        if removed:
            warnings.append(f"cities no longer published: {', '.join(removed)}")

    for variable_name, category_id in CATEGORIES.items():
        category_data = raw_data.get(variable_name)
        if not isinstance(category_data, dict):
            errors.append(f"{variable_name} must be an object")
            continue

        expected_keys = EXPECTED_KEYS[category_id]
        actual_keys = set(category_data)
        missing_keys = sorted(expected_keys - actual_keys)
        extra_keys = sorted(actual_keys - expected_keys)
        if missing_keys:
            errors.append(f"{variable_name} missing metrics: {', '.join(missing_keys)}")
        if extra_keys:
            errors.append(
                f"{variable_name} has uncatalogued metrics: {', '.join(extra_keys)}"
            )

        for definition in METRICS_BY_CATEGORY[category_id]:
            values = category_data.get(definition.source_key)
            if not isinstance(values, list):
                errors.append(
                    f"{variable_name}.{definition.source_key} must be an array"
                )
                continue
            if len(values) != len(normalized_names):
                errors.append(
                    f"{variable_name}.{definition.source_key} has {len(values)} values; "
                    f"expected {len(normalized_names)}"
                )
                continue

            for index, value in enumerate(values):
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(
                        f"{definition.metric_id}[{index}] must be a number or null"
                    )
                    continue
                if not math.isfinite(value):
                    errors.append(f"{definition.metric_id}[{index}] is not finite")
                    continue
                if definition.hard_min is not None and value < definition.hard_min:
                    errors.append(
                        f"{definition.metric_id}[{index}]={value} is below "
                        f"hard minimum {definition.hard_min}"
                    )
                if definition.hard_max is not None and value > definition.hard_max:
                    errors.append(
                        f"{definition.metric_id}[{index}]={value} is above "
                        f"hard maximum {definition.hard_max}"
                    )
                if (
                    definition.warning_min is not None
                    and value < definition.warning_min
                ):
                    warnings.append(
                        f"{definition.metric_id}[{index}]={value} is below soft range"
                    )
                if (
                    definition.warning_max is not None
                    and value > definition.warning_max
                ):
                    warnings.append(
                        f"{definition.metric_id}[{index}]={value} is above soft range"
                    )

    return errors, warnings


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def fetch_page(
    url: str,
    *,
    timeout: float,
    user_agent: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> FetchResult:
    """Fetch the public page with conservative retries."""

    for attempt in range(1, max_attempts + 1):
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Cache-Control": "no-cache",
            "User-Agent": user_agent,
        }
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return FetchResult(
                    status=response.status,
                    html=body.decode(charset),
                    attempts=attempt,
                )
        except HTTPError as error:
            if error.code in RETRYABLE_HTTP_CODES and attempt < max_attempts:
                delay = parse_retry_after(error.headers.get("Retry-After"))
                time.sleep(delay if delay is not None else 2 ** (attempt - 1))
                continue
            raise ScrapeError(f"HTTP {error.code} while fetching {url}") from error
        except URLError as error:
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            raise ScrapeError(
                f"Network error while fetching {url}: {error.reason}"
            ) from error
        except TimeoutError as error:
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            raise ScrapeError(f"Timeout while fetching {url}") from error

    raise ScrapeError(f"Could not fetch {url}")


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            http_status INTEGER,
            source_hash TEXT,
            outcome TEXT NOT NULL,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            raw_html_path TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            source_hash TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            parser_version TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS observations (
            city_id TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            value_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            active INTEGER NOT NULL CHECK (active IN (0, 1)),
            PRIMARY KEY (city_id, metric_id)
        );

        CREATE TABLE IF NOT EXISTS observation_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_id TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            value_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            event TEXT NOT NULL,
            source_hash TEXT NOT NULL
        );
        """
    )
    connection.commit()
    return connection


def successful_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    placeholders = ",".join("?" for _ in SUCCESS_OUTCOMES)
    return connection.execute(
        f"SELECT * FROM runs WHERE outcome IN ({placeholders}) "
        "ORDER BY run_id DESC LIMIT 1",
        SUCCESS_OUTCOMES,
    ).fetchone()


def load_snapshot(
    connection: sqlite3.Connection, source_hash: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT data_json FROM snapshots WHERE source_hash = ?", (source_hash,)
    ).fetchone()
    return json.loads(row["data_json"]) if row else None


def save_snapshot(
    connection: sqlite3.Connection,
    source_hash: str,
    data: dict[str, Any],
    observed_at: str,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO snapshots(source_hash,data_json,observed_at,parser_version) "
        "VALUES (?,?,?,?)",
        (source_hash, canonical_json(data), observed_at, PARSER_VERSION),
    )


def value_json(value: Any) -> str:
    return canonical_json(value)


def apply_observations(
    connection: sqlite3.Connection,
    data: dict[str, Any],
    source_hash: str,
    observed_at: str,
) -> list[str]:
    """Upsert current observations and retire records absent from a full scrape."""

    city_names = data["cityNames"]
    current_keys: set[tuple[str, str]] = set()
    warnings: list[str] = []

    for city_index, city_name in enumerate(city_names):
        city_id = stable_id("city", city_name)
        for definition in METRIC_CATALOG:
            values = data[VARIABLE_BY_CATEGORY[definition.category_id]][
                definition.source_key
            ]
            value = values[city_index]
            metric_id = definition.metric_id
            current_keys.add((city_id, metric_id))
            serialized = value_json(value)
            new_hash = content_hash(value)
            existing = connection.execute(
                "SELECT * FROM observations WHERE city_id=? AND metric_id=?",
                (city_id, metric_id),
            ).fetchone()

            if existing is None:
                connection.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?,1)",
                    (
                        city_id,
                        metric_id,
                        serialized,
                        new_hash,
                        observed_at,
                        observed_at,
                        observed_at,
                    ),
                )
                event = "inserted"
            elif existing["content_hash"] != new_hash or not existing["active"]:
                connection.execute(
                    "UPDATE observations SET value_json=?, content_hash=?, last_seen_at=?, "
                    "last_changed_at=?, active=1 WHERE city_id=? AND metric_id=?",
                    (
                        serialized,
                        new_hash,
                        observed_at,
                        observed_at,
                        city_id,
                        metric_id,
                    ),
                )
                event = "updated"
                if existing["value_json"] != "null" and serialized == "null":
                    warnings.append(f"{city_name} {metric_id} changed to null")
            else:
                connection.execute(
                    "UPDATE observations SET last_seen_at=?, active=1 "
                    "WHERE city_id=? AND metric_id=?",
                    (observed_at, city_id, metric_id),
                )
                event = None

            if event is not None:
                connection.execute(
                    "INSERT INTO observation_versions "
                    "(city_id,metric_id,value_json,content_hash,observed_at,event,source_hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        city_id,
                        metric_id,
                        serialized,
                        new_hash,
                        observed_at,
                        event,
                        source_hash,
                    ),
                )

    active_rows = connection.execute(
        "SELECT city_id, metric_id, value_json, content_hash FROM observations WHERE active=1"
    ).fetchall()
    for row in active_rows:
        key = (row["city_id"], row["metric_id"])
        if key in current_keys:
            continue
        connection.execute(
            "UPDATE observations SET active=0, last_seen_at=? WHERE city_id=? AND metric_id=?",
            (observed_at, row["city_id"], row["metric_id"]),
        )
        connection.execute(
            "INSERT INTO observation_versions "
            "(city_id,metric_id,value_json,content_hash,observed_at,event,source_hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                row["city_id"],
                row["metric_id"],
                row["value_json"],
                row["content_hash"],
                observed_at,
                "retired",
                source_hash,
            ),
        )

    return warnings


def metric_output(definition: MetricDefinition) -> dict[str, Any]:
    return {
        "metric_id": definition.metric_id,
        "category_id": definition.category_id,
        "source_key": definition.source_key,
        "label_th": definition.label_th,
        "display_unit": definition.display_unit,
        "description_th": definition.description_th,
        "value_type": "number",
        "nullable": True,
        "hard_min": definition.hard_min,
        "hard_max": definition.hard_max,
    }


def build_export(
    data: dict[str, Any],
    *,
    source_hash: str,
    run_id: int,
    data_observed_at: str,
    last_checked_at: str,
    warnings: list[str],
) -> dict[str, Any]:
    city_names = data["cityNames"]
    cities = [
        {
            "city_id": stable_id("city", city_name),
            "name_th": city_name,
            "source_index": index,
        }
        for index, city_name in enumerate(city_names)
    ]
    observations: list[dict[str, Any]] = []
    for city_index, city_name in enumerate(city_names):
        city_id = stable_id("city", city_name)
        for definition in METRIC_CATALOG:
            value = data[VARIABLE_BY_CATEGORY[definition.category_id]][
                definition.source_key
            ][city_index]
            observations.append(
                {
                    "city_id": city_id,
                    "metric_id": definition.metric_id,
                    "value": value,
                }
            )
    return {
        "schema_version": 1,
        "parser_version": PARSER_VERSION,
        "run_id": run_id,
        "source": {
            "url": SOURCE_URL,
            "snapshot_id": f"sha256:{source_hash}",
            "data_observed_at": data_observed_at,
            "last_checked_at": last_checked_at,
        },
        "cities": cities,
        "metrics": [metric_output(definition) for definition in METRIC_CATALOG],
        "observations": observations,
        "warnings": warnings,
    }


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, indent=2, allow_nan=False)
            temporary.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def save_raw_html(output_dir: Path, run_id: int, page_html: str) -> Path:
    path = output_dir / "raw_html" / f"run-{run_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page_html, encoding="utf-8")
    return path


def update_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    http_status: int | None = None,
    source_hash: str | None = None,
    outcome: str,
    warnings: list[str] | None = None,
    error: str | None = None,
    raw_html_path: Path | None = None,
) -> None:
    connection.execute(
        "UPDATE runs SET finished_at=?, http_status=?, source_hash=?, "
        "outcome=?, warnings_json=?, error=?, raw_html_path=? WHERE run_id=?",
        (
            finished_at,
            http_status,
            source_hash,
            outcome,
            json.dumps(warnings or [], ensure_ascii=False),
            error,
            str(raw_html_path) if raw_html_path else None,
            run_id,
        ),
    )
    connection.commit()


def run_pipeline(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    state_path: Path = DEFAULT_STATE_PATH,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    keep_html: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Run one safe, incremental collection and return its summary."""

    output_dir.mkdir(parents=True, exist_ok=True)
    connection = open_database(state_path)
    started_at = now()
    run_cursor = connection.execute(
        "INSERT INTO runs(started_at,outcome,warnings_json) VALUES (?,?,?)",
        (started_at, "running", "[]"),
    )
    run_id = run_cursor.lastrowid
    if run_id is None:
        raise ScrapeError("SQLite did not return a run ID")
    connection.commit()

    previous = successful_run(connection)
    previous_data = (
        load_snapshot(connection, previous["source_hash"])
        if previous is not None and previous["source_hash"]
        else None
    )
    raw_html_path: Path | None = None

    try:
        fetched = fetch_page(
            SOURCE_URL,
            timeout=timeout,
            user_agent=user_agent,
        )
        if fetched.html is None:
            raise ScrapeError(f"Unexpected empty response with HTTP {fetched.status}")
        if keep_html:
            raw_html_path = save_raw_html(output_dir, run_id, fetched.html)

        parsed_data = clean_dataset(parse_raw_data(fetched.html))
        errors, warnings = validate_dataset(parsed_data, previous_data)
        if errors:
            raise ScrapeError("Source validation failed: " + "; ".join(errors))
        source_hash = content_hash(parsed_data)
        data_observed_at = now()

        if (
            previous is not None
            and previous_data is not None
            and previous["source_hash"] == source_hash
        ):
            save_snapshot(connection, source_hash, parsed_data, data_observed_at)
            warnings.append(
                "Parsed source data is unchanged; no observation versions created"
            )
            outcome = "unchanged_data"
        else:
            save_snapshot(connection, source_hash, parsed_data, data_observed_at)
            with connection:
                warnings.extend(
                    apply_observations(
                        connection, parsed_data, source_hash, data_observed_at
                    )
                )
            outcome = "success"

        export = build_export(
            parsed_data,
            source_hash=source_hash,
            run_id=run_id,
            data_observed_at=data_observed_at,
            last_checked_at=now(),
            warnings=warnings,
        )
        write_json_atomic(output_dir / "latest.json", export)
        update_run(
            connection,
            run_id,
            finished_at=now(),
            http_status=fetched.status,
            source_hash=source_hash,
            outcome=outcome,
            warnings=warnings,
            raw_html_path=raw_html_path,
        )
        return {
            "run_id": run_id,
            "outcome": outcome,
            "http_status": fetched.status,
            "city_count": len(parsed_data["cityNames"]),
            "observation_count": len(parsed_data["cityNames"]) * len(METRIC_CATALOG),
            "source_hash": source_hash,
            "warnings": warnings,
        }
    except Exception as error:
        update_run(
            connection,
            run_id,
            finished_at=now(),
            outcome="failed",
            error=str(error),
            warnings=[],
            raw_html_path=raw_html_path,
        )
        raise
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Incrementally collect Evaluate the City source observations."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--save-html",
        action="store_true",
        help="Keep the downloaded HTML under output/raw_html.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run built-in parser and validation tests.",
    )
    return parser


class ScraperTests(unittest.TestCase):
    def test_extract_balanced_literal_ignores_braces_in_strings_and_comments(
        self,
    ) -> None:
        script = "const data = {text: '} ]', nested: [1, /* ] */ 2]}; const after = 1;"
        literal_text = extract_js_literal(script, "data")
        self.assertEqual(
            chompjs.parse_js_object(literal_text), {"text": "} ]", "nested": [1, 2]}
        )

    def test_clean_text_normalizes_unicode_and_whitespace(self) -> None:
        self.assertEqual(clean_text("  เทศบาล\u00a0นคร\nลำปาง  "), "เทศบาล นคร ลำปาง")

    def test_validation_preserves_null_and_rejects_misalignment(self) -> None:
        data = {
            "cityNames": ["A", "B"],
            "geoData": {
                definition.source_key: [None, 1]
                for definition in METRICS_BY_CATEGORY["environment"]
            },
            "infData": {
                definition.source_key: [1, 1]
                for definition in METRICS_BY_CATEGORY["infrastructure"]
            },
            "pplData": {
                definition.source_key: [1, 1]
                for definition in METRICS_BY_CATEGORY["society_economy"]
            },
        }
        data["geoData"]["pm25"] = [None]
        errors, _ = validate_dataset(data)
        self.assertTrue(any("pm25 has 1 values" in error for error in errors))
        self.assertIsNone(data["geoData"]["pm25"][0])

    def test_validation_rejects_invalid_value_types(self) -> None:
        data = {
            "cityNames": ["A", "B"],
            "geoData": {
                definition.source_key: [1, 1]
                for definition in METRICS_BY_CATEGORY["environment"]
            },
            "infData": {
                definition.source_key: [1, 1]
                for definition in METRICS_BY_CATEGORY["infrastructure"]
            },
            "pplData": {
                definition.source_key: [1, 1]
                for definition in METRICS_BY_CATEGORY["society_economy"]
            },
        }
        data["infData"]["electricity"] = ["99.9", 100]
        errors, _ = validate_dataset(data)
        self.assertTrue(
            any("infrastructure.electricity[0]" in error for error in errors)
        )

    def test_stable_id_is_repeatable(self) -> None:
        self.assertEqual(stable_id("city", "Lampang"), stable_id("city", "Lampang"))
        self.assertNotEqual(stable_id("city", "Lampang"), stable_id("city", "Lamphun"))

    def test_catalog_has_expected_shape(self) -> None:
        self.assertEqual(len(METRIC_CATALOG), 39)
        self.assertEqual(len(METRICS_BY_CATEGORY["environment"]), 7)
        self.assertEqual(len(METRICS_BY_CATEGORY["infrastructure"]), 15)
        self.assertEqual(len(METRICS_BY_CATEGORY["society_economy"]), 17)


def run_self_tests() -> None:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ScraperTests)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        run_self_tests()
        return 0
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    try:
        summary = run_pipeline(
            output_dir=args.output_dir,
            state_path=args.state_path,
            timeout=args.timeout,
            keep_html=args.save_html,
            user_agent=args.user_agent,
        )
    except (ScrapeError, OSError, ValueError, sqlite3.Error) as error:
        print(f"Scrape failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Run {summary['run_id']} {summary['outcome']}: "
        f"{summary['city_count']} cities, "
        f"{summary['observation_count']} observations"
    )
    print(f"Source hash: {summary['source_hash']}")
    if summary.get("warnings"):
        print(f"Warnings: {len(summary['warnings'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
