#!/usr/bin/env python3
"""Incremental scraper for the public LeSuper PMUA area-based listing.

One GET returns the whole dataset: the business listings, the source's own
summary statistics and its filter values. The script exports each listing row
exactly as the source sent it, wrapped with a stable key, and compares each
run with the previous export in the output directory, which is the only state
kept.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
SOURCE_URL = "https://lesuper.app/api/opendata/pmua/area-based"
DEFAULT_USER_AGENT = "LearningAreaBasedCollector"
DEFAULT_TIMEOUT = 90
STATS_KEY = "area_based_stats"
Reporter = Callable[[str], None]

DATASETS = ("area_based", "area_based_stats")
OUTPUT_NAMES = {"area_based": "area_based.json", "area_based_stats": "area_based_stats.json"}
# A run with fewer rows than this is treated as a broken response, not as news.
MINIMUM_RECORDS = {"area_based": 500, "area_based_stats": 1}
ROW_FIELDS = (
    "id", "fiscalYear", "projectName", "researchUnit", "region", "province", "district",
    "subDistrict", "businessName", "sequence", "createdAt", "updatedAt",
)


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
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def fetch_payload(session: requests.Session, timeout: int) -> dict[str, Any]:
    response = session.get(SOURCE_URL, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    return payload


def semantic_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_listing_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Wrap each source row without changing any of its values.

    Blank and placeholder-looking values (empty province, "[object Object]"
    business names) are source facts and stay as sent.
    """
    if payload.get("status") != 200:
        raise ValueError(f"source status is {payload.get('status')!r}, expected 200")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("'data' is not a list")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    new_fields: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"row {index} is not an object")
        source_id = row.get("id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError(f"row {index} has no usable id")
        if source_id in seen:
            raise ValueError(f"duplicate id {source_id}")
        seen.add(source_id)
        new_fields |= set(row) - set(ROW_FIELDS)
        records.append(
            {
                **row,
                "record_type": "area_based_listing",
                "source_key": source_id,
                "source_payload_hash": semantic_hash(row),
            }
        )
    warnings: list[str] = []
    if new_fields:
        warnings.append(f"rows carry fields not seen before: {', '.join(sorted(new_fields))}")
    stats = payload.get("stats")
    if isinstance(stats, dict) and stats.get("totalRecords") != len(records):
        warnings.append(f"stats.totalRecords is {stats.get('totalRecords')!r} but the source sent {len(records)} rows")
    return records, warnings


def build_stats_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stats = payload.get("stats")
    filters = payload.get("filters")
    if not isinstance(stats, dict) or not stats:
        raise ValueError("'stats' is missing or not an object")
    if not isinstance(filters, dict):
        raise ValueError("'filters' is missing or not an object")
    body = {"stats": stats, "filters": filters}
    return [
        {
            **body,
            "record_type": "area_based_stats",
            "source_key": STATS_KEY,
            "source_payload_hash": semantic_hash(body),
        }
    ]


def validate_records(dataset: str, records: list[dict[str, Any]]) -> None:
    if len(records) < MINIMUM_RECORDS[dataset]:
        raise ValueError(f"{dataset}: found {len(records)} records, below safety floor")
    seen: set[str] = set()
    for record in records:
        key = record.get("source_key")
        if not isinstance(key, str) or not key.strip():
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
    validate_records(dataset, records)
    previous = load_previous_records(dataset, previous_path)
    incoming = {record["source_key"]: record for record in records}
    removed = set(previous) - set(incoming)
    if previous and removed and not allow_large_removal:
        threshold = max(10, math.ceil(len(previous) * 0.5))
        if len(removed) >= threshold:
            raise ValueError(f"{dataset}: refusing to retire {len(removed)} of {len(previous)} records")
    result = {"new": 0, "changed": 0, "unchanged": 0, "retired": len(removed)}
    for key, record in incoming.items():
        if key not in previous:
            result["new"] += 1
        elif semantic_hash(previous[key]) != semantic_hash(record):
            result["changed"] += 1
        else:
            result["unchanged"] += 1
    return result


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
    output_dir: Path, dataset: str, records: list[dict[str, Any]], warnings: list[str], scraped_at: str, run_id: str
) -> None:
    """Write one export: records in source_key order, keys sorted recursively."""
    exported = [
        json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for record in sorted(records, key=lambda record: record["source_key"])
    ]
    write_json(
        output_dir / OUTPUT_NAMES[dataset],
        {
            "schema_version": 1,
            "dataset": dataset,
            "source_urls": [SOURCE_URL],
            "scraped_at": scraped_at,
            "run_id": run_id,
            "record_count": len(exported),
            "warnings": warnings,
            "data": exported,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", action="append", choices=DATASETS, help="Export only this dataset. Repeatable.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--allow-large-removal", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    selected = tuple(args.dataset or DATASETS)
    reporter = report_factory(args.quiet)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary: dict[str, Any] = {"run_id": run_id, "datasets": {}, "failures": []}
    try:
        reporter(f"Fetching {SOURCE_URL}")
        payload = fetch_payload(create_session(args.user_agent), args.timeout)
    except Exception as exc:  # Nothing was collected, so every selected dataset keeps its old export.
        summary["failures"].append(f"fetch: {exc}")
        for dataset in selected:
            summary["datasets"][dataset] = {"status": "failed", "error": f"fetch: {exc}"}
        print(f"fetch: {exc}", file=sys.stderr)
        summary["status"] = "failed"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1

    builders: dict[str, Callable[[], tuple[list[dict[str, Any]], list[str]]]] = {
        "area_based": lambda: build_listing_records(payload),
        "area_based_stats": lambda: (build_stats_records(payload), []),
    }
    for dataset in selected:
        try:
            records, warnings = builders[dataset]()
            stats = compare_with_previous(
                dataset, records, args.output_dir / OUTPUT_NAMES[dataset], allow_large_removal=args.allow_large_removal
            )
            warnings = [*warnings, f"sync: {stats}"]
            write_export(args.output_dir, dataset, records, warnings, utc_now(), run_id)
            summary["datasets"][dataset] = {"status": "success", "record_count": len(records), **stats, "warnings": warnings}
            reporter(f"{dataset}: {len(records)} records")
        except Exception as exc:  # Preserve the other dataset and the prior good export.
            summary["failures"].append(f"{dataset}: {exc}")
            summary["datasets"][dataset] = {"status": "failed", "error": str(exc)}
            print(f"{dataset}: {exc}", file=sys.stderr)
    summary["status"] = "failed" if summary["failures"] else "success"
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
