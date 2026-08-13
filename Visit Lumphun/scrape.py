"""Incremental public-content scraper for Visit Lamphun.

Visit Lamphun is a React single-page app; its public Vite bundle contains the
static content shown on the five pages. Tree-sitter parses that bundle into an
AST, so the scraper can read published literals without executing remote code.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import tree_sitter_javascript  # pyright: ignore[reportMissingImports]
from tree_sitter import Language, Node, Parser  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state" / "visit_lamphun.sqlite3"
OUTPUT_DIR = ROOT / "output"
BASE_URL = "https://visit-lamphun.web.app"
PAGES = {
    "homepage": "/app/homepage",
    "recommend": "/app/recommend",
    "travel": "/app/travel",
    "komepage": "/app/komepage",
    "contact": "/app/contact",
}
LANGUAGES = ("TH", "EN", "CN")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch(url: str) -> str:
    request = Request(url, headers={"User-Agent": "VisitLamphunPublicScraper/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def clean_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", value).strip()


def clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    return clean_text(value)


def absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    value = clean_text(value)
    if not isinstance(value, str):
        return None
    if value.startswith(("https://", "http://")):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return None


def normalized_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits or None


def normalize_time(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)\b", value)
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def multilingual(values: dict[str, Any], field: str) -> dict[str, str | None]:
    return {
        language: clean_text(values.get(language, {}).get(field)) or None
        for language in LANGUAGES
    }


def stable_id(*parts: Any) -> str:
    text = "|".join(clean_text(str(part or "")) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def literal(bundle: BundleData, name: str) -> Any:
    return bundle.literal(name)


class BundleData:
    """Read published JavaScript literal values from a Tree-sitter AST."""

    def __init__(self, source: str):
        self.source = source.encode("utf-8")
        language = Language(tree_sitter_javascript.language())
        self.tree = Parser(language).parse(self.source)
        self.assignments: dict[str, Node] = {}
        self._index_assignments()

    def _index_assignments(self) -> None:
        pending = [self.tree.root_node]
        while pending:
            node = pending.pop()
            if node.type == "variable_declarator":
                name = node.child_by_field_name("name")
                value = node.child_by_field_name("value")
            elif node.type == "assignment_expression":
                name = node.child_by_field_name("left")
                value = node.child_by_field_name("right")
            else:
                name = value = None
            if name is not None and value is not None and name.type == "identifier":
                self.assignments[self.text(name)] = value
            pending.extend(reversed(node.children))

    def text(self, node: Node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8")

    def literal(self, name: str) -> Any:
        node = self.assignments.get(name)
        if node is None:
            raise ValueError(f"missing expected published data collection: {name}")
        return self.convert(node)

    def convert(self, node: Node) -> Any:
        if node.type == "object":
            result: dict[str, Any] = {}
            for pair in node.named_children:
                if pair.type != "pair":
                    raise ValueError(f"unsupported object member: {pair.type}")
                key = pair.child_by_field_name("key")
                value = pair.child_by_field_name("value")
                if key is None or value is None:
                    raise ValueError("object pair is missing a key or value")
                result[self.property_key(key)] = self.convert(value)
            return result
        if node.type == "array":
            return [self.convert(child) for child in node.named_children]
        if node.type == "string":
            return self.decode_string(self.text(node))
        if node.type == "template_string":
            if any(
                child.type == "template_substitution" for child in node.named_children
            ):
                raise ValueError(
                    "template strings with substitutions are not supported"
                )
            return self.text(node)[1:-1]
        if node.type == "number":
            raw = self.text(node)
            return (
                float(raw)
                if any(marker in raw for marker in (".", "e", "E"))
                else int(raw)
            )
        if node.type == "true":
            return True
        if node.type == "false":
            return False
        if node.type == "null":
            return None
        if node.type == "unary_expression":
            operator = self.text(node.children[0])
            argument = node.child_by_field_name("argument")
            if argument is None:
                raise ValueError("unary expression is missing its argument")
            value = self.convert(argument)
            if operator == "!":
                return not value
            if operator == "-":
                return -value
        if node.type == "parenthesized_expression":
            return self.convert(node.named_children[0])
        raise ValueError(f"unsupported JavaScript value: {node.type}")

    def property_key(self, node: Node) -> str:
        if node.type == "string":
            return self.decode_string(self.text(node))
        return self.text(node)

    @staticmethod
    def decode_string(raw: str) -> str:
        """Decode a JavaScript single- or double-quoted literal without evaluation."""
        try:
            return ast.literal_eval(raw)
        except (SyntaxError, ValueError) as error:
            raise ValueError(
                f"could not decode JavaScript string literal: {raw[:80]!r}"
            ) from error


def fare(raw: str | None) -> dict[str, Any]:
    raw_text = clean_text(raw)
    if not isinstance(raw_text, str):
        raw_text = ""
    number = re.search(r"\d+(?:\.\d+)?", raw_text)
    return {
        "amount": float(number.group()) if number else None,
        "currency": "THB" if ("บาท" in raw_text or "THB" in raw_text) else None,
        "raw": raw_text or None,
    }


def extract_homepage(bundle: BundleData, warnings: list[str]) -> dict[str, Any]:
    details = literal(bundle, "hl")
    station_pairs = [
        ("hp", "Xm"),
        ("lpts", "Zm"),
        ("mc", "Jm"),
        ("jts", "eg"),
        ("kt", "tg"),
        ("jtt", "ng"),
        ("mt", "ig"),
        ("prt", "rg"),
        ("syl", "ag"),
        ("cm", "sg"),
        ("lgts", "og"),
        ("lmls", "lg"),
    ]
    stations = []
    for station_id, venue_variable in station_pairs:
        station_data = {
            language: details[language][station_id] for language in LANGUAGES
        }
        venues = []
        for index, venue in enumerate(literal(bundle, venue_variable)):
            venue = clean_value(venue)
            link = absolute_url(venue.get("locationLink"))
            names = venue.get("locationName", {})
            venues.append(
                {
                    "venue_id": stable_id(
                        "homepage", station_id, link or names.get("TH"), index
                    ),
                    "name": {
                        lang: clean_text(names.get(lang)) or None for lang in LANGUAGES
                    },
                    "image_url": absolute_url(venue.get("locationImg")),
                    "google_maps_url": link,
                    "source_status": venue.get("status"),
                }
            )
        stations.append(
            {
                "station_id": station_id,
                "name": multilingual(station_data, "name"),
                "description": multilingual(station_data, "description"),
                "image_url": absolute_url(station_data["TH"].get("img")),
                "venues": venues,
            }
        )
    return {"map": {"stations": stations}}


def cards(
    bundle: BundleData, variable: str, category_id: str, labels: dict[str, str]
) -> dict[str, Any]:
    values = literal(bundle, variable)
    items = []
    for index, english in enumerate(values["EN"]):
        translations = {language: values[language][index] for language in LANGUAGES}
        items.append(
            {
                "item_id": stable_id(
                    "recommend",
                    category_id,
                    translations["TH"].get("title"),
                    translations["TH"].get("description"),
                ),
                "title": multilingual(translations, "title"),
                "description": multilingual(translations, "description"),
                "image_url": absolute_url(english.get("image")),
                "source_status": english.get("status"),
            }
        )
    return {"category_id": category_id, "label": labels, "items": items}


def extract_recommend(bundle: BundleData, warnings: list[str]) -> dict[str, Any]:
    return {
        "categories": [
            cards(
                bundle,
                "kM",
                "goods",
                {"TH": "ของดี", "EN": "Best of the best", "CN": "精品"},
            ),
            cards(
                bundle,
                "OM",
                "notable",
                {"TH": "ที่เด่น", "EN": "Outstanding", "CN": "著名景点"},
            ),
            cards(bundle, "MM", "famous", {"TH": "คนดัง", "EN": "Famous", "CN": "名人"}),
        ]
    }


def extract_travel(bundle: BundleData, warnings: list[str]) -> dict[str, Any]:
    def train(collection: str, days: list[str]) -> list[dict[str, Any]]:
        values = literal(bundle, collection)
        result = []
        for index, item in enumerate(values["TH"]):
            result.append(
                {
                    "service_id": stable_id(
                        "train",
                        days,
                        item.get("origiinTime"),
                        item.get("destinationTime"),
                        index,
                    ),
                    "service_days": days,
                    "origin": {
                        "name": multilingual(
                            {l: values[l][index] for l in LANGUAGES}, "originProvince"
                        ),
                        "station": multilingual(
                            {l: values[l][index] for l in LANGUAGES}, "originStation"
                        ),
                    },
                    "destination": {
                        "name": multilingual(
                            {l: values[l][index] for l in LANGUAGES},
                            "destinationProvince",
                        ),
                        "station": multilingual(
                            {l: values[l][index] for l in LANGUAGES},
                            "destinationStation",
                        ),
                    },
                    "departure_time": normalize_time(item.get("origiinTime")),
                    "arrival_time": normalize_time(item.get("destinationTime")),
                    "fare": fare(str(item.get("price", "")) + " THB"),
                    "description": multilingual(
                        {l: values[l][index] for l in LANGUAGES}, "detail"
                    ),
                    "detail_url": None,
                }
            )
        return result

    tram_values = literal(bundle, "PM")
    tram = []
    for index, item in enumerate(tram_values["TH"]):
        translations = {lang: tram_values[lang][index] for lang in LANGUAGES}
        tram.append(
            {
                "service_id": stable_id("tram", item.get("time"), index),
                "route_name": multilingual(translations, "locationName"),
                "departure_time": normalize_time(item.get("time")),
                "fare": fare(item.get("price")),
                "detail_url": None,
            }
        )
    other_values = literal(bundle, "UM")
    other = []
    for index, item in enumerate(other_values["TH"]):
        translations = {lang: other_values[lang][index] for lang in LANGUAGES}
        other.append(
            {
                "service_id": stable_id(
                    "other", item.get("type"), item.get("title"), index
                ),
                "type": item.get("type"),
                "name": multilingual(translations, "title"),
                "location": multilingual(translations, "location"),
                "phone_display": clean_text(item.get("tel")) or None,
                "phone_normalized": normalized_phone(item.get("tel")),
                "operator": multilingual(translations, "company"),
                "detail_url": None,
            }
        )
    return {
        "train": {
            "service_days": [
                {
                    "label": {"TH": "วันธรรมดา", "EN": "Weekdays", "CN": "工作日"},
                    "days": ["MON", "TUE", "WED", "THU", "FRI"],
                },
                {
                    "label": {"TH": "วันหยุด", "EN": "Weekends", "CN": "周末"},
                    "days": ["SAT", "SUN"],
                },
            ],
            "services": train("LM", ["MON", "TUE", "WED", "THU", "FRI"])
            + train("jM", ["SAT", "SUN"]),
        },
        "tourism_tram": {
            "operating_days": ["TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            "closed_days": ["MON"],
            "services": tram,
        },
        "other_transport": other,
    }


def extract_komepage(bundle: BundleData, warnings: list[str]) -> dict[str, Any]:
    # `FM` is the published multilingual lantern-group collection. The code
    # validates its record shape below, so a bundle change fails safely rather
    # than silently collecting a different array.
    values = literal(bundle, "FM")
    groups = []
    for index, item in enumerate(values["TH"]):
        name, phone = clean_text(item.get("groupName")), clean_text(item.get("tel"))
        translations = {lang: values[lang][index] for lang in LANGUAGES}
        groups.append(
            {
                "group_id": stable_id("kome", name, phone),
                "name": {
                    lang: clean_text(translations[lang].get("groupName")) or None
                    for lang in LANGUAGES
                },
                "phone_display": phone or None,
                "phone_normalized": normalized_phone(phone),
            }
        )
    return {"lantern_production_groups": groups}


def extract_contact(bundle: BundleData, warnings: list[str]) -> dict[str, Any]:
    values = literal(bundle, "HM")
    emergency, contacts = [], []
    for index, group in enumerate(values["TH"]):
        translations = {lang: values[lang][index] for lang in LANGUAGES}
        entries = group.get("tel", [])
        if index == 0:
            for entry_index, entry in enumerate(entries):
                phone = clean_text(entry.get("tel"))
                emergency.append(
                    {
                        "service": {
                            lang: clean_text(
                                translations[lang]["tel"][entry_index].get("info")
                            )
                            or None
                            for lang in LANGUAGES
                        },
                        "phone_display": phone or None,
                        "phone_normalized": normalized_phone(phone),
                    }
                )
            continue
        phones = [
            {
                "label": {
                    "TH": clean_text(entry.get("info")) or None,
                    "EN": None,
                    "CN": None,
                },
                "phone_display": clean_text(entry.get("tel")) or None,
                "phone_normalized": normalized_phone(entry.get("tel")),
            }
            for entry in entries
            if entry.get("tel")
        ]
        address = next(
            (
                clean_text(entry.get("info"))
                for entry in entries
                if not entry.get("tel")
            ),
            None,
        )
        hours = next(
            (
                clean_text(entry.get("tel"))
                for entry in entries
                if "เปิดทำการ" in str(entry.get("info"))
            ),
            None,
        )
        contacts.append(
            {
                "contact_id": stable_id("contact", group.get("title")),
                "name": multilingual(translations, "title"),
                "address": {"TH": address, "EN": None, "CN": None},
                "opening_hours_raw": hours,
                "phones": phones,
            }
        )
    resources = [
        {
            "resource_id": "facebook",
            "category": "social",
            "name": {"TH": "Facebook", "EN": "Facebook", "CN": "Facebook"},
            "description": {"TH": None, "EN": None, "CN": None},
            "url": None,
            "image_url": None,
        },
        {
            "resource_id": "tourist-care",
            "category": "public_service",
            "name": {"TH": "Tourist Care", "EN": "Tourist Care", "CN": "Tourist Care"},
            "description": {
                "TH": "ใส่ใจนักท่องเที่ยวเว็บไซต์สำหรับนักท่องเที่ยว",
                "EN": "Public Service",
                "CN": None,
            },
            "url": None,
            "image_url": None,
        },
        {
            "resource_id": "line",
            "category": "line",
            "name": {"TH": "Line", "EN": "Line", "CN": "Line"},
            "description": {
                "TH": "รายงานเหตุผิดปกติทั้งทางด้าน คน สัตว์ สิ่งแวดล้อมและแจ้งเหตุงานบริการสาธารณะ",
                "EN": None,
                "CN": None,
            },
            "url": None,
            "image_url": None,
        },
    ]
    return {
        "emergency_numbers": emergency,
        "service_contacts": contacts,
        "resources": resources,
    }


EXTRACTORS = {
    "homepage": extract_homepage,
    "recommend": extract_recommend,
    "travel": extract_travel,
    "komepage": extract_komepage,
    "contact": extract_contact,
}


def setup_database(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, bundle_sha256 TEXT, outcome TEXT, warnings_json TEXT);
        CREATE TABLE IF NOT EXISTS records (record_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, section TEXT NOT NULL, payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_changed_at TEXT NOT NULL, active INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS record_versions (version_id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, observed_at TEXT NOT NULL, event TEXT NOT NULL, payload_json TEXT NOT NULL, content_hash TEXT NOT NULL);
    """)


def flatten(
    page_id: str, data: Any, section: str = "data"
) -> list[tuple[str, str, dict[str, Any]]]:
    records = []
    if isinstance(data, dict):
        identity = next(
            (
                data.get(key)
                for key in (
                    "station_id",
                    "venue_id",
                    "item_id",
                    "service_id",
                    "group_id",
                    "contact_id",
                    "resource_id",
                )
                if data.get(key)
            ),
            None,
        )
        if identity:
            records.append((str(identity), section, data))
            return records
        for key, value in data.items():
            records.extend(flatten(page_id, value, f"{section}.{key}"))
    elif isinstance(data, list):
        for item in data:
            records.extend(flatten(page_id, item, section))
    return records


def save_page(
    connection: sqlite3.Connection, page_id: str, data: dict[str, Any], timestamp: str
) -> None:
    current = {record_id for record_id, _, _ in flatten(page_id, data)}
    for record_id, section, payload in flatten(page_id, data):
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        row = connection.execute(
            "SELECT content_hash FROM records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            event = "inserted"
            connection.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,1)",
                (
                    record_id,
                    page_id,
                    section,
                    payload_json,
                    content_hash,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        elif row[0] != content_hash:
            event = "updated"
            connection.execute(
                "UPDATE records SET section=?,payload_json=?,content_hash=?,last_seen_at=?,last_changed_at=?,active=1 WHERE record_id=?",
                (section, payload_json, content_hash, timestamp, timestamp, record_id),
            )
        else:
            event = None
            connection.execute(
                "UPDATE records SET last_seen_at=?,active=1 WHERE record_id=?",
                (timestamp, record_id),
            )
        if event:
            connection.execute(
                "INSERT INTO record_versions(record_id,observed_at,event,payload_json,content_hash) VALUES (?,?,?,?,?)",
                (record_id, timestamp, event, payload_json, content_hash),
            )
    active_rows = connection.execute(
        "SELECT record_id,payload_json,content_hash FROM records WHERE page_id=? AND active=1",
        (page_id,),
    ).fetchall()
    for record_id, payload_json, content_hash in active_rows:
        if record_id not in current:
            connection.execute(
                "UPDATE records SET active=0,last_seen_at=? WHERE record_id=?",
                (timestamp, record_id),
            )
            connection.execute(
                "INSERT INTO record_versions(record_id,observed_at,event,payload_json,content_hash) VALUES (?,?,?,?,?)",
                (record_id, timestamp, "retired", payload_json, content_hash),
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force-static",
        action="store_true",
        help="Parse static content even if the bundle hash is unchanged.",
    )
    args = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Each route is a shell for the same SPA bundle. Fetch every requested
    # route anyway: this confirms the source pages are still publicly served.
    page_html = {
        page_id: fetch(urljoin(BASE_URL, path)) for page_id, path in PAGES.items()
    }
    script_path = re.search(
        r'<script[^>]+src="([^"]+index-[^"]+\.js)"', page_html["homepage"]
    )
    if not script_path:
        raise RuntimeError("could not locate Vite bundle")
    bundle_source = fetch(urljoin(BASE_URL, script_path.group(1)))
    bundle_hash, timestamp = hashlib.sha256(bundle_source.encode()).hexdigest(), now()
    connection = sqlite3.connect(STATE_PATH)
    setup_database(connection)
    run_id = connection.execute(
        "INSERT INTO runs(started_at,bundle_sha256,outcome,warnings_json) VALUES (?,?,?,?)",
        (timestamp, bundle_hash, "running", "[]"),
    ).lastrowid
    try:
        previous = connection.execute(
            "SELECT bundle_sha256 FROM runs WHERE outcome='success' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        static_is_current = (
            previous is not None
            and previous[0] == bundle_hash
            and not args.force_static
        )
        bundle_data = None if static_is_current else BundleData(bundle_source)
        for page_id, extractor in EXTRACTORS.items():
            warnings: list[str] = []
            existing_output = OUTPUT_DIR / f"{page_id}.json"
            if static_is_current and existing_output.exists():
                output = json.loads(existing_output.read_text(encoding="utf-8"))
                data = output["data"]
                warnings.append(
                    "Static bundle unchanged; reused the previous static extraction."
                )
            else:
                if bundle_data is None:
                    raise RuntimeError(
                        "static extraction requires a parsed JavaScript bundle"
                    )
                data = clean_value(extractor(bundle_data, warnings))
                save_page(connection, page_id, data, timestamp)
            output = {
                "schema_version": 1,
                "page_id": page_id,
                "source_url": urljoin(BASE_URL, PAGES[page_id]),
                "scraped_at": timestamp,
                "bundle_sha256": bundle_hash,
                "warnings": warnings,
                "data": data,
            }
            (OUTPUT_DIR / f"{page_id}.json").write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        connection.execute(
            "UPDATE runs SET finished_at=?,outcome=? WHERE run_id=?",
            (now(), "success", run_id),
        )
        connection.commit()
        print(
            f"Saved five page outputs to {OUTPUT_DIR} using bundle {bundle_hash[:12]}."
        )
    except Exception as error:
        connection.execute(
            "UPDATE runs SET finished_at=?,outcome=?,warnings_json=? WHERE run_id=?",
            (now(), "failed", json.dumps([str(error)]), run_id),
        )
        connection.commit()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
