#!/usr/bin/env python3
"""Lag en samlet RSS-feed fra offentlige kilder om Orkdal sjukehus."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from docx import Document
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
ITEMS_FILE = DATA_DIR / "items.json"
SNAPSHOTS_FILE = DATA_DIR / "snapshots.json"
STATUS_FILE = DOCS_DIR / "status.json"


@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    source_id: str
    published: str
    discovered: str
    summary: str
    matched_terms: list[str]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(value or ""))
    return " ".join(value.casefold().split())


def clean_text(value: str, limit: int = 5000) -> str:
    value = value or ""
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return " ".join(value.split())[:limit]


def clean_url(value: str) -> str:
    parts = urlsplit(value)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def stable_id(source_id: str, url: str, suffix: str = "") -> str:
    raw = f"{source_id}\n{clean_url(url)}\n{suffix}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    haystack = normalized(text)
    return [term for term in keywords if normalized(term) in haystack]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Monitor:
    def __init__(self, config: dict[str, Any], dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        opts = config["monitor"]
        self.timeout = int(opts.get("timeout_seconds", 30))
        self.max_bytes = int(opts.get("max_document_mb", 15)) * 1024 * 1024
        self.max_candidates = int(opts.get("max_candidates_per_source", 80))
        self.keywords = list(config["keywords"])
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": opts["user_agent"],
            "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.5",
        })
        self.snapshots = load_json(SNAPSHOTS_FILE, {})

    def fetch(self, url: str) -> requests.Response:
        response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        response.raise_for_status()
        length = int(response.headers.get("content-length", 0) or 0)
        if length > self.max_bytes:
            raise ValueError(f"Dokumentet er for stort ({length} byte)")
        if len(response.content) > self.max_bytes:
            raise ValueError(f"Dokumentet er for stort ({len(response.content)} byte)")
        return response

    def document_text(self, url: str, response: requests.Response | None = None) -> tuple[str, str, str]:
        response = response or self.fetch(url)
        ctype = response.headers.get("content-type", "").lower()
        path = urlsplit(str(response.url)).path.lower()
        title = ""
        published = ""
        if "pdf" in ctype or path.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(response.content))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            title = str((reader.metadata or {}).get("/Title") or Path(path).name)
            return clean_text(text, 200_000), clean_text(title, 300), published
        if "wordprocessingml" in ctype or path.endswith(".docx"):
            doc = Document(io.BytesIO(response.content))
            text = "\n".join(p.text for p in doc.paragraphs)
            return clean_text(text, 200_000), Path(path).name, published
        soup = BeautifulSoup(response.text, "html.parser")
        node = soup.find("h1") or soup.find("title")
        title = clean_text(node.get_text(" ") if node else Path(path).name, 300)
        meta = soup.find("meta", attrs={"property": "article:published_time"})
        if meta and meta.get("content"):
            published = str(meta["content"])
        if not published:
            t = soup.find("time")
            if t and t.get("datetime"):
                published = str(t["datetime"])
        return clean_text(soup.get_text(" "), 200_000), title, published

    def item_from_url(self, source: dict[str, Any], url: str, link_title: str = "") -> Item | None:
        response = self.fetch(url)
        text, page_title, published = self.document_text(url, response)
        matches = keyword_matches(f"{link_title} {page_title} {url} {text}", self.keywords)
        if not matches:
            return None
        title = page_title or link_title or Path(urlsplit(url).path).name
        summary = text[:700]
        return Item(stable_id(source["id"], url), title, clean_url(str(response.url)), source["name"],
                    source["id"], published or now_iso(), now_iso(), summary, matches)

    def html_links(self, source: dict[str, Any]) -> list[Item]:
        response = self.fetch(source["url"])
        soup = BeautifulSoup(response.text, "html.parser")
        patterns = [re.compile(p, re.I) for p in source.get("link_patterns", [])]
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for link in soup.find_all("a", href=True):
            url = clean_url(urljoin(str(response.url), str(link["href"])))
            if urlsplit(url).scheme not in {"http", "https"}:
                continue
            label = clean_text(link.get_text(" "), 500)
            if patterns and not any(p.search(url) or p.search(label) for p in patterns):
                continue
            if url in seen:
                continue
            seen.add(url)
            candidates.append((url, label))
        items: list[Item] = []
        for url, label in candidates[: self.max_candidates]:
            inline_matches = keyword_matches(f"{label} {url}", self.keywords)
            if not source.get("fetch_content", False):
                if inline_matches:
                    items.append(Item(stable_id(source["id"], url), label or url, url, source["name"],
                                      source["id"], now_iso(), now_iso(), label, inline_matches))
                continue
            try:
                item = self.item_from_url(source, url, label)
                if item:
                    items.append(item)
            except Exception as exc:  # én ødelagt PDF skal ikke stoppe kilden
                print(f"WARN {source['id']} dokument {url}: {exc}", file=sys.stderr)
            time.sleep(0.05)
        return items

    def google_news(self, source: dict[str, Any]) -> list[Item]:
        url = "https://news.google.com/rss/search?q=" + quote_plus(source["query"]) + "&hl=no&gl=NO&ceid=NO:no"
        result_terms = source.get("result_terms", self.keywords)
        response = self.fetch(url)
        parsed = feedparser.parse(response.content)
        items: list[Item] = []
        for entry in parsed.entries[: self.max_candidates]:
            title = clean_text(entry.get("title", ""), 500)
            summary = clean_text(entry.get("summary", ""), 1000)
            link = clean_url(entry.get("link", ""))
            matches = keyword_matches(f"{title} {summary}", result_terms)
            # Google kan tolke OR-søk bredt. Bare eksplisitte Orkdal-treff slipper gjennom.
            if not matches:
                continue
            published = entry.get("published", now_iso())
            items.append(Item(stable_id(source["id"], link), title, link, source["name"], source["id"],
                              published, now_iso(), summary, matches))
        return items

    def page_snapshot(self, source: dict[str, Any]) -> list[Item]:
        response = self.fetch(source["url"])
        text, title, published = self.document_text(source["url"], response)
        matches = keyword_matches(text, self.keywords)
        if source.get("require_keyword", True) and not matches:
            return []
        digest = hashlib.sha256(normalized(text).encode()).hexdigest()
        old = self.snapshots.get(source["id"])
        self.snapshots[source["id"]] = {"hash": digest, "checked": now_iso(), "url": source["url"]}
        if not old and not source.get("alert_on_first", False):
            return []
        if old and old.get("hash") == digest:
            return []
        label = "Første måling" if not old else "Kilden er endret"
        return [Item(stable_id(source["id"], source["url"], digest), f"{label}: {title}",
                     clean_url(str(response.url)), source["name"], source["id"], published or now_iso(),
                     now_iso(), text[:700], matches or ["endringskontroll"])]

    def run_source(self, source: dict[str, Any]) -> list[Item]:
        kind = source["type"]
        if kind == "html_links":
            items = self.html_links(source)
            if not items and source.get("fallback_query"):
                fallback = dict(source)
                fallback["query"] = source["fallback_query"]
                items = self.google_news(fallback)
            return items
        if kind == "google_news":
            return self.google_news(source)
        if kind == "page_snapshot":
            return self.page_snapshot(source)
        raise ValueError(f"Ukjent kildetype: {kind}")

    def run(self) -> tuple[list[Item], list[dict[str, Any]]]:
        all_items: list[Item] = []
        statuses: list[dict[str, Any]] = []
        for source in self.config["sources"]:
            started = time.monotonic()
            try:
                items = self.run_source(source)
                all_items.extend(items)
                statuses.append({"id": source["id"], "name": source["name"], "ok": True,
                                 "items": len(items), "checked": now_iso(),
                                 "seconds": round(time.monotonic() - started, 2)})
                print(f"OK   {source['id']}: {len(items)} treff")
            except Exception as exc:
                statuses.append({"id": source["id"], "name": source["name"], "ok": False,
                                 "items": 0, "checked": now_iso(), "error": str(exc),
                                 "seconds": round(time.monotonic() - started, 2)})
                print(f"FEIL {source['id']}: {exc}", file=sys.stderr)
        if not self.dry_run:
            save_json(SNAPSHOTS_FILE, self.snapshots)
        return all_items, statuses


def parse_date(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return datetime.now(timezone.utc)


def merge_items(config: dict[str, Any], fresh: list[Item]) -> list[Item]:
    existing = {row["id"]: row for row in load_json(ITEMS_FILE, [])}
    first_run = not bool(existing)
    if first_run:
        limit = int(config["feed"].get("initial_items_per_source", 8))
        selected: list[Item] = []
        counts: dict[str, int] = {}
        for item in sorted(fresh, key=lambda i: parse_date(i.published), reverse=True):
            counts[item.source_id] = counts.get(item.source_id, 0)
            if counts[item.source_id] < limit:
                selected.append(item)
                counts[item.source_id] += 1
        fresh = selected
    for item in fresh:
        row = asdict(item)
        if item.id in existing:
            row["discovered"] = existing[item.id].get("discovered", item.discovered)
        existing[item.id] = row
    rows = list(existing.values())
    rows.sort(key=lambda r: parse_date(r.get("published") or r.get("discovered", "")), reverse=True)
    return [Item(**row) for row in rows[: int(config["feed"].get("max_items", 250))]]


def build_rss(config: dict[str, Any], items: list[Item]) -> bytes:
    feed = config["feed"]
    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = feed["title"]
    ET.SubElement(channel, "link").text = feed["site_url"]
    ET.SubElement(channel, "description").text = feed["description"]
    ET.SubElement(channel, "language").text = "nb-NO"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    for item in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = f"[{item.source}] {item.title}"
        ET.SubElement(node, "link").text = item.url
        ET.SubElement(node, "guid", {"isPermaLink": "false"}).text = item.id
        ET.SubElement(node, "pubDate").text = format_datetime(parse_date(item.published))
        terms = ", ".join(item.matched_terms)
        ET.SubElement(node, "description").text = f"Treff: {terms}\n\n{item.summary}"
        ET.SubElement(node, "category").text = item.source
    ET.indent(rss, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8") + b"\n"


def build_dashboard(config: dict[str, Any], items: list[Item], statuses: list[dict[str, Any]]) -> str:
    rows = []
    for item in items[:50]:
        rows.append(f"<tr><td>{html.escape(item.published[:10])}</td><td>{html.escape(item.source)}</td>"
                    f"<td><a href=\"{html.escape(item.url, quote=True)}\">{html.escape(item.title)}</a></td>"
                    f"<td>{html.escape(', '.join(item.matched_terms))}</td></tr>")
    cards = []
    for status in statuses:
        cls = "ok" if status["ok"] else "error"
        detail = f"{status['items']} treff" if status["ok"] else status.get("error", "Ukjent feil")
        cards.append(f"<li class=\"{cls}\"><strong>{html.escape(status['name'])}</strong>: {html.escape(str(detail))}</li>")
    return f"""<!doctype html><html lang=\"nb\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>{html.escape(config['feed']['title'])}</title><style>body{{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:.55rem;text-align:left;vertical-align:top}}.ok::marker{{color:#16803a}}.error{{color:#a11}}a{{color:#075ea8}}</style></head><body><h1>{html.escape(config['feed']['title'])}</h1><p><a href=\"orkdal-sjukehus.xml\">Abonner på RSS-feeden</a> · Sist kontrollert {html.escape(now_iso())}</p><h2>Kildestatus</h2><ul>{''.join(cards)}</ul><h2>Siste treff</h2><table><thead><tr><th>Dato</th><th>Kilde</th><th>Treff</th><th>Søkeord</th></tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = yaml.safe_load((ROOT / args.config).read_text(encoding="utf-8"))
    monitor = Monitor(config, dry_run=args.dry_run)
    fresh, statuses = monitor.run()
    if args.dry_run:
        print(f"Totalt {len(fresh)} treff; ingen filer skrevet")
        return 0 if any(s["ok"] for s in statuses) else 1
    items = merge_items(config, fresh)
    save_json(ITEMS_FILE, [asdict(i) for i in items])
    save_json(STATUS_FILE, {"checked": now_iso(), "sources": statuses})
    feed_path = ROOT / config["feed"]["feed_path"]
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    feed_path.write_bytes(build_rss(config, items))
    (DOCS_DIR / "index.html").write_text(build_dashboard(config, items, statuses), encoding="utf-8")
    return 0 if any(s["ok"] for s in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
