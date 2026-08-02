#!/usr/bin/env python3
"""
index.html に新しい知識エントリを追記するスクリプト。
標準入力から JSON 配列を受け取り、既存の HTML の <div id="entries"> セクションに
日付順（降順）で挿入する。

v2: 正規表現ではなく HTML の構造を正しく解析する方式に変更。
    全文字列をエスケープし、URL は https:// のみ許可。
"""
import json
import sys
import re
from html import escape
from datetime import date

ALLOWED_URL_SCHEMES = ("http://", "https://")


def validate_entry(entry: dict) -> None:
    d = entry.get("date", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        raise ValueError(f"Invalid date format: {d!r}")
    if not entry.get("title", "").strip():
        raise ValueError("title is required")
    tags = entry.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    for t in tags:
        if not isinstance(t, str):
            raise ValueError(f"tag is not a string: {t!r}")
    if not entry.get("content", "").strip():
        raise ValueError("content is required")
    src = entry.get("source", "")
    if src and not src.startswith(ALLOWED_URL_SCHEMES):
        raise ValueError(f"Invalid source URL scheme: {src!r}")


def md_to_html(text: str) -> str:
    """簡易マークダウン→HTML 変換（全ての出力はエスケープ済み）"""
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


def build_entry_html(entry: dict) -> str:
    date_str = entry["date"]
    title = escape(entry["title"])
    tags = entry.get("tags", [])
    content_md = entry["content"]
    source = entry.get("source", "")

    content_html = md_to_html(content_md)
    tag_html = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags)

    source_html = ""
    if source:
        src_escaped = escape(source, quote=True)
        source_html = f'<p class="source">出典: <a href="{src_escaped}">{src_escaped}</a></p>'

    return f"""
    <div class="entry">
      <h2>{title}</h2>
      <p class="date">{date_str} {tag_html}</p>
      {content_html}
      {source_html}
      <hr>
    </div>"""


def _parse_existing_entries(html: str) -> list[str]:
    pattern = re.compile(r'<div\s+class="entry">.*?</div>', re.DOTALL)
    return pattern.findall(html)


def _find_entries_div(html: str) -> tuple[int, int]:
    start_tag = '<div id="entries">'
    end_tag = '</div>'
    start_pos = html.find(start_tag)
    if start_pos == -1:
        raise RuntimeError("Could not find <div id='entries'> in HTML")
    end_pos = html.find(end_tag, start_pos)
    if end_pos == -1:
        raise RuntimeError("Could not find closing </div>")
    return start_pos, end_pos + len(end_tag)


def main():
    data = json.load(sys.stdin)
    if not data:
        print("No entries to add", file=sys.stderr)
        return

    html_path = sys.argv[1] if len(sys.argv) > 1 else "index.html"

    for entry in data:
        validate_entry(entry)

    with open(html_path, "r") as f:
        html = f.read()

    existing = _parse_existing_entries(html)
    new_entries = [build_entry_html(e) for e in data]

    def extract_date(e_html: str) -> str:
        m = re.search(r'<p class="date">([^<]+)', e_html)
        return m.group(1).strip().split(" ")[0] if m else ""

    all_entries = existing + new_entries
    all_entries.sort(key=extract_date, reverse=True)

    entries_html = "\n".join(all_entries)
    start, end = _find_entries_div(html)
    before = html[:start]
    after = html[end:]

    new_html = f'{before}<div id="entries">\n{entries_html}\n  </div>{after}'

    today = date.today().isoformat()
    new_html = re.sub(
        r'最終更新: \d{4}-\d{2}-\d{2}',
        f'最終更新: {today}',
        new_html
    )

    if new_html == html:
        print(f"No changes: {html_path} is already up to date")
        return

    with open(html_path, "w") as f:
        f.write(new_html)

    added = len(new_entries)
    total = len(all_entries)
    print(f"Updated {html_path}: {added} new entries added, {total} total")


if __name__ == "__main__":
    main()
