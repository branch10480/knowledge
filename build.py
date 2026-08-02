#!/usr/bin/env python3
"""
entries.json + template.html から index.html を生成するビルドスクリプト。
P2: RSS/Atom フィード, 個別エントリページ, 月別アーカイブ, 関連エントリ表示に対応.
"""
from __future__ import annotations
import json
import re
import os
from html import escape
from datetime import date

ALLOWED_URL_SCHEMES = ("http://", "https://")
BASE_URL = "https://branch10480.github.io/knowledge"


# ── Markdown → HTML ───────────────────────────────────────────────

def _is_html(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<p") or stripped.startswith("<ul") or stripped.startswith("<h")


def md_to_html(text: str) -> str:
    lines = text.split("\n")
    out = []
    in_list = False
    for line in lines:
        m = re.match(r'^(#{1,3})\s+(.*)', line)
        if m:
            level = len(m.group(1))
            content = escape(m.group(2))
            out.append(f"<h{level}>{content}</h{level}>")
            continue
        m = re.match(r'^[-*]\s+(.*)', line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = escape(m.group(1))
            out.append(f"<li>{content}</li>")
            continue
        if in_list and line.strip() == "":
            out.append("</ul>")
            in_list = False
            continue
        if in_list and not re.match(r'^[-*]\s', line):
            out.append("</ul>")
            in_list = False
        line_escaped = escape(line)
        line_escaped = re.sub(
            r'\[([^\]]+)\]\(([^)]+)\)',
            lambda m: _safe_link(m.group(1), m.group(2)),
            line_escaped
        )
        line_escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', line_escaped)
        if line.strip() == "":
            out.append("<br>")
        else:
            out.append(f"<p>{line_escaped}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _safe_link(text: str, url: str) -> str:
    text_escaped = escape(text)
    url_stripped = url.strip()
    if url_stripped.startswith(ALLOWED_URL_SCHEMES):
        url_escaped = escape(url_stripped, quote=True)
        return f'<a href="{url_escaped}">{text_escaped}</a>'
    else:
        return f"{text_escaped}({escape(url_stripped)})"


# ── Slug 生成 ─────────────────────────────────────────────────────

def make_slug(title: str, date_str: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\-_]', '', title)
    slug = slug.lower()[:80]
    return f"{date_str}-{slug}" if date_str else "entry"


# ── エントリレンダリング（共通）────────────────────────────────────

def render_entry_html(entry: dict, show_related: bool = False, related_entries: list | None = None) -> str:
    date_str = entry["date"]
    title = escape(entry["title"])
    tags = entry.get("tags", [])
    content_raw = entry["content"]
    source = entry.get("source", "")

    if _is_html(content_raw):
        content_html = content_raw.strip()
    else:
        content_html = md_to_html(content_raw)

    tag_html = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags)
    source_html = ""
    if source:
        src_escaped = escape(source, quote=True)
        source_html = f'<p class="source">\u51fa\u5178: <a href="{src_escaped}">{src_escaped}</a></p>'

    related_html = ""
    if show_related and related_entries:
        related_items = []
        for r in related_entries:
            r_title = escape(r["title"])
            r_date = r["date"]
            r_slug = make_slug(r["title"], r_date)
            related_items.append(f'<li><a href="entry/{r_slug}.html">{r_title}</a> <span class="date">({r_date})</span></li>')
        if related_items:
            related_html = f'''
    <div class="related-entries">
      <h3>\u95a2\u9023\u30a8\u30f3\u30c8\u30ea</h3>
      <ul>{"".join(related_items)}</ul>
    </div>'''

    return f"""
    <div class="entry">
      <h2><a href="entry/{make_slug(entry['title'], date_str)}.html">{title}</a></h2>
      <p class="date">{date_str} {tag_html}</p>
      {content_html}
      {source_html}
      {related_html}
      <hr>
    </div>"""


# ── RSS/Atom フィード生成 ─────────────────────────────────────────

def generate_atom(entries: list) -> str:
    feed_title = "Knowledge"
    feed_link = BASE_URL
    now = date.today().isoformat()
    items = []
    for e in entries:
        title = escape(e["title"])
        link = f'{BASE_URL}/entry/{make_slug(e["title"], e["date"])}.html'
        content = e["content"] if _is_html(e["content"]) else md_to_html(e["content"])
        items.append(f"""
  <item>
    <title>{escape(e['title'])}</title>
    <link>{link}</link>
    <guid>{link}</guid>
    <pubDate>{_to_rfc822(e['date'])}</pubDate>
    <description>{content}</description>
  </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>{escape(feed_title)}</title>
  <link href="{feed_link}"/>
  <updated>{now}T00:00:00+09:00</updated>
  <id>{feed_link}/</id>
  <author><name>branch10480</name></author>
  {"".join(items)}
</feed>"""


def _to_rfc822(date_str: str) -> str:
    months = {"01":"Jan","02":"Feb","03":"Mar","04":"Apr","05":"May","06":"Jun",
              "07":"Jul","08":"Aug","09":"Sep","10":"Oct","11":"Nov","12":"Dec"}
    parts = date_str.split("-")
    if len(parts) == 3:
        return f"{parts[2]} {months.get(parts[1], '')} {parts[0]} 00:00:00 +0000"
    return date_str


# ── 月別アーカイブページ生成 ───────────────────────────────────────

def generate_archive_page(month: str, entries: list) -> str:
    month_title = f"{month} のエントリ"
    items = []
    for e in entries:
        title = escape(e["title"])
        slug = make_slug(e["title"], e["date"])
        tags = "".join(f'<span class="tag">{escape(t)}</span>' for t in e.get("tags", []))
        summary = e["content"][:200] if _is_html(e["content"]) else e["content"][:200]
        items.append(f"""
    <div class="entry">
      <h3><a href="entry/{slug}.html">{title}</a></h3>
      <p class="date">{e['date']} {tags}</p>
      <p>{summary}...</p>
      <hr>
    </div>""")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(month_title)} - Knowledge</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'SF Mono', sans-serif; max-width: 800px; margin: 0 auto; padding: 2rem 1rem; background: #1a1a1a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #c0c0ff; border-bottom: 1px solid #333; }}
h2 {{ color: #a0a0ee; margin-top: 2em; }}
h3 {{ color: #9090dd; }}
a {{ color: #8888ff; }}
.date {{ color: #888; font-size: 0.9em; }}
.tag {{ display: inline-block; background: #333; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; margin-right: 4px; }}
.entry {{ margin: 1.5em 0; padding: 1em; background: #222; border-radius: 8px; }}
.source {{ color: #777; font-size: 0.85em; }}
@media (prefers-color-scheme: light) {{ body {{ background: #f5f5f5; color: #222; }} h1 {{ color: #4444aa; }} a {{ color: #4444cc; }} .entry {{ background: #fff; }} .tag {{ background: #e0e0e0; color: #333; }} }}
</style>
</head>
<body>
<h1><a href="../index.html" style="color:#c0c0ff">\U0001f4da Knowledge</a> / {escape(month_title)}</h1>
<main>
{"".join(items)}
</main>
</body>
</html>"""


# ── 個別エントリページ生成 ─────────────────────────────────────────

def generate_single_page(entry: dict, related_entries: list) -> str:
    title = escape(entry["title"])
    date_str = entry["date"]
    tags = entry.get("tags", [])
    content_raw = entry["content"]
    source = entry.get("source", "")

    if _is_html(content_raw):
        content_html = content_raw.strip()
    else:
        content_html = md_to_html(content_raw)

    tag_html = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags)
    source_html = ""
    if source:
        src_escaped = escape(source, quote=True)
        source_html = f'<p class="source">\u51fa\u5178: <a href="{src_escaped}">{src_escaped}</a></p>'

    related_html = ""
    if related_entries:
        rel_items = []
        for r in related_entries:
            r_title = escape(r["title"])
            r_date = r["date"]
            r_slug = make_slug(r["title"], r_date)
            rel_items.append(f'<li><a href="{r_slug}.html">{r_title}</a> <span class="date">({r_date})</span></li>')
        if rel_items:
            related_html = f'''
    <div class="related-entries">
      <h3>\u95a2\u9023\u30a8\u30f3\u30c8\u30ea</h3>
      <ul>{"".join(rel_items)}</ul>
    </div>'''

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Knowledge</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'SF Mono', sans-serif; max-width: 800px; margin: 0 auto; padding: 2rem 1rem; background: #1a1a1a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #c0c0ff; border-bottom: 1px solid #333; }}
h2 {{ color: #a0a0ee; margin-top: 2em; }}
h3 {{ color: #9090dd; }}
a {{ color: #8888ff; }}
.date {{ color: #888; font-size: 0.9em; }}
.tag {{ display: inline-block; background: #333; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; margin-right: 4px; }}
.entry {{ margin: 1.5em 0; padding: 1em; background: #222; border-radius: 8px; }}
.related-entries {{ margin-top: 2em; padding: 1em; background: #222; border-radius: 8px; }}
.related-entries h3 {{ color: #9090dd; margin-top: 0; }}
.related-entries ul {{ list-style: none; padding-left: 0; }}
.related-entries li {{ margin: 0.5em 0; }}
.source {{ color: #777; font-size: 0.85em; }}
@media (prefers-color-scheme: light) {{ body {{ background: #f5f5f5; color: #222; }} h1 {{ color: #4444aa; }} a {{ color: #4444cc; }} .entry, .related-entries {{ background: #fff; }} .tag {{ background: #e0e0e0; color: #333; }} }}
</style>
</head>
<body>
<nav><a href="index.html" style="color:#c0c0ff">\U0001f4da Knowledge \u2190</a></nav>
<main>
<h1>{title}</h1>
<p class="date">{date_str} {tag_html}</p>
{content_html}
{source_html}
{related_html}
</main>
</body>
</html>"""


# ── メインビルド ───────────────────────────────────────────────────

def main():
    import sys
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    entries_path = os.path.join(repo_dir, "entries.json")
    template_path = os.path.join(repo_dir, "template.html")
    index_path = os.path.join(repo_dir, "index.html")

    with open(entries_path, "r") as f:
        entries = json.load(f)
    with open(template_path, "r") as f:
        template = f.read()

    today = date.today().isoformat()

    # 月別アーカイブ作成
    months = {}
    for e in entries:
        ym = e["date"][:7]  # YYYY-MM
        months.setdefault(ym, []).append(e)

    archive_dir = os.path.join(repo_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    for ym, month_entries in sorted(months.items(), reverse=True):
        path = os.path.join(archive_dir, f"{ym}.html")
        with open(path, "w") as f:
            f.write(generate_archive_page(ym, month_entries))

    # 個別エントリページ + 関連エントリ
    entry_dir = os.path.join(repo_dir, "entry")
    os.makedirs(entry_dir, exist_ok=True)

    for idx, entry in enumerate(entries):
        slug = make_slug(entry["title"], entry["date"])
        tags = set(entry.get("tags", []))
        related = [e for e in entries if e is not entry and tags & set(e.get("tags", []))]
        related_html = generate_single_page(entry, related[:5])
        with open(os.path.join(entry_dir, f"{slug}.html"), "w") as f:
            f.write(related_html)

    # index.html 用（関連エントリ付き）
    entries_with_related = []
    for idx, entry in enumerate(entries):
        tags = set(entry.get("tags", []))
        related = [e for e in entries if e is not entry and tags & set(e.get("tags", []))]
        entries_with_related.append((entry, related[:5]))

    entries_html_parts = []
    for entry, rel in entries_with_related:
        entries_html_parts.append(render_entry_html(entry, show_related=True, related_entries=rel))
    entries_html = "\n".join(entries_html_parts)

    html = template.replace("__TODAY__", today).replace("__ENTRIES__", entries_html)
    with open(index_path, "w") as f:
        f.write(html)

    # RSS/Atom フィード
    atom_path = os.path.join(repo_dir, "feed.xml")
    with open(atom_path, "w") as f:
        f.write(generate_atom(entries))

    print(f"Built index.html ({len(entries)} entries), {len(months)} archives, {len(entries)} single pages, feed.xml")


if __name__ == "__main__":
    main()
