#!/usr/bin/env python3
"""Write output/manifest_input.json for one ฝ่าย 2 scraper, ready for evidence_push.py.

The human-declared parts (source_id, dataset_key, grain, identity_fields and the
as_of rule) come from the scraper's committed evidence.json. Everything else is
read from the output files of one finished run. Nothing is guessed: if the
output folder is not exactly one complete run of the declared datasets, no
manifest is written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any

DECLARATION_NAME = "evidence.json"
MANIFEST_INPUT_NAME = "manifest_input.json"
# Files evidence_push.py itself skips when uploading a run folder.
PUSH_SKIPPED_NAMES = {MANIFEST_INPUT_NAME, "manifest.json"}
RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")
SUPPORTED_AS_OF_RULES = {"scraped_at"}
FETCHED_BY_ENV = "EVIDENCE_FETCHED_BY"


class ManifestError(ValueError):
    pass


def required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be a non-empty string")
    return value


def safe_relative_file(value: Any, label: str) -> str:
    name = required_text(value, label)
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ManifestError(f"{label} is not a safe relative path: {name!r}")
    return name


def load_declaration(scraper_dir: Path) -> dict[str, Any]:
    path = scraper_dir / DECLARATION_NAME
    try:
        declaration = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"missing {path}") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"cannot parse {path}: {error}") from error
    if not isinstance(declaration, dict):
        raise ManifestError(f"{path} must be a JSON object")
    required_text(declaration.get("source_id"), "source_id")
    if declaration.get("as_of_rule") not in SUPPORTED_AS_OF_RULES:
        raise ManifestError(f"as_of_rule must be one of {sorted(SUPPORTED_AS_OF_RULES)}")
    datasets = declaration.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise ManifestError("datasets must be a non-empty list")
    keys: set[str] = set()
    files: set[str] = set()
    for index, entry in enumerate(datasets):
        label = f"datasets[{index}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{label} must be an object")
        key = required_text(entry.get("dataset_key"), f"{label}.dataset_key")
        file = safe_relative_file(entry.get("file"), f"{label}.file")
        required_text(entry.get("grain"), f"{label}.grain")
        identity = entry.get("identity_fields")
        if not isinstance(identity, list) or not all(isinstance(item, str) and item.strip() for item in identity):
            raise ManifestError(f"{label}.identity_fields must be a list of non-empty strings")
        if key in keys:
            raise ManifestError(f"duplicate dataset_key: {key}")
        if file in files:
            raise ManifestError(f"duplicate file: {file}")
        keys.add(key)
        files.add(file)
    return declaration


def parse_scraped_at(value: Any, file: str) -> datetime:
    text = required_text(value, f"{file} scraped_at")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise ManifestError(f"{file} scraped_at is not an ISO timestamp: {text!r}") from error
    if moment.tzinfo is None:
        raise ManifestError(f"{file} scraped_at has no timezone: {text!r}")
    return moment


def build_manifest_input(scraper_dir: Path, fetched_by: str) -> tuple[dict[str, Any], str]:
    """Return the manifest_input content and the run ID shared by every dataset file."""
    fetched_by = required_text(fetched_by, "fetched_by").strip()
    declaration = load_declaration(scraper_dir)
    output_dir = scraper_dir / "output"
    if not output_dir.is_dir():
        raise ManifestError(f"missing output folder: {output_dir}")

    declared = {entry["file"] for entry in declaration["datasets"]}
    present = {
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() and path.relative_to(output_dir).as_posix() not in PUSH_SKIPPED_NAMES
    }
    missing = sorted(declared - present)
    if missing:
        raise ManifestError(f"declared files missing from output/: {', '.join(missing)}")
    # evidence_push.py uploads every other file in the folder, so an undeclared file would be published.
    undeclared = sorted(present - declared)
    if undeclared:
        raise ManifestError(f"undeclared files in output/ would be uploaded; remove them first: {', '.join(undeclared)}")

    datasets: list[dict[str, Any]] = []
    upstream_urls: list[str] = []
    run_ids: dict[str, set[str]] = {}
    timestamps: list[tuple[datetime, str]] = []
    for entry in declaration["datasets"]:
        file = entry["file"]
        try:
            envelope = json.loads((output_dir / file).read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ManifestError(f"cannot parse output/{file}: {error}") from error
        if not isinstance(envelope, dict):
            raise ManifestError(f"output/{file} must be a JSON object envelope")
        run_id = required_text(envelope.get("run_id"), f"{file} run_id")
        run_ids.setdefault(run_id, set()).add(file)
        scraped_at = envelope.get("scraped_at")
        timestamps.append((parse_scraped_at(scraped_at, file), scraped_at))
        urls = envelope.get("source_urls")
        if urls is None and envelope.get("source_url") is not None:
            urls = [envelope["source_url"]]
        if urls is None and envelope.get("source_base_url") is not None:
            urls = [envelope["source_base_url"]]
        for url in urls or []:
            if isinstance(url, str) and url and url not in upstream_urls:
                upstream_urls.append(url)
        datasets.append(
            {
                "dataset_key": entry["dataset_key"],
                "file": file,
                "as_of": scraped_at,
                "grain": entry["grain"],
                "identity_fields": list(entry["identity_fields"]),
            }
        )

    if len(run_ids) != 1:
        detail = "; ".join(f"{run_id}: {', '.join(sorted(files))}" for run_id, files in sorted(run_ids.items()))
        raise ManifestError(f"output/ mixes runs, re-run the full scraper first ({detail})")
    run_id = next(iter(run_ids))
    if not RUN_ID_PATTERN.match(run_id):
        raise ManifestError(f"run_id must look like 20260818T041500Z, got {run_id!r}")

    manifest_input = {
        "fetched_by": fetched_by,
        "fetched_at": min(timestamps)[1],
        "upstream": [{"url": url} for url in upstream_urls],
        "datasets": datasets,
    }
    return manifest_input, run_id


def write_json(path: Path, value: dict[str, Any]) -> None:
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        except BaseException:
            temporary.close()
            temporary_path.unlink(missing_ok=True)
            raise
    temporary_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scraper_dir", type=Path, help='Scraper folder, e.g. "ฝ่าย 2/Cultural Map"')
    parser.add_argument(
        "--fetched-by",
        default=os.environ.get(FETCHED_BY_ENV, ""),
        help=f"GitHub name of the person who ran the scraper (or set {FETCHED_BY_ENV}).",
    )
    args = parser.parse_args()
    try:
        manifest_input, run_id = build_manifest_input(args.scraper_dir, args.fetched_by)
    except ManifestError as error:
        print(f"No manifest written: {error}", file=sys.stderr)
        return 2
    target = args.scraper_dir / "output" / MANIFEST_INPUT_NAME
    write_json(target, manifest_input)
    declaration = load_declaration(args.scraper_dir)
    print(f"Wrote {target} ({len(manifest_input['datasets'])} datasets, run {run_id})")
    print("Push with (from aiat-dashboard-final):")
    print(f'  python tools/evidence_push.py {declaration["source_id"]} "{args.scraper_dir.resolve() / "output"}" --run-id {run_id}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
