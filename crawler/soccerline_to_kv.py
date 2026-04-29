from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES


KST = timezone(timedelta(hours=9))
SOURCE_ID = "soccerline-locker"
BASE_URL = "https://soccerline.kr/board"
FALLBACK_IMAGE_URL = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'%3E%3Crect width='640' height='360' fill='%2322292f'/%3E%3Ctext x='320' y='196' text-anchor='middle' font-family='Arial,sans-serif' font-size='32' fill='%23d8dee6'%3ELINK%3C/text%3E%3C/svg%3E"


def log(message: str) -> None:
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def compact(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(value or "")).lower()


def text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def blocked(title: str, keywords: list[str]) -> bool:
    clean = compact(title)
    return any(compact(keyword) in clean for keyword in keywords)


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


def decode_payload(encoded: str) -> dict[str, Any]:
    encoded = re.sub(r"\s+", "", encoded or "")
    key = hashlib.pbkdf2_hmac("sha1", b"x7mQ9pL2vN4kY8jR", bytes.fromhex("A1B2C3D4E5F67890"), 2, dklen=32)
    plaintext = AES.new(key, AES.MODE_CBC, bytes.fromhex(encoded[:32])).decrypt(bytes.fromhex(encoded[32:]))
    pad = plaintext[-1]
    if 1 <= pad <= AES.block_size:
        plaintext = plaintext[:-pad]
    return json.loads(plaintext.decode("utf-8"))


def articles_from_page(session: requests.Session, url: str, timeout: int) -> list[dict[str, Any]]:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    try:
        markup = response.content.decode("utf-8")
    except UnicodeDecodeError:
        response.encoding = response.apparent_encoding or "utf-8"
        markup = response.text
    soup = BeautifulSoup(markup, "html.parser")
    tag = soup.select_one('script#articles[type="text/json-soccerline-encoded"]')
    if not tag:
        return []
    payload = decode_payload(tag.get_text("", strip=True))
    articles = payload.get("content") if isinstance(payload, dict) else []
    return [article for article in articles if isinstance(article, dict)]


def parse_date(value: str) -> str:
    value = text(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST).isoformat()


def recent(value: str, hours: int) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST) >= datetime.now(KST) - timedelta(hours=hours)


def source_config(config: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in config.get("sources", []) if item.get("id") == SOURCE_ID), None)


def collect(config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = source_config(config)
    if not source or source.get("enabled") is False:
        return [], {"id": SOURCE_ID, "name": SOURCE_ID, "rank": 999, "enabled": False, "count": 0}
    settings = config.get("settings", {})
    keywords = source.get("search_keywords") or source.get("keywords") or config.get("keywords") or []
    deny = [*(config.get("blocked_keywords") or []), *(source.get("blocked_keywords") or [])]
    timeout = int(source.get("request_timeout", settings.get("request_timeout", 15)) or 15)
    hours = int(source.get("hours", settings.get("hours", 12)) or 12)
    limit = int(source.get("max_items_per_source", settings.get("max_items_per_source", 12)) or 12)
    template = source.get("search_url_template") or "https://soccerline.kr/board?categoryDepth01=5&page=0&searchText={keyword}&searchType=0&searchWindow="
    session = make_session()
    now = datetime.now(KST).isoformat()
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    errors: list[str] = []

    for keyword in keywords:
        url = str(template).replace("{keyword}", quote(str(keyword)))
        try:
            articles = articles_from_page(session, url, timeout)
        except Exception as exc:
            errors.append(f"{keyword}: {exc}")
            continue
        for article in articles:
            title = text(str(article.get("subject") or ""))
            article_id = article.get("idx")
            if not title or not article_id or blocked(title, deny):
                continue
            published_at = parse_date(str(article.get("writeDate") or ""))
            if published_at and not recent(published_at, hours):
                continue
            link = f"{BASE_URL}/{article_id}?categoryDepth01=5"
            if link in seen:
                continue
            seen.add(link)
            items.append(
                {
                    "id": f"{SOURCE_ID}:{hashlib.sha1(link.encode('utf-8')).hexdigest()[:12]}",
                    "sourceId": SOURCE_ID,
                    "sourceName": source.get("name", SOURCE_ID),
                    "title": title,
                    "link": link,
                    "imageUrl": FALLBACK_IMAGE_URL,
                    "publishedAt": published_at or now,
                    "collectedAt": now,
                    "dateSource": "parsed" if published_at else "collected",
                }
            )
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break

    summary = {
        "id": SOURCE_ID,
        "name": source.get("name", SOURCE_ID),
        "rank": source.get("rank", 999),
        "enabled": True,
        "count": len(items),
    }
    if errors and not items:
        summary["error"] = "; ".join(errors[:3])
    return items, summary


def sort_items(items: list[dict[str, Any]]) -> None:
    items.sort(key=lambda item: (item.get("publishedAt") or item.get("collectedAt") or "", item.get("collectedAt") or ""), reverse=True)


def merge(feed: dict[str, Any], items: list[dict[str, Any]], summary: dict[str, Any], max_total: int) -> dict[str, Any]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*items, *(feed.get("items") or [])]:
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
    sources = [source for source in feed.get("sources", []) if isinstance(source, dict) and source.get("id") != SOURCE_ID]
    sources.append({**summary, "count": counts.get(SOURCE_ID, 0)})
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
    feed_path = Path(args.feed)
    feed = load_json(feed_path) if feed_path.exists() else {}
    items, summary = collect(config)
    payload = merge(feed, items, summary, int(config.get("settings", {}).get("max_total_items", 500) or 500))
    feed_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"soccerline: {len(items)} items")
    if not args.skip_upload:
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
