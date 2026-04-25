from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, quote_from_bytes, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


KST = timezone(timedelta(hours=9))
IMAGE_EXTENSIONS = (".avif", ".gif", ".jpg", ".jpeg", ".png", ".webp")
DEFAULT_FALLBACK_IMAGE_URL = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'%3E"
    "%3Crect width='640' height='360' fill='%2322292f'/%3E"
    "%3Cpath d='M228 192h184v24H228zM256 144h128v24H256z' fill='%239aa7b2'/%3E"
    "%3Ctext x='320' y='250' text-anchor='middle' font-family='Arial,sans-serif' "
    "font-size='28' fill='%23d8dee6'%3ELINK%3C/text%3E%3C/svg%3E"
)
SKIP_IMAGE_HINTS = (
    "blank",
    "btn_",
    "button",
    "captcha",
    "common",
    "emoji",
    "emoticon",
    "icon",
    "logo",
    "profile",
    "rank",
    "reply",
    "spacer",
    "sprite",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


@dataclass
class CrawlSettings:
    hours: int = 12
    max_items_per_source: int = 12
    max_total_items: int = 500
    request_timeout: int = 15
    detail_timeout: int = 15
    sleep_seconds: float = 0.7
    kv_key: str = "feed:latest"


def log(message: str) -> None:
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_settings(config: dict[str, Any]) -> CrawlSettings:
    return CrawlSettings(**{**CrawlSettings().__dict__, **config.get("settings", {})})


def load_existing_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"warning: ignored existing payload: {exc}")
        return {}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(value or "")).lower()


def display_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def has_keyword(title: str, keywords: list[str]) -> bool:
    clean = normalize_text(title)
    return any(normalize_text(keyword) in clean for keyword in keywords)


def source_keywords(source: dict[str, Any], default_keywords: list[str]) -> list[str]:
    if source.get("match_all"):
        return []
    return source.get("keywords") or default_keywords


def source_blocked_keywords(source: dict[str, Any], default_blocked_keywords: list[str]) -> list[str]:
    return [*default_blocked_keywords, *source.get("blocked_keywords", [])]


def source_item_limit(source: dict[str, Any], settings: CrawlSettings) -> int:
    value = source.get("max_items_per_source", source.get("max_items", settings.max_items_per_source))
    return max(1, int(value or settings.max_items_per_source))


def has_blocked_keyword(value: str, blocked_keywords: list[str]) -> bool:
    clean = normalize_text(value)
    return any(normalize_text(keyword) in clean for keyword in blocked_keywords)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        }
    )
    return session


def fetch_text(session: requests.Session, url: str, timeout: int) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code < 500 or attempt == 2:
                raise
            last_error = exc
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise
            last_error = exc

        time.sleep(0.8 * (attempt + 1))

    if last_error:
        raise last_error
    raise RuntimeError(f"failed to fetch {url}")


def with_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    encoded = f"{quote(key, safe='')}={quote(value, safe='')}"
    clean_parts: list[str] = []
    replaced = False

    for part in parsed.query.split("&"):
        if not part:
            continue
        name = part.split("=", 1)[0]
        if name == key:
            clean_parts.append(encoded)
            replaced = True
        else:
            clean_parts.append(part)

    if not replaced:
        clean_parts.append(encoded)

    clean_query = "&".join(clean_parts)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, parsed.fragment))


def expand_pages(url: str, page_count: int, page_param: str, page_start: int) -> list[str]:
    urls = [url]
    for page_number in range(page_start + 1, page_start + page_count):
        urls.append(with_query_param(url, page_param, str(page_number)))
    return urls


def encode_search_keyword(keyword: str, encoding: str) -> str:
    normalized = (encoding or "utf-8").lower().replace("_", "-")
    if normalized in {"euc-kr", "cp949"}:
        return quote_from_bytes(keyword.encode("cp949", errors="ignore"))
    return quote(keyword)


def search_keywords(source: dict[str, Any], default_keywords: list[str]) -> list[str]:
    return source.get("search_keywords") or source.get("keywords") or default_keywords


def source_search_urls(source: dict[str, Any], default_keywords: list[str]) -> list[str]:
    urls = list(source.get("search_urls") or [])
    templates = source.get("search_url_templates") or source.get("search_url_template") or []
    if isinstance(templates, str):
        templates = [templates]

    keyword_encoding = source.get("search_encoding", "utf-8")
    for template in templates:
        for keyword in search_keywords(source, default_keywords):
            encoded = encode_search_keyword(str(keyword), keyword_encoding)
            urls.append(str(template).replace("{keyword}", encoded))

    page_count = max(1, int(source.get("search_pages", 1) or 1))
    page_param = str(source.get("search_page_param", source.get("page_param", "page")))
    page_start = int(source.get("search_page_start", source.get("page_start", 1)) or 1)
    expanded: list[str] = []
    for url in urls:
        expanded.extend(expand_pages(urljoin(source["url"], url), page_count, page_param, page_start))
    return expanded


def source_list_urls(source: dict[str, Any], default_keywords: list[str] | None = None) -> list[str]:
    explicit_urls = source.get("list_urls") or source.get("page_urls") or source.get("urls")
    if explicit_urls:
        list_urls = [urljoin(source["url"], url) for url in explicit_urls]
    else:
        page_count = max(1, int(source.get("pages", 1) or 1))
        page_param = str(source.get("page_param", "page"))
        page_start = int(source.get("page_start", 1) or 1)
        list_urls = expand_pages(source["url"], page_count, page_param, page_start)

    urls = [*list_urls, *source_search_urls(source, default_keywords or [])]
    seen: set[str] = set()
    urls = [url for url in urls if not (url in seen or seen.add(url))]
    return urls


def canonical_url(url: str, source: dict[str, Any] | None = None) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    keep_keys = {"id", "no", "num", "document_srl", "wr_id", "idx", "code", "bo_table", "table", "b", "nid"}
    if source:
        keep_keys.update(str(key).lower() for key in source.get("canonical_keep_keys", []))
    kept = []
    for key in sorted(query):
        if key.lower() in keep_keys:
            for value in query[key]:
                kept.append((key, value))
    clean_query = urlencode(kept)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", clean_query, ""))


def source_blocked_urls(source: dict[str, Any]) -> set[str]:
    return {
        canonical_url(urljoin(source["url"], str(url)), source)
        for url in source.get("blocked_urls", [])
    }


def is_allowed_link(url: str, source: dict[str, Any]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False

    base_host = urlparse(source["url"]).netloc.lower()
    if parsed.netloc.lower() and parsed.netloc.lower() != base_host:
        return False

    haystack = f"{parsed.path}?{parsed.query}"
    allow_rules = source.get("link_allow") or []
    regex_rules = source.get("link_allow_regex") or []
    if not allow_rules and not regex_rules:
        return True
    if any(rule in haystack for rule in allow_rules):
        return True
    return any(re.search(rule, haystack) for rule in regex_rules)


def discover_candidates(
    session: requests.Session,
    source: dict[str, Any],
    keywords: list[str],
    blocked_keywords: list[str],
    settings: CrawlSettings,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    errors: list[str] = []
    active_keywords = source_keywords(source, keywords)
    active_blocked_keywords = source_blocked_keywords(source, blocked_keywords)
    active_blocked_urls = source_blocked_urls(source)

    for index, list_url in enumerate(source_list_urls(source, keywords)):
        if index:
            time.sleep(settings.sleep_seconds)

        try:
            html_text = fetch_text(session, list_url, settings.request_timeout)
        except Exception as exc:
            errors.append(f"{list_url}: {exc}")
            continue

        soup = BeautifulSoup(html_text, "html.parser")

        for anchor in soup.select("a[href]"):
            title = display_text(anchor.get_text(" ", strip=True))
            if not title or len(title) < 2 or len(title) > 140:
                continue
            if active_keywords and not has_keyword(title, active_keywords):
                continue
            if has_blocked_keyword(title, active_blocked_keywords):
                continue

            url = urljoin(list_url, anchor.get("href", ""))
            if not is_allowed_link(url, source):
                continue

            key = canonical_url(url, source)
            if key in active_blocked_urls:
                continue
            if key in seen:
                continue
            seen.add(key)
            candidate = {"title": title, "url": url}
            published_at = extract_candidate_published_at(anchor)
            if published_at:
                candidate["publishedAt"] = published_at
            candidates.append(candidate)

    if not candidates and errors:
        raise RuntimeError("; ".join(errors[:3]))

    return candidates[: source_item_limit(source, settings) * 4]


def find_image_url(soup: BeautifulSoup, page_url: str) -> str:
    meta_selectors = [
        'meta[property="og:image"]',
        'meta[name="twitter:image"]',
        'meta[property="twitter:image"]',
    ]
    for selector in meta_selectors:
        tag = soup.select_one(selector)
        image_url = tag.get("content", "") if tag else ""
        if is_usable_image_url(image_url):
            return urljoin(page_url, image_url)

    content_selectors = [
        ".view_content img",
        ".read_body img",
        ".board-read img",
        ".article img",
        ".post img",
        ".contents img",
        ".content img",
        ".xe_content img",
        "#bo_v_con img",
        "article img",
        "img",
    ]
    for selector in content_selectors:
        for image in soup.select(selector):
            image_url = (
                image.get("data-original")
                or image.get("data-src")
                or image.get("data-lazy-src")
                or image.get("src")
                or ""
            )
            if is_usable_image_url(image_url):
                return urljoin(page_url, image_url)

    return ""


def fallback_image_url(source: dict[str, Any], page_url: str) -> str:
    image_url = source.get("fallback_image_url") or ""
    if not image_url and source.get("allow_missing_image"):
        image_url = DEFAULT_FALLBACK_IMAGE_URL
    if image_url.startswith("data:"):
        return image_url
    return urljoin(page_url, image_url) if image_url else ""


def is_usable_image_url(value: str) -> bool:
    if not value:
        return False
    lowered = html.unescape(value).lower()
    if lowered.startswith("data:"):
        return False
    if any(hint in lowered for hint in SKIP_IMAGE_HINTS):
        return False
    parsed = urlparse(lowered)
    path = parsed.path
    return path.endswith(IMAGE_EXTENSIONS) or "image" in lowered or "thumbnail" in lowered


def parse_published_at(soup: BeautifulSoup) -> str:
    selectors = [
        'meta[property="article:published_time"]',
        'meta[name="date"]',
        'time[datetime]',
    ]
    for selector in selectors:
        tag = soup.select_one(selector)
        value = ""
        if tag:
            value = tag.get("content") or tag.get("datetime") or ""
        iso = normalize_date(value)
        if iso:
            return iso
    return ""


def extract_candidate_published_at(anchor: Any) -> str:
    parent = anchor
    for _ in range(5):
        parent = getattr(parent, "parent", None)
        if not parent:
            break
        text = display_text(parent.get_text(" ", strip=True))
        for pattern in (
            r"\d{4}[-./]\d{2}[-./]\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?",
            r"\d{4}/\d{2}/\d{2}\s+(?:AM|PM)\s+\d{1,2}:\d{2}",
            r"\d{2}-\d{2}\s+\d{1,2}:\d{2}",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            normalized = normalize_date(match.group(0))
            if normalized:
                return normalized
    return ""


def is_recent(value: str, hours: int) -> bool:
    if not value:
        return True
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if published.tzinfo is None:
        published = published.replace(tzinfo=KST)
    return published.astimezone(KST) >= datetime.now(KST) - timedelta(hours=hours)


def normalize_date(value: str) -> str:
    value = display_text(value)
    if not value:
        return ""
    am_pm_match = re.search(
        r"(\d{4})/(\d{2})/(\d{2})\s+(AM|PM)\s+(\d{1,2}):(\d{2})",
        value,
        flags=re.IGNORECASE,
    )
    if am_pm_match:
        year, month, day, meridiem, hour, minute = am_pm_match.groups()
        hour_value = int(hour)
        if meridiem.upper() == "PM" and hour_value < 12:
            hour_value += 12
        if meridiem.upper() == "AM" and hour_value == 12:
            hour_value = 0
        return datetime(
            int(year),
            int(month),
            int(day),
            hour_value,
            int(minute),
            tzinfo=KST,
        ).isoformat()
    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%m-%d %H:%M",
    ):
        try:
            parsed = datetime.strptime(value, pattern)
            if pattern.startswith("%m"):
                parsed = parsed.replace(year=datetime.now(KST).year)
            return parsed.replace(tzinfo=KST).isoformat()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=KST).isoformat()
        return parsed.astimezone(KST).isoformat()
    except ValueError:
        pass
    return ""


def crawl_source(
    session: requests.Session,
    source: dict[str, Any],
    keywords: list[str],
    blocked_keywords: list[str],
    settings: CrawlSettings,
) -> tuple[list[dict[str, Any]], str | None]:
    active_blocked_keywords = source_blocked_keywords(source, blocked_keywords)
    try:
        candidates = discover_candidates(session, source, keywords, blocked_keywords, settings)
    except Exception as exc:
        return [], f"list error: {exc}"

    items: list[dict[str, Any]] = []
    item_limit = source_item_limit(source, settings)
    crawl_started = datetime.now(KST)
    for position, candidate in enumerate(candidates):
        if len(items) >= item_limit:
            break

        time.sleep(settings.sleep_seconds)
        try:
            detail_html = fetch_text(session, candidate["url"], settings.detail_timeout)
            soup = BeautifulSoup(detail_html, "html.parser")
            published_at = candidate.get("publishedAt") or parse_published_at(soup)
            if not published_at:
                published_at = (crawl_started - timedelta(seconds=position)).isoformat()
            if not is_recent(published_at, settings.hours):
                continue

            image_url = find_image_url(soup, candidate["url"]) or fallback_image_url(source, candidate["url"])
            if not image_url:
                continue
            if has_blocked_keyword(f"{candidate['title']} {candidate['url']} {image_url}", active_blocked_keywords):
                continue

            link = canonical_url(candidate["url"], source)
            items.append(
                {
                    "id": f"{source['id']}:{hashlib.sha1(link.encode('utf-8')).hexdigest()[:12]}",
                    "sourceId": source["id"],
                    "sourceName": source.get("name", source["id"]),
                    "title": candidate["title"],
                    "link": link,
                    "imageUrl": image_url,
                    "publishedAt": published_at,
                }
            )
        except Exception as exc:
            log(f"  - detail skipped: {candidate['title'][:30]} / {exc}")

    return items, None


def build_payload(config: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings(config)
    keywords = config.get("keywords") or []
    blocked_keywords = config.get("blocked_keywords") or []
    sources = [source for source in config.get("sources", []) if source.get("enabled", True)]
    session = make_session()
    all_items: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    replace_source_ids: list[str] = []
    blocked_urls: list[str] = []

    for source in sorted(sources, key=lambda item: item.get("rank", 999)):
        log(f"crawl: {source.get('name', source['id'])}")
        items, error = crawl_source(session, source, keywords, blocked_keywords, settings)
        all_items.extend(items)
        blocked_urls.extend(source_blocked_urls(source))
        if source.get("replace_existing"):
            replace_source_ids.append(source["id"])
        summary = {
            "id": source["id"],
            "name": source.get("name", source["id"]),
            "rank": source.get("rank", 999),
            "enabled": True,
            "count": len(items),
        }
        if error:
            summary["error"] = error
        source_summaries.append(summary)
        log(f"  -> {len(items)} items")
        time.sleep(settings.sleep_seconds)

    all_items = dedupe_items(all_items)
    all_items.sort(key=lambda item: item.get("publishedAt") or "", reverse=True)

    return {
        "generatedAt": datetime.now(KST).isoformat(),
        "sources": source_summaries,
        "items": all_items,
        "_replaceSourceIds": replace_source_ids,
        "_blockedUrls": blocked_urls,
    }


def merge_payloads(existing_payload: dict[str, Any], new_payload: dict[str, Any], settings: CrawlSettings) -> dict[str, Any]:
    active_source_ids = {
        source.get("id")
        for source in new_payload.get("sources", [])
        if isinstance(source, dict) and source.get("id")
    }
    replace_source_ids = {
        source_id
        for source_id in new_payload.get("_replaceSourceIds", [])
        if isinstance(source_id, str)
    }
    blocked_urls = {
        url
        for url in new_payload.get("_blockedUrls", [])
        if isinstance(url, str)
    }
    existing_items = existing_payload.get("items") if isinstance(existing_payload.get("items"), list) else []
    if active_source_ids:
        existing_items = [
            item
            for item in existing_items
            if not isinstance(item, dict)
            or (item.get("sourceId") in active_source_ids and item.get("sourceId") not in replace_source_ids)
        ]
    existing_items = [
        item
        for item in existing_items
        if not isinstance(item, dict) or item.get("link") not in blocked_urls
    ]
    new_items = new_payload.get("items") if isinstance(new_payload.get("items"), list) else []
    new_items = [
        item
        for item in new_items
        if not isinstance(item, dict) or item.get("link") not in blocked_urls
    ]
    items = dedupe_items([*new_items, *existing_items])
    items.sort(key=lambda item: item.get("publishedAt") or "", reverse=True)
    items = items[: settings.max_total_items]

    existing_sources = existing_payload.get("sources") if isinstance(existing_payload.get("sources"), list) else []
    if active_source_ids:
        existing_sources = [
            source
            for source in existing_sources
            if (
                isinstance(source, dict)
                and source.get("id") in active_source_ids
                and source.get("id") not in replace_source_ids
            )
        ]
    sources = merge_source_summaries(
        existing_sources,
        new_payload.get("sources") if isinstance(new_payload.get("sources"), list) else [],
        items,
    )

    payload = {
        **new_payload,
        "sources": sources,
        "items": items,
    }
    payload.pop("_replaceSourceIds", None)
    payload.pop("_blockedUrls", None)
    return payload


def merge_source_summaries(
    existing_sources: list[dict[str, Any]],
    new_sources: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        source_id = item.get("sourceId")
        if source_id:
            counts[source_id] = counts.get(source_id, 0) + 1

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in [*new_sources, *existing_sources]:
        source_id = source.get("id")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        merged.append({**source, "count": counts.get(source_id, 0)})

    return merged


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = item.get("link") or item.get("imageUrl")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def upload_to_kv(payload: dict[str, Any], key: str) -> None:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    namespace_id = os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID", "").strip()
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()

    missing = [
        name
        for name, value in {
            "CLOUDFLARE_ACCOUNT_ID": account_id,
            "CLOUDFLARE_KV_NAMESPACE_ID": namespace_id,
            "CLOUDFLARE_API_TOKEN": api_token,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/storage/kv/namespaces/{namespace_id}/values/{quote(key, safe='')}"
    )
    response = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"KV upload failed: HTTP {response.status_code} {response.text[:500]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl public board links and upload a thumbnail feed to Cloudflare KV.")
    parser.add_argument("--config", default="crawler/board_sources.json")
    parser.add_argument("--out", default="crawler/feed.latest.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_json(config_path)
    settings = load_settings(config)
    new_payload = build_payload(config)

    out_path = Path(args.out)
    existing_payload = load_existing_payload(out_path)
    payload = merge_payloads(existing_payload, new_payload, settings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(
        f"wrote: {out_path} "
        f"({len(new_payload['items'])} new, {len(payload['items'])} total)"
    )

    if args.dry_run or args.skip_upload:
        return 0

    key = os.environ.get("KV_KEY") or config.get("settings", {}).get("kv_key", "feed:latest")
    upload_to_kv(payload, key)
    log(f"uploaded to KV key: {key}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
