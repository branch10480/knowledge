#!/usr/bin/env python3
"""
entries.json + template.html から index.html を生成するビルドスクリプト。
"""
import json
import re
import os
from html import escape
from datetime import date

ALLOWED_URL_SCHEMES = ("http://", "https://")


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


def render_entry(entry: dict) -> str:
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

    return f"""
    <div class="entry">
      <h2>{title}</h2>
      <p class="date">{date_str} {tag_html}</p>
      {content_html}
      {source_html}
      <hr>
    </div>"""


def main():
    import sys
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    entries_path = os.path.join(repo_dir, "entries.json")
    template_path = os.path.join(repo_dir, "template.html")
    output_path = os.path.join(repo_dir, "index.html")

    with open(entries_path, "r") as f:
        entries = json.load(f)
    with open(template_path, "r") as f:
        template = f.read()

    today = date.today().isoformat()
    entries_html = "\n".join(render_entry(e) for e in entries)

    html = template.replace("__TODAY__", today).replace("__ENTRIES__", entries_html)

    with open(output_path, "w") as f:
        f.write(html)

    print(f"Built {output_path}: {len(entries)} entries, updated {today}")


if __name__ == "__main__":
    main()
