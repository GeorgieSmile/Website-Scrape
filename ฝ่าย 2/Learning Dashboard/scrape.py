#!/usr/bin/env python3
"""Incremental scraper for the public LeSuper PMUA dashboard.

One POST returns the whole dashboard: four business-count tables (province,
entity type, category, region), the monthly local-economic impact of six
regions, and two totals. The script exports every table row and every impact
object with the source's values unchanged, and compares each run with the
previous export in the output directory, which is the only state kept.
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
SOURCE_URL = "https://lesuper.app/api/opendata/pmua"
DEFAULT_USER_AGENT = "LearningDashboardCollector"
DEFAULT_TIMEOUT = 60
Reporter = Callable[[str], None]

DATASETS = ("dashboard_counts", "dashboard_impact", "dashboard_totals")
OUTPUT_NAMES = {name: f"{name}.json" for name in DATASETS}
# A run with fewer records than this is treated as a broken response, not as news.
MINIMUM_RECORDS = {"dashboard_counts": 50, "dashboard_impact": 6, "dashboard_totals": 1}

# Each table is a list whose first row is a display header, then [label, count] rows.
COUNT_DIMENSIONS = ("provinces", "entityTypes", "categories", "geography")
# geographyImpact has six objects with no region label. The page names them by
# position with this fixed list, so the scraper does the same.
REGION_ORDER = (
    "ภาคเหนือ", "ภาคกลาง", "ภาคตะวันออกเฉียงเหนือ", "ภาคตะวันตก", "ภาคตะวันออก", "ภาคใต้",
)
IMPACT_FIELDS = ("localEmployeeAmount", "localEmployeeExpense", "localResourceConsumption", "localResourceExpense")
# impactSummary key (spelling as the source sends it) -> the regional field it totals.
SUMMARY_FIELDS = {
    "totalEmplyeeAmount": "localEmployeeAmount",
    "totalEmployeeExpense": "localEmployeeExpense",
    "totalResourceConsumption": "localResourceConsumption",
    "totalResourceExpense": "localResourceExpense",
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
        allowed_methods=frozenset({"GET", "POST"}),  # The dashboard API only answers POST; the request is read-only.
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def fetch_payload(session: requests.Session, timeout: int) -> dict[str, Any]:
    """POST with no body, exactly as the dashboard page does."""
    response = session.post(SOURCE_URL, timeout=timeout)
    response.raise_for_status()
    # The source labels JSON as text/html or text/plain, so decode the body ourselves.
    payload = json.loads(response.content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    return payload


def semantic_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_count_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn the four header-plus-rows tables into one record per row.

    The header row is not a data row: its two texts are kept on every record of
    the table as dimension_label and count_label.
    """
    records: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    for dimension in COUNT_DIMENSIONS:
        table = payload.get(dimension)
        if not isinstance(table, list) or len(table) < 2:
            raise ValueError(f"{dimension}: table is missing or has no rows")
        header, *rows = table
        if not isinstance(header, list) or len(header) != 2 or not all(isinstance(text, str) for text in header):
            raise ValueError(f"{dimension}: first row is not a two-text header")
        seen: set[str] = set()
        total = 0
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or not row[0].strip():
                raise ValueError(f"{dimension}: malformed row {index}: {row!r}")
            if not is_number(row[1]):
                raise ValueError(f"{dimension}: count of {row[0]!r} is not a number: {row[1]!r}")
            if row[0] in seen:
                raise ValueError(f"{dimension}: duplicate label {row[0]!r}")
            seen.add(row[0])
            total += row[1]
            records.append(
                {
                    "record_type": "dashboard_count",
                    "source_key": f"{dimension}:{row[0]}",
                    "dimension": dimension,
                    "dimension_label": header[0],
                    "count_label": header[1],
                    "row_index": index,
                    "label": row[0],
                    "count": row[1],
                    "source_payload_hash": semantic_hash(row),
                }
            )
        totals[dimension] = total
    warnings: list[str] = []
    if len(set(totals.values())) > 1:
        warnings.append("count tables do not add up to the same total: " + ", ".join(f"{k}={v}" for k, v in totals.items()))
    return records, warnings


def build_impact_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Label the six impact objects by position, as the page does."""
    impacts = payload.get("geographyImpact")
    if not isinstance(impacts, list) or len(impacts) != len(REGION_ORDER):
        raise ValueError(f"geographyImpact must hold {len(REGION_ORDER)} objects, one per region")
    regions = [row[0] for row in (payload.get("geography") or [])[1:] if isinstance(row, list) and row]
    if sorted(regions) != sorted(REGION_ORDER):
        raise ValueError("region names in 'geography' differ from the page's region list; impact labels would be a guess")
    warnings: list[str] = []
    if tuple(regions) != REGION_ORDER:
        warnings.append(
            "'geography' lists the regions in a different order than the page's fixed list; "
            "impact objects are labelled with the page's list"
        )
    records: list[dict[str, Any]] = []
    for index, impact in enumerate(impacts):
        if not isinstance(impact, dict):
            raise ValueError(f"geographyImpact[{index}] is not an object")
        for field in IMPACT_FIELDS:
            if not is_number(impact.get(field)):
                raise ValueError(f"geographyImpact[{index}].{field} is missing or not a number")
        records.append(
            {
                **impact,
                "record_type": "dashboard_impact_region",
                "source_key": f"impact:{REGION_ORDER[index]}",
                "region": REGION_ORDER[index],
                "region_index": index,
                "source_payload_hash": semantic_hash(impact),
            }
        )
    return records, warnings


def build_totals_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep the impact totals and the separate excluded value as the source sent them."""
    summary = payload.get("impactSummary")
    if not isinstance(summary, dict) or not summary:
        raise ValueError("'impactSummary' is missing or empty")
    records = [
        {
            **summary,
            "record_type": "dashboard_impact_summary",
            "source_key": "impact_summary",
            "source_payload_hash": semantic_hash(summary),
        }
    ]
    warnings: list[str] = []
    excluded = payload.get("excludedResourceExpense")
    if isinstance(excluded, dict) and excluded:
        records.append(
            {
                **excluded,
                "record_type": "dashboard_excluded_resource_expense",
                "source_key": "excluded_resource_expense",
                "source_payload_hash": semantic_hash(excluded),
            }
        )
    else:
        warnings.append("'excludedResourceExpense' is missing or empty; the page does not use it")
    impacts = payload.get("geographyImpact")
    if isinstance(impacts, list) and all(isinstance(item, dict) for item in impacts):
        for total_key, field in SUMMARY_FIELDS.items():
            regional = sum(item.get(field) or 0 for item in impacts)
            if summary.get(total_key) != regional:
                warnings.append(f"impactSummary.{total_key} is {summary.get(total_key)!r} but the regions add up to {regional}")
    return records, warnings


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
    # Totals has only 2 records, below the usual 10-record minimum, so the guard
    # here is simply half of the previous records.
    if previous and removed and not allow_large_removal and len(removed) >= math.ceil(len(previous) * 0.5):
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

    builders: dict[str, Callable[[dict[str, Any]], tuple[list[dict[str, Any]], list[str]]]] = {
        "dashboard_counts": build_count_records,
        "dashboard_impact": build_impact_records,
        "dashboard_totals": build_totals_records,
    }
    for dataset in selected:
        try:
            records, warnings = builders[dataset](payload)
            stats = compare_with_previous(
                dataset, records, args.output_dir / OUTPUT_NAMES[dataset], allow_large_removal=args.allow_large_removal
            )
            warnings = [*warnings, f"sync: {stats}"]
            write_export(args.output_dir, dataset, records, warnings, utc_now(), run_id)
            summary["datasets"][dataset] = {"status": "success", "record_count": len(records), **stats, "warnings": warnings}
            reporter(f"{dataset}: {len(records)} records")
        except Exception as exc:  # Preserve the other datasets and the prior good exports.
            summary["failures"].append(f"{dataset}: {exc}")
            summary["datasets"][dataset] = {"status": "failed", "error": str(exc)}
            print(f"{dataset}: {exc}", file=sys.stderr)
    summary["status"] = "failed" if summary["failures"] else "success"
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
