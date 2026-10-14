#!/usr/bin/env python3
"""Incremental scraper for the LeSuper "ธุรกิจปันกัน 4" wallet aggregates by business cluster.

The public dashboard asks two POST endpoints (household and community
business) for the current month-to-date figures of each business cluster. The
source takes no date, so a run can only see the current month. The script
exports one record per cluster, each holding the source's cluster figures
exactly as sent, and compares each run with the previous export in the output
directory, which is the only state kept.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
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
API_BASE = "https://lesuper.app/api/opendata/superapp/gen4/cluster"
DEFAULT_USER_AGENT = "WalletClusterCollector"
DEFAULT_TIMEOUT = 60
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
Reporter = Callable[[str], None]

DATASETS = ("wallet_cluster_household", "wallet_cluster_business")
WALLET_CODES = {"wallet_cluster_household": "hh", "wallet_cluster_business": "bu"}
OUTPUT_NAMES = {
    "wallet_cluster_household": "wallet_cluster_household.json",
    "wallet_cluster_business": "wallet_cluster_business.json",
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
        allowed_methods=frozenset({"GET", "POST"}),  # The cluster API only answers POST; the request is read-only.
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def post_snapshot(session: requests.Session, url: str, timeout: int) -> dict[str, Any]:
    """POST with no body, exactly as the dashboard does; the server ignores any body."""
    response = session.post(url, timeout=timeout)
    response.raise_for_status()
    # The source labels JSON as text/html or text/plain, so decode the body ourselves.
    payload = json.loads(response.content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{url}: response is not a JSON object")
    return payload


def semantic_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seconds_to_utc(updated: Any) -> str | None:
    seconds = updated.get("_seconds") if isinstance(updated, dict) else None
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


def build_cluster_records(code: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Wrap each cluster's source figures without changing any of its values.

    The response lists the clusters three times (figures, names, counts), so
    the lists are cross-checked and any disagreement fails the dataset rather
    than guessing which list is right.
    """
    month = payload.get("thisMonth")
    if not isinstance(month, str) or not MONTH_PATTERN.match(month):
        raise ValueError(f"{code}: response has no usable thisMonth ({month!r})")
    clusters = payload.get("snapshotClusters")
    names = payload.get("clustersName")
    categories = payload.get("categories")
    if not isinstance(clusters, list) or not clusters:
        raise ValueError(f"{code}: 'snapshotClusters' is missing or empty")
    if not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names):
        raise ValueError(f"{code}: 'clustersName' is missing or has blank names")
    if len(clusters) != len(names):
        raise ValueError(f"{code}: {len(clusters)} clusters but {len(names)} cluster names")
    if len(set(names)) != len(names):
        raise ValueError(f"{code}: cluster names are not unique")
    if not isinstance(categories, list) or len(categories) != len(names) + 1:
        raise ValueError(f"{code}: 'categories' does not hold a header row plus {len(names)} clusters")
    counts: dict[str, Any] = {}
    for row in categories[1:]:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str):
            raise ValueError(f"{code}: malformed 'categories' row {row!r}")
        counts[row[0]] = row[1]
    if set(counts) != set(names):
        raise ValueError(f"{code}: 'categories' names differ from 'clustersName'")
    ids: list[str | None] = [None] * len(names)
    if "clusters" in payload:  # The business endpoint also sends [{id, name}].
        listed = payload["clusters"]
        if not isinstance(listed, list) or [item.get("name") for item in listed if isinstance(item, dict)] != names:
            raise ValueError(f"{code}: 'clusters' names differ from 'clustersName'")
        ids = [item.get("id") for item in listed]

    records: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict) or cluster.get("category") != names[index]:
            raise ValueError(f"{code}: cluster {index} does not match name {names[index]!r}")
        business_count = counts[names[index]]
        records.append(
            {
                "record_type": "wallet_cluster_month_to_date",
                "source_key": f"{code}:{index}",
                "wallet": code,
                "month": month,
                "thisMonthName": payload.get("thisMonthName"),
                "yesterdayName": payload.get("yesterdayName"),
                "cluster_index": index,
                "cluster_id": ids[index],
                "category": names[index],
                "business_count": business_count,
                "as_of_utc": seconds_to_utc(cluster.get("updatedAt")),
                "snapshot": cluster,
                "source_payload_hash": semantic_hash(
                    {"snapshot": cluster, "business_count": business_count, "month": month}
                ),
            }
        )
    return records


def validate_records(dataset: str, records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValueError(f"{dataset}: found no records, below safety floor")
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
    """Count new, changed, unchanged and retired clusters against the last export.

    The record key leaves the month out, so a month rollover shows up as
    changed clusters. A handful of clusters is below the usual 10-record
    removal floor, so the guard here trips when half or more of the previous
    clusters are missing.
    """
    validate_records(dataset, records)
    previous = load_previous_records(dataset, previous_path)
    incoming = {record["source_key"]: record for record in records}
    removed = set(previous) - set(incoming)
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
    """Write one export: records in cluster order, keys sorted recursively."""
    exported = [
        json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for record in sorted(records, key=lambda record: record["cluster_index"])
    ]
    write_json(
        output_dir / OUTPUT_NAMES[dataset],
        {
            "schema_version": 1,
            "dataset": dataset,
            "source_urls": [f"{API_BASE}/{WALLET_CODES[dataset]}"],
            "scraped_at": scraped_at,
            "run_id": run_id,
            "record_count": len(exported),
            "warnings": warnings,
            "data": exported,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", action="append", choices=DATASETS, help="Collect only this dataset. Repeatable.")
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
    session = create_session(args.user_agent)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary: dict[str, Any] = {"run_id": run_id, "datasets": {}, "failures": []}
    for dataset in selected:
        try:
            code = WALLET_CODES[dataset]
            reporter(f"Collecting {dataset}")
            payload = post_snapshot(session, f"{API_BASE}/{code}", args.timeout)
            records = build_cluster_records(code, payload)
            stats = compare_with_previous(
                dataset, records, args.output_dir / OUTPUT_NAMES[dataset], allow_large_removal=args.allow_large_removal
            )
            warnings = [f"sync: {stats}"]
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
