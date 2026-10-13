"""Incremental scraper for public At Local Thailand content.

It collects seven independent datasets from the main At Local website and its
separate marketplace. Product records intentionally remain in two datasets.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
MAIN_BASE = "https://atlocalthailand.com"
MARKET_BASE = "https://market.atlocalthailand.com"
DEFAULT_DELAY = 1.0
DEFAULT_USER_AGENT = "AtLocalCollector"
DATASETS = (
    "articles",
    "cultures",
    "product_map",
    "marketplace_products",
    "entrepreneurs",
    "festivals",
    "areas_16",
)
PLACEHOLDERS = {"", "-", "ไม่มี", "null", "undefined"}
Reporter = Callable[[str], None]


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    output_name: str
    source_urls: tuple[str, ...]
    minimum_records: int


SPECS = {
    "articles": DatasetSpec(
        "articles", "articles.json", (f"{MAIN_BASE}/api/front/articles",), 1
    ),
    "cultures": DatasetSpec(
        "cultures", "cultures.json", (f"{MAIN_BASE}/api/front/culture",), 1
    ),
    "product_map": DatasetSpec(
        "product_map",
        "product_map.json",
        (f"{MAIN_BASE}/api/s9001/get_items_by_search_filter_v1",),
        100,
    ),
    "marketplace_products": DatasetSpec(
        "marketplace_products", "marketplace_products.json", (f"{MARKET_BASE}/",), 1
    ),
    "entrepreneurs": DatasetSpec(
        "entrepreneurs", "entrepreneurs.json", (f"{MAIN_BASE}/api/front/owners",), 1
    ),
    "festivals": DatasetSpec(
        "festivals",
        "festivals.json",
        (f"{MAIN_BASE}/activity", f"{MAIN_BASE}/api/front/highlights"),
        1,
    ),
    "areas_16": DatasetSpec(
        "areas_16", "areas_16.json", (f"{MAIN_BASE}/api/s9001/get_map_info_all_v2",), 1
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


def fetch_json(
    session: requests.Session,
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    response = session.request(method, url, json=payload, timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=60)
    response.raise_for_status()
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


def absolute_url(value: Any, base_url: str) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    url = urljoin(base_url, text)
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else None


def parse_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = clean_text(value)
    if not text:
        return None
    try:
        result = float(text.replace(",", ""))
        return int(result) if result.is_integer() else result
    except ValueError:
        return None


def parse_json_list(value: Any, warnings: list[str], field: str) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        warnings.append(f"{field} is not a JSON array")
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        warnings.append(f"Malformed JSON in {field}")
        return []
    if not isinstance(parsed, list):
        warnings.append(f"{field} does not decode to an array")
        return []
    return parsed


def semantic_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_id(dataset: str, record: dict[str, Any]) -> str:
    if dataset == "marketplace_products":
        return f"{record['shop_id']}:{record['marketplace_product_id']}"
    field = {
        "articles": "article_id",
        "cultures": "culture_id",
        "product_map": "map_product_id",
        "entrepreneurs": "entrepreneur_id",
        "festivals": "festival_id",
        "areas_16": "area_id",
    }[dataset]
    return str(record[field])


def require_records(dataset: str, records: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for record in records:
        key = record_id(dataset, record)
        title = record.get("title") or record.get("name")
        if not key:
            raise ValueError(f"{dataset}: record missing stable ID")
        # The legacy map catalog currently contains records with an ID and
        # shop/location data but an empty item_name. Preserve those records;
        # their per-record warning makes the source omission visible.
        if not title and dataset != "product_map":
            raise ValueError(f"{dataset}: record missing stable ID or title")
        if key in seen:
            raise ValueError(f"{dataset}: duplicate record ID {key}")
        seen.add(key)
    if len(records) < SPECS[dataset].minimum_records:
        raise ValueError(
            f"{dataset}: found {len(records)} records, below safety floor {SPECS[dataset].minimum_records}"
        )


def api_data(payload: Any, source: str) -> Any:
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise ValueError(f"Unexpected response from {source}")
    return payload.get("data")


def collect_articles(
    session: requests.Session, _: float, __: int, ___: Reporter
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = f"{MAIN_BASE}/api/front/articles"
    data = api_data(fetch_json(session, source), source)
    if not isinstance(data, dict) or not isinstance(data.get("articles"), list):
        raise TypeError("articles response lacks articles array")
    records = []
    for item in data["articles"]:
        records.append(
            {
                "record_type": "article",
                "article_id": str(item["id"]),
                "title": clean_text(item.get("title")),
                "published_date": clean_text(item.get("date")),
                "summary_html": clean_html(item.get("summary")),
                "summary_text": html_text(item.get("summary")),
                "image_url": absolute_url(item.get("imageUrl"), MAIN_BASE),
                "province": {
                    "id": clean_text(item.get("provinceId")),
                    "name": clean_text(item.get("province")),
                },
                "area": {
                    "id": clean_text(item.get("areaId")),
                    "name": clean_text(item.get("area")),
                },
                "active": bool(item.get("active")),
                "detail_api_url": f"{MAIN_BASE}/api/front/article/{item['id']}",
            }
        )
    metadata = {"provinces": data.get("provinces", []), "areas": data.get("areas", [])}
    return records, metadata


def collect_cultures(
    session: requests.Session, _: float, __: int, ___: Reporter
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = f"{MAIN_BASE}/api/front/culture"
    data = api_data(fetch_json(session, source), source)
    if not isinstance(data, list):
        raise TypeError("culture response lacks array")
    records = [
        {
            "record_type": "culture",
            "culture_id": str(item["id"]),
            "type_raw": clean_text(item.get("type")),
            "title": clean_text(item.get("title")),
            "published_date": clean_text(item.get("date")),
            "summary_html": clean_html(item.get("summary")),
            "summary_text": html_text(item.get("summary")),
            "image_url": absolute_url(item.get("imageUrl"), MAIN_BASE),
            "active": bool(item.get("active")),
            "detail_api_url": f"{MAIN_BASE}/api/front/culture/{item['id']}",
        }
        for item in data
    ]
    return records, {}


def collect_entrepreneurs(
    session: requests.Session, _: float, __: int, ___: Reporter
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = f"{MAIN_BASE}/api/front/owners"
    data = api_data(fetch_json(session, source), source)
    if not isinstance(data, list):
        raise TypeError("owners response lacks array")
    records = [
        {
            "record_type": "entrepreneur",
            "entrepreneur_id": str(item["id"]),
            "title": clean_text(item.get("title")),
            "type_raw": clean_text(item.get("type")),
            "image_url": absolute_url(item.get("imageUrl"), MAIN_BASE),
            "active": bool(item.get("active")),
            "source_url": f"{MAIN_BASE}/owner",
        }
        for item in data
    ]
    return records, {}


def collect_product_map(
    session: requests.Session, _: float, __: int, ___: Reporter
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = f"{MAIN_BASE}/api/s9001/get_items_by_search_filter_v1"
    payload = {
        "item_tag": "ALL",
        "item_type": "ALL",
        "item_sale_channel": "ALL",
        "item_district": "ALL",
        "item_search": "",
    }
    response = fetch_json(session, source, "POST", payload)
    if (
        not isinstance(response, dict)
        or response.get("status") != "Success"
        or not isinstance(response.get("data"), list)
    ):
        raise ValueError("map product response is not successful")
    records: list[dict[str, Any]] = []
    for item in response["data"]:
        warnings: list[str] = []
        name = clean_text(item.get("item_name"))
        if not name:
            warnings.append("Missing item_name in source")
        filenames = [
            clean_text(value)
            for value in parse_json_list(
                item.get("picture_list"), warnings, "picture_list"
            )
        ]
        filenames = [value for value in filenames if value]
        tags = [
            clean_text(value)
            for value in parse_json_list(item.get("tags"), warnings, "tags")
        ]
        tags = [value for value in tags if value]
        records.append(
            {
                "record_type": "map_product",
                "map_product_id": item["id"],
                "name": name,
                "price_thb": parse_number(item.get("unit_price")),
                "shop_name": clean_text(item.get("shop_name")),
                "tags": tags,
                "image_filenames": filenames,
                "image_urls": [
                    f"{MAIN_BASE}/api/s9001/get_image_item/{name}" for name in filenames
                ],
                "map_url": absolute_url(item.get("map"), MAIN_BASE),
                "product_type": {
                    "id": item.get("item_type_id"),
                    "name": clean_text(item.get("item_type_name")),
                },
                "sale_channel": {
                    "id": item.get("items_sale_channel_id"),
                    "name": clean_text(item.get("item_sale_channel")),
                },
                "fulfilment_status": {
                    "id": item.get("item_status_id"),
                    "name": clean_text(item.get("item_status")),
                },
                "province": clean_text(item.get("province_th")),
                "district": clean_text(item.get("district_th")),
                "exclusive": bool(item.get("exclusive")),
                "warnings": warnings,
            }
        )
    return records, {}


def split_area_value(value: Any, field: str) -> tuple[str, str]:
    text = clean_text(value)
    if not text or ";" not in text:
        raise ValueError(f"Invalid 16-area {field} value: {value!r}")
    content, identifier = text.rsplit(";", 1)
    if not content or not identifier:
        raise ValueError(f"Invalid 16-area {field} value: {value!r}")
    return content, identifier


def collect_areas_16(
    session: requests.Session, _: float, __: int, ___: Reporter
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = f"{MAIN_BASE}/api/s9001/get_map_info_all_v2"
    response = fetch_json(session, source, "POST", {})
    data = (
        response.get("data")
        if isinstance(response, dict) and response.get("status") == "Success"
        else None
    )
    fields = ("province", "district", "name", "description", "picture_list")
    if not isinstance(data, dict) or any(
        not isinstance(data.get(field), list) for field in fields
    ):
        raise ValueError("16-area response lacks required arrays")
    length = len(data["province"])
    if not length or any(len(data[field]) != length for field in fields):
        raise ValueError("16-area arrays have unequal lengths")
    records = []
    for index in range(length):
        province, area_id = split_area_value(data["province"][index], "province")
        district, check_id = split_area_value(data["district"][index], "district")
        name, name_id = split_area_value(data["name"][index], "name")
        description, description_id = split_area_value(
            data["description"][index], "description"
        )
        images_json, image_id = split_area_value(
            data["picture_list"][index], "picture_list"
        )
        if len({area_id, check_id, name_id, description_id, image_id}) != 1:
            raise ValueError(f"16-area row {index} has inconsistent IDs")
        warnings: list[str] = []
        filenames = [
            clean_text(value)
            for value in parse_json_list(images_json, warnings, "picture_list")
        ]
        filenames = [value for value in filenames if value]
        records.append(
            {
                "record_type": "area_16",
                "area_id": area_id,
                "province": province,
                "district": district,
                "name": name,
                "description_raw": description,
                "image_filenames": filenames,
                "image_urls": [
                    f"{MAIN_BASE}/api/s9001/get_image_shop/{filename}"
                    for filename in filenames
                ],
                "warnings": warnings,
            }
        )
    return records, {}


def extract_next_chunks(page_html: str) -> list[str]:
    chunks: list[str] = []
    for script in BeautifulSoup(page_html, "html.parser").find_all("script"):
        text = script.string or script.get_text()
        if "self.__next_f.push" not in text:
            continue
        match = re.search(r"self\.__next_f\.push\(\[\d+,", text)
        if not match:
            continue
        fragment = text[match.end() :]
        try:
            value, _ = json.JSONDecoder().raw_decode(fragment)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            chunks.append(value)
    return chunks


def extract_marketplace_products(page_html: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for chunk in extract_next_chunks(page_html):
        start = 0
        while True:
            marker = chunk.find('"products":[', start)
            if marker < 0:
                break
            array_start = marker + len('"products":')
            try:
                products, end = decoder.raw_decode(chunk[array_start:])
            except json.JSONDecodeError:
                start = array_start + 1
                continue
            if isinstance(products, list):
                found.extend(
                    item
                    for item in products
                    if isinstance(item, dict) and item.get("id") and item.get("name")
                )
            start = array_start + end
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in found:
        key = (str(item.get("shop_id") or ""), str(item["id"]))
        if key[0]:
            deduped[key] = item
    return list(deduped.values())


def discover_shop_urls(page_html: str) -> list[str]:
    paths = sorted(set(re.findall(r"/shop/(\d+)", page_html)))
    return [f"{MARKET_BASE}/shop/{shop_id}" for shop_id in paths]


def marketplace_record(item: dict[str, Any]) -> dict[str, Any]:
    product_id = str(item["id"])
    shop_id = str(item["shop_id"])
    title = clean_text(item.get("name"))
    slug = re.sub(r"\s+", "-", title or "product")
    images = [
        absolute_url(value, MARKET_BASE)
        for value in item.get("images", [])
        if absolute_url(value, MARKET_BASE)
    ]
    return {
        "record_type": "marketplace_product",
        "marketplace_product_id": product_id,
        "shop_id": shop_id,
        "name": title,
        "description_raw": clean_text(item.get("description")),
        "price_thb": parse_number(item.get("price")),
        "stock_quantity": parse_number(item.get("stock_quantity")),
        "category": clean_text(item.get("category")),
        "active": bool(item.get("is_active")),
        "province": clean_text(item.get("province")),
        "district": clean_text(item.get("district")),
        "image_urls": images,
        "created_at": clean_text(str(item.get("created_at", "")).removeprefix("$D")),
        "updated_at": clean_text(str(item.get("updated_at", "")).removeprefix("$D")),
        "product_url": f"{MARKET_BASE}/product/{product_id}/{slug}",
    }


def collect_marketplace_products(
    session: requests.Session, delay: float, limit: int, report: Reporter
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    home = fetch_text(session, f"{MARKET_BASE}/")
    pages = [(f"{MARKET_BASE}/", home)]
    shop_urls = discover_shop_urls(home)
    if not shop_urls:
        raise ValueError("Marketplace homepage did not expose any shop routes")
    for index, url in enumerate(shop_urls, 1):
        if limit and index >= limit:
            break
        report(f"marketplace products: shop {index}/{len(shop_urls)}")
        time.sleep(delay)
        pages.append((url, fetch_text(session, url)))
    raw: dict[tuple[str, str], dict[str, Any]] = {}
    for _, page in pages:
        for product in extract_marketplace_products(page):
            raw[(str(product["shop_id"]), str(product["id"]))] = product
    records = [marketplace_record(value) for _, value in sorted(raw.items())]
    return records, {"shop_urls": shop_urls}


def canonical_festival_url(value: str) -> str | None:
    url = absolute_url(html_module.unescape(value), MAIN_BASE)
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname != "atlocalthailand.com" or not parsed.path.startswith(
        "/festival/"
    ):
        return None
    lower_path = parsed.path.lower()
    blocked = (
        "photo_regis",
        "register",
        "approve",
        "report",
        "review",
        "admin",
        "login",
    )
    if any(part in lower_path for part in blocked):
        return None
    return f"{MAIN_BASE}{parsed.path.rstrip('/')}"


def festival_urls_from_activity(page_html: str) -> list[str]:
    return sorted(
        {
            url
            for value in re.findall(
                r"""(?:href|url)=["']([^"']+)["']""", page_html, flags=re.IGNORECASE
            )
            if (url := canonical_festival_url(value))
        }
    )


def extract_media_urls(
    soup: BeautifulSoup, page_url: str
) -> tuple[list[str], list[str], list[str]]:
    images = sorted(
        {
            url
            for tag in soup.select("img[src]")
            if (url := absolute_url(tag.get("src"), page_url))
        }
    )
    videos = sorted(
        {
            url
            for tag in soup.select("video[src], source[src], iframe[src]")
            if (url := absolute_url(tag.get("src"), page_url))
        }
    )
    links = sorted(
        {
            url
            for tag in soup.select("a[href]")
            if (url := absolute_url(tag.get("href"), page_url))
            and urlparse(url).hostname
            not in {"atlocalthailand.com", "www.atlocalthailand.com"}
        }
    )
    return images, videos, links


def festival_record(
    url: str, highlight: dict[str, Any] | None, page_html: str
) -> dict[str, Any]:
    soup = BeautifulSoup(page_html, "html.parser")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    heading = soup.find("h1")
    title = clean_text(heading.get_text(" ", strip=True) if heading else None)
    if not title and highlight:
        title = clean_text(highlight.get("title"))
    if not title:
        title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else None)
    body = soup.find("main") or soup.body or soup
    description_html = clean_html(str(body))
    images, videos, links = extract_media_urls(soup, url)
    cover = (
        absolute_url(highlight.get("imageUrl"), MAIN_BASE)
        if highlight
        else (images[0] if images else None)
    )
    return {
        "record_type": "festival",
        "festival_id": urlparse(url).path.removeprefix("/festival/"),
        "title": title,
        "source_url": url,
        "highlight_id": (
            str(highlight["id"])
            if highlight and highlight.get("id") is not None
            else None
        ),
        "cover_image_url": cover,
        "description_html": description_html,
        "description_text": html_text(description_html),
        "image_urls": images,
        "video_urls": videos,
        "external_links": links,
        "discovered_from": [
            source
            for source in ("activity_page", "front_highlights_api")
            if (source == "activity_page" or highlight)
        ],
    }


def collect_festivals(
    session: requests.Session, delay: float, limit: int, report: Reporter
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    highlights_url = f"{MAIN_BASE}/api/front/highlights"
    highlights_data = api_data(fetch_json(session, highlights_url), highlights_url)
    if not isinstance(highlights_data, list):
        raise TypeError("highlights response lacks array")
    highlights: dict[str, dict[str, Any]] = {}
    for item in highlights_data:
        if isinstance(item, dict) and item.get("url"):
            url = canonical_festival_url(str(item["url"]))
            if url:
                highlights[url] = item
    activity = fetch_text(session, f"{MAIN_BASE}/activity")
    activity_urls = festival_urls_from_activity(activity)
    urls = sorted(set(activity_urls) | set(highlights))
    if not urls:
        raise ValueError("No public festival routes discovered")
    records = []
    for index, url in enumerate(urls, 1):
        if limit and index > limit:
            break
        report(f"festivals: {index}/{len(urls)}")
        if index > 1:
            time.sleep(delay)
        records.append(
            festival_record(url, highlights.get(url), fetch_text(session, url))
        )
    return records, {
        "activity_urls": activity_urls,
        "highlight_count": len(highlights_data),
    }


COLLECTORS = {
    "articles": collect_articles,
    "cultures": collect_cultures,
    "product_map": collect_product_map,
    "marketplace_products": collect_marketplace_products,
    "entrepreneurs": collect_entrepreneurs,
    "festivals": collect_festivals,
    "areas_16": collect_areas_16,
}


def compare_with_previous(
    dataset: str,
    records: list[dict[str, Any]],
    previous_path: Path,
    allow_large_removal: bool,
) -> dict[str, int]:
    """Count changes against the previous output file and apply the removal guard."""
    counts = {"new": 0, "changed": 0, "unchanged": 0, "retired": 0}
    previous: dict[str, dict[str, Any]] = {}
    if previous_path.exists():
        try:
            payload = json.loads(previous_path.read_text(encoding="utf-8"))
            data = payload["data"]
            if not isinstance(data, list):
                raise TypeError("'data' is not a list")
            for item in data:
                key = record_id(dataset, item)
                if key in previous:
                    raise ValueError(f"duplicate record ID {key}")
                previous[key] = item
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError(
                f"{dataset}: cannot read previous output {previous_path.name}: {error!r}"
            ) from error
    seen: set[str] = set()
    for record in records:
        key = record_id(dataset, record)
        seen.add(key)
        if key not in previous:
            counts["new"] += 1
        elif semantic_hash(previous[key]) != semantic_hash(record):
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
    missing = [key for key in previous if key not in seen]
    if previous and len(missing) / len(previous) > 0.25 and not allow_large_removal:
        raise ValueError(
            f"{dataset}: refusing to retire {len(missing)}/{len(previous)} active records"
        )
    counts["retired"] = len(missing)
    return counts


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def write_dataset(
    path: Path,
    dataset: str,
    records: list[dict[str, Any]],
    metadata: dict[str, Any],
    scraped_at: str,
) -> int:
    """Write one dataset envelope: records ordered by ID, keys sorted recursively."""
    exported = [
        json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True))
        for record in sorted(records, key=lambda record: record_id(dataset, record))
    ]
    write_json(
        path,
        {
            "schema_version": 1,
            "dataset": dataset,
            "source_urls": list(SPECS[dataset].source_urls),
            "scraped_at": scraped_at,
            "record_count": len(exported),
            "warnings": [],
            "metadata": metadata,
            "data": exported,
        },
    )
    return len(exported)



def run_scrape(args: argparse.Namespace) -> int:
    selected = DATASETS if args.dataset == "all" else (args.dataset,)
    report = report_factory(args.quiet)
    now = utc_now()
    run_id = now
    results: dict[str, Any] = {}
    failures: list[str] = []
    session = create_session(args.user_agent)
    try:
        for dataset in selected:
            try:
                report(f"{dataset}: collecting")
                records, metadata = COLLECTORS[dataset](
                    session, args.delay, args.limit, report
                )
                require_records(dataset, records)
                if args.limit:
                    results[dataset] = {
                        "record_count": len(records),
                        "smoke_test": True,
                        "metadata": metadata,
                    }
                    continue
                output_path = OUTPUT_DIR / SPECS[dataset].output_name
                stats = compare_with_previous(
                    dataset, records, output_path, args.allow_large_removal
                )
                record_count = write_dataset(
                    output_path, dataset, records, metadata, now
                )
                results[dataset] = {"record_count": record_count, **stats}
            except Exception as error:  # noqa: BLE001
                failures.append(f"{dataset}: {error}")
                report(f"{dataset}: FAILED: {error}")
        summary = {
            "run_id": run_id,
            "status": "failed" if failures else "success",
            "datasets": results,
            "failures": failures,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if failures else 0
    finally:
        session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape public At Local Thailand content."
    )
    parser.add_argument("--dataset", choices=("all", *DATASETS), default="all")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Smoke test limit. Does not write JSON.",
    )
    parser.add_argument("--allow-large-removal", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.delay < 0 or args.limit < 0:
        raise SystemExit("--delay and --limit must be non-negative")
    return run_scrape(args)


if __name__ == "__main__":
    sys.exit(main())
