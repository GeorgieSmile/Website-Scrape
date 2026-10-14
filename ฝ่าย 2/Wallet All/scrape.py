#!/usr/bin/env python3
"""Incremental scraper for the LeSuper "ธุรกิจปันกัน 4" project-wide wallet aggregates.

The public dashboard asks two POST endpoints (household and community
business) for one month at a time. The script asks for every month from
2024-06 up to the current month-to-date and exports one record per month, each
holding the source response exactly as sent. Each run is compared with the
previous export in the output directory, which is the only state kept.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import re
import sys
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
API_BASE = "https://lesuper.app/api/opendata/superapp/gen4"
DEFAULT_USER_AGENT = "WalletAllCollector"
DEFAULT_DELAY = 0.25  # Pause after each request, per worker.
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_TIMEOUT = 60
EARLIEST_MONTH = "2024-06"  # First month with data; earlier months answer 200 with null values.
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
Reporter = Callable[[str], None]

DATASETS = ("wallet_household", "wallet_business")
WALLET_CODES = {"wallet_household": "hh", "wallet_business": "bu"}
OUTPUT_NAMES = {"wallet_household": "wallet_household.json", "wallet_business": "wallet_business.json"}
# A run with fewer months than this is treated as a broken response, not as news.
MINIMUM_RECORDS = 20


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
        allowed_methods=frozenset({"GET", "POST"}),  # The wallet API only answers POST; the request is read-only.
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retries))
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


def post_month(session: requests.Session, url: str, request_date: str, timeout: int) -> dict[str, Any]:
    """POST the dashboard's own request body; an empty date means the current month."""
    response = session.post(url, json={"date": request_date}, timeout=timeout)
    response.raise_for_status()
    # The source labels JSON as text/html or text/plain, so decode the body ourselves.
    payload = json.loads(response.content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{url} date={request_date!r}: response is not a JSON object")
    return payload


def month_end(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def months_before(end_month: str, start_month: str = EARLIEST_MONTH) -> list[str]:
    """Return every month from start_month up to, but not including, end_month."""
    year, number = (int(part) for part in start_month.split("-"))
    end_year, end_number = (int(part) for part in end_month.split("-"))
    months: list[str] = []
    while (year, number) < (end_year, end_number):
        months.append(f"{year:04d}-{number:02d}")
        year, number = (year + 1, 1) if number == 12 else (year, number + 1)
    return months


def semantic_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_time(payload: dict[str, Any]) -> str | None:
    """Return the source's own snapshot time (UTC ISO) when the month has data."""
    snapshot = payload.get("snapshot")
    updated = snapshot.get("updatedAt") if isinstance(snapshot, dict) else None
    seconds = updated.get("_seconds") if isinstance(updated, dict) else None
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


def month_record(code: str, payload: dict[str, Any], request_date: str, *, month_to_date: bool) -> dict[str, Any]:
    """Wrap one source response without changing any of its values."""
    month = payload.get("thisMonth")
    if not isinstance(month, str) or not MONTH_PATTERN.match(month):
        raise ValueError(f"{code} date={request_date!r}: response has no usable thisMonth ({month!r})")
    expected = request_date[:7]
    if request_date and month != expected:
        raise ValueError(f"{code}: asked for {expected} but the source answered for {month}")
    as_of = snapshot_time(payload)
    return {
        "record_type": "wallet_month",
        "source_key": f"{code}:{month}",
        "wallet": code,
        "month": month,
        "request_date": request_date,
        "is_month_to_date": month_to_date,
        "has_snapshot": isinstance(payload.get("snapshot"), dict),
        "as_of_utc": as_of,
        "response": payload,
        "source_payload_hash": semantic_hash(payload),
    }


def collect_months(
    dataset: str,
    *,
    months: list[str] | None,
    workers: int,
    delay: float,
    timeout: int,
    user_agent: str,
    reporter: Reporter,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect one record per month; months=None means current month plus all history."""
    code = WALLET_CODES[dataset]
    url = f"{API_BASE}/{code}"
    get_session = thread_session_provider(user_agent)
    warnings: list[str] = []
    records: list[dict[str, Any]] = []

    def pause() -> None:
        if delay > 0:
            time.sleep(delay)

    if months is None:
        current = post_month(get_session(), url, "", timeout)
        pause()
        record = month_record(code, current, "", month_to_date=True)
        if not record["has_snapshot"]:
            raise ValueError(f"{code}: the current month came back without a snapshot")
        records.append(record)
        months = months_before(record["month"])
        reporter(f"{dataset}: current month is {record['month']}; fetching {len(months)} earlier months")

    def fetch_one(month: str) -> dict[str, Any]:
        payload = post_month(get_session(), url, month_end(month), timeout)
        pause()
        return payload

    with closing(fetch_ordered(fetch_one, months, workers)) as results:
        for month, payload in zip(months, results):
            record = month_record(code, payload, month_end(month), month_to_date=False)
            if not record["has_snapshot"]:
                warnings.append(f"{record['source_key']}: the source answered without a snapshot")
            records.append(record)
    return sorted(records, key=lambda record: record["month"]), warnings


def validate_records(dataset: str, records: list[dict[str, Any]], *, minimum: int | None = None) -> None:
    if len(records) < (MINIMUM_RECORDS if minimum is None else minimum):
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
    minimum: int | None = None,
) -> dict[str, int]:
    validate_records(dataset, records, minimum=minimum)
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
    """Write one export: records in month order, keys sorted recursively."""
    exported = [
        json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for record in sorted(records, key=lambda record: record["source_key"])
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
    parser.add_argument(
        "--month", action="append", metavar="YYYY-MM",
        help="Smoke test: fetch only this month (repeatable). Writes nothing.",
    )
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Seconds each worker pauses after every request.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Concurrent month requests, 1 to {MAX_WORKERS}.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--allow-large-removal", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.delay < 0 or args.timeout < 1:
        parser.error("delay must be non-negative and timeout positive")
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    if args.month and any(not MONTH_PATTERN.match(month) for month in args.month):
        parser.error("--month must look like 2024-09")
    selected = tuple(args.dataset or DATASETS)
    reporter = report_factory(args.quiet)

    smoke = bool(args.month)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary: dict[str, Any] = {"run_id": run_id, "datasets": {}, "failures": []}
    for dataset in selected:
        try:
            reporter(f"Collecting {dataset}")
            records, warnings = collect_months(
                dataset, months=sorted(set(args.month)) if smoke else None, workers=args.workers,
                delay=args.delay, timeout=args.timeout, user_agent=args.user_agent, reporter=reporter,
            )
            if smoke:
                summary["datasets"][dataset] = {"status": "smoke_test", "record_count": len(records), "warnings": warnings}
                reporter(f"{dataset}: smoke test collected {len(records)} records")
                continue
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
