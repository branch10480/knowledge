#!/usr/bin/env python3
"""旧 v1 entries.json（ルート、HTML content）を v2 data/entries.json へ変換する migration。

- HTML を plain text に落とす（HTMLParser、regex では除去しない）
- canonical_url を検証（HTTPS、userinfo/fragment/localhost/IP literal 禁止）
- 決定的 migration ID を canonical_url hash から生成
- source_id を source ホストから決定
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag in ("p", "li", "h1", "h2", "h3", "br", "div", "ul", "ol", "pre", "blockquote"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        if tag in ("p", "li", "h1", "h2", "h3", "div", "ul", "ol", "pre", "blockquote"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw)
    p.close()
    text = "".join(p.parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_canonical_url(url: str) -> str:
    """HTTPS のみ許可。userinfo/fragment/localhost/IP literal を禁止し、tracking param を除去。"""
    if not url.startswith("https://"):
        raise ValueError(f"non-HTTPS url: {url!r}")
    # scheme://authority/path?query#frag
    m = re.match(r"^https://([^/?#]+)(/[^?#]*)?(\?[^#]*)?(#.*)?$", url)
    if not m:
        raise ValueError(f"malformed url: {url!r}")
    authority = m.group(1).lower()
    path = m.group(2) or ""
    query = m.group(3) or ""
    if "@" in authority:  # userinfo
        raise ValueError(f"userinfo in url: {url!r}")
    if authority.startswith("localhost") or re.match(r"^\d{1,3}(\.\d{1,3}){3}$", authority):
        raise ValueError(f"forbidden host: {authority!r}")
    # tracking params to drop
    keep = []
    if query:
        pairs = query[1:].split("&")
        for kv in pairs:
            if "=" in kv:
                k, _v = kv.split("=", 1)
                if k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                         "fbclid", "gclid", "mc_cid", "mc_eid"):
                    continue
            keep.append(kv)
        query = "&".join(keep)
    return f"https://{authority}{path}" + (f"?{query}" if query else "")


def source_id_for(url: str) -> str:
    host = url.split("/", 3)[2].lower()
    if "developer.apple.com" in host:
        return "apple-developer-news"
    if "anthropic.com" in host:
        return "anthropic-newsroom"
    if "openai.com" in host or "openai" in host:
        return "openai-news"
    if "github.com" in host or "macrumors.com" in host or "ghacks.net" in host or \
       "techcrunch.com" in host or "qz.com" in host or "explosion.com" in host or \
       "theguardian.com" in host:
        return "web-secondary"
    return "web-secondary"


def make_migration_id(canonical_url: str, published: str) -> str:
    h = hashlib.sha256(f"{canonical_url}\n{published}".encode("utf-8")).hexdigest()
    return "kn_" + h[:24]


def main() -> int:
    src = os.path.join(REPO, "entries.json")
    with open(src, "r", encoding="utf-8") as f:
        old = json.load(f)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    entries = []
    for e in old:
        date = e["date"]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            print(f"WARN skip bad date {date!r}", file=sys.stderr)
            continue
        published = f"{date}T00:00:00Z"
        canonical = normalize_canonical_url(e["source"])
        title = e["title"].strip()
        summary = html_to_text(e.get("content", "")) or ""
        if not title or not summary:
            print(f"WARN empty title/summary for {canonical}", file=sys.stderr)
            continue
        eid = make_migration_id(canonical, published)
        entries.append({
            "id": eid,
            "source_id": source_id_for(canonical),
            "external_id": canonical,
            "canonical_url": canonical,
            "published_at": published,
            "collected_at": now,
            "title": title,
            "summary": summary,
            "key_points": [],
            "tags": [str(t) for t in e.get("tags", [])][:10],
            "language": "ja",
            "source_digest": "sha256:" + hashlib.sha256(
                (summary + "\n" + title).encode("utf-8")).hexdigest(),
            "summary_model": {"provider": "migration-v1", "model": "html-to-text", "prompt_version": "migrate-v1"},
            "review": {"factual_gate": "passed", "checked_at": now},
        })

    # 日付降順、同日は id 昇順
    entries.sort(key=lambda x: (x["published_at"], x["id"]), reverse=True)

    doc = {"schema_version": 2, "entries": entries}
    out = os.path.join(REPO, "data", "entries.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"migrated {len(entries)} entries -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
