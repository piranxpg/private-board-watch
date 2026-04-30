from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


KST = timezone(timedelta(hours=9))
FALLBACK_IMAGE_URL = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'%3E"
    "%3Crect width='640' height='360' fill='%2322292f'/%3E"
    "%3Ctext x='320' y='196' text-anchor='middle' font-family='Arial,sans-serif' "
    "font-size='32' fill='%23d8dee6'%3ELINK%3C/text%3E%3C/svg%3E"
)


@dataclass(frozen=True)
class DogdripSource:
    id: str
    name: str
    rank: float
    mid: str
    url: str
    max_items: int = 24


SOURCES = (
    DogdripSource("dogdrip-dogdrip", "개드립 개드립게시판", 7.85, "dogdrip", "https://www.dogdrip.net/dogdrip"),
    DogdripSource("dogdrip-userdog", "개드립 유저개드립게시판", 7.9, "userdog", "https://www.dogdrip.net/userdog"),
)
EXTRA_KEYWORDS = ["후방", "약후", "ㅇㅎ", "ㅎㅂ", "nsfw", "비키니", "몸매", "눈나", "호불호", "불호", "처자"]


def log(message: str) -> None:
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(value or "")).lower()


def has_keyword(value: str, keywords: list[str]) -> bool:
    clean = normalize_text(value)
    return any(normalize_text(keyword) in clean for keyword in keywords)


def has_blocked_keyword(value: str, keywords: list[str]) -> bool:
    clean = normalize_text(value)
    return any(normalize_text(keyword) in clean for keyword in keywords)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        }
    )
    return session


def fetch_text(session: requests.Session, url: str, timeout: int) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text


def parse_relative_date(value: str) -> str:
    value = clean_text(value)
    patterns = (
        (r"방금\s*전", timedelta(seconds=0)),
        (r"(\d+)\s*초\s*전", "seconds"),
        (r"(\d+)\s*분\s*전", "minutes"),
        (r"(\d+)\s*시간\s*전", "hours"),
        (r"(\d+)\s*일\s*전", "days"),
    )
    for pattern, unit in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        delta = unit if isinstance(unit, timedelta) else timedelta(**{unit: int(match.group(1))})
        return (datetime.now(KST) - delta).isoformat()

    match = re.search(r"(\d{1,2})[-.](\d{1,2})\s+(\d{1,2}):(\d{2})", value)
    if match:
        month, day, hour, minute = (int(part) for part in match.groups())
        try:
            return datetime(datetime.now(KST).year, month, day, hour, minute, tzinfo=KST).isoformat()
        except ValueError:
            return ""
    return ""


def source_urls(source: DogdripSource, keywords: list[str]) -> list[tuple[str, bool]]:
    urls: list[tuple[str, bool]] = [(source.url, False), (f"{source.url}?page=2", False)]
    for keyword in keywords:
        encoded = quote(keyword)
        urls.append(
            (
                f"https://www.dogdrip.net/index.php?mid={source.mid}&search_target=title&search_keyword={encoded}",
                True,
            )
        )
    return urls


def collect_source(
    session: requests.Session,
    source: DogdripSource,
    keywords: list[str],
    blocked_keywords: list[str],
    timeout: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    now = datetime.now(KST).isoformat()

    for url, trusted_search in source_urls(source, keywords):
        try:
            soup = BeautifulSoup(fetch_text(session, url, timeout), "html.parser")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue

        for anchor in soup.select("a.ed.title-link[data-document-srl]"):
            title = clean_text(anchor.get_text(" ", strip=True))
            document_id = anchor.get("data-document-srl") or ""
            if not title or not document_id:
                continue
            if not trusted_search and not has_keyword(title, keywords):
                continue
            if has_blocked_keyword(title, blocked_keywords):
                continue
            link = f"https://www.dogdrip.net/{document_id}"
            if link in seen:
                continue

            row = anchor.find_parent("li")
            published_at = parse_relative_date(row.get_text(" ", strip=True) if row else "")
            image_url = ""
            image = row.select_one("img.webzine-thumbnail") if row else None
            if image:
                image_url = urljoin(url, image.get("src") or "")

            seen.add(link)
            items.append(
                {
                    "id": f"{source.id}:{hashlib.sha1(link.encode('utf-8')).hexdigest()[:12]}",
                    "sourceId": source.id,
                    "sourceName": source.name,
                    "title": title,
                    "link": link,
                    "imageUrl": image_url or FALLBACK_IMAGE_URL,
                    "publishedAt": published_at or now,
                    "collectedAt": now,
                    "dateSource": "parsed" if published_at else "collected",
                }
            )
            if len(items) >= source.max_items:
                break
        if len(items) >= source.max_items:
            break

    summary: dict[str, Any] = {"id": source.id, "name": source.name, "rank": source.rank, "enabled": True, "count": len(items)}
    if errors and not items:
        summary["error"] = "; ".join(errors[:3])
    return items, summary


def sort_items(items: list[dict[str, Any]]) -> None:
    items.sort(key=lambda item: (item.get("publishedAt") or item.get("collectedAt") or "", item.get("collectedAt") or ""), reverse=True)


def merge(feed: dict[str, Any], new_items: list[dict[str, Any]], summaries: list[dict[str, Any]], max_total: int) -> dict[str, Any]:
    source_ids = {source.id for source in SOURCES}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*new_items, *(feed.get("items") or [])]:
        if not isinstance(item, dict):
            continue
        key = item.get("link") or item.get("imageUrl") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    sort_items(merged)
    merged = merged[:max_total]

    counts: dict[str, int] = {}
    for item in merged:
        source_id = item.get("sourceId")
        if source_id:
            counts[source_id] = counts.get(source_id, 0) + 1

    sources = [source for source in feed.get("sources", []) if isinstance(source, dict) and source.get("id") not in source_ids]
    for summary in summaries:
        sources.append({**summary, "count": counts.get(summary["id"], 0)})
    sources.sort(key=lambda source: source.get("rank", 999))
    return {**feed, "generatedAt": datetime.now(KST).isoformat(), "sources": sources, "items": merged}


def upload_to_kv(payload: dict[str, Any], key: str) -> None:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    namespace_id = os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    missing = [name for name, value in {"CLOUDFLARE_ACCOUNT_ID": account_id, "CLOUDFLARE_KV_NAMESPACE_ID": namespace_id, "CLOUDFLARE_API_TOKEN": token}.items() if not value]
    if missing:
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{quote(key, safe='')}"
    response = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"KV upload failed: HTTP {response.status_code} {response.text[:500]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="crawler/board_sources.json")
    parser.add_argument("--feed", default="crawler/feed.latest.json")
    parser.add_argument("--skip-upload", action="store_true")
    args = parser.parse_args()

    config = load_json(Path(args.config))
    keywords = list(dict.fromkeys([*(config.get("keywords") or []), *EXTRA_KEYWORDS]))
    blocked_keywords = config.get("blocked_keywords") or []
    settings = config.get("settings", {})
    timeout = int(settings.get("request_timeout", 15) or 15)
    max_total = int(settings.get("max_total_items", 500) or 500)
    session = make_session()

    items: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for source in SOURCES:
        source_items, summary = collect_source(session, source, keywords, blocked_keywords, timeout)
        items.extend(source_items)
        summaries.append(summary)
        log(f"{source.id}: {len(source_items)} items")

    feed_path = Path(args.feed)
    feed = load_json(feed_path) if feed_path.exists() else {}
    payload = merge(feed, items, summaries, max_total)
    feed_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.skip_upload:
        key = os.environ.get("KV_KEY") or settings.get("kv_key", "feed:latest")
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
