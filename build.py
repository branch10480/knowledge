#!/usr/bin/env python3
"""
entries.json から index.html を生成するビルドスクリプト。
entries.json が正本（JSON 配列、日付降順）。
全エントリをレンダリングして index.html を書き出す。
content が HTML の場合はそのまま挿入、Markdown の場合は md_to_html() で変換。
"""
import json
import re
from html import escape
from datetime import date

ALLOWED_URL_SCHEMES = ("http://", "https://")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge</title>
<style>
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Mono', 'Noto Sans JP', sans-serif;
  max-width: 800px;
  margin: 0 auto;
  padding: 2rem 1rem;
  background: #1a1a1a;
  color: #e0e0e0;
  line-height: 1.7;
}
h1 { color: #c0c0ff; border-bottom: 1px solid #333; }
h2 { color: #a0a0ee; margin-top: 2em; }
h3 { color: #9090dd; }
a { color: #8888ff; }
.date { color: #888; font-size: 0.9em; }
.section { margin: 1.5em 0; padding: 1em; background: #222; border-radius: 8px; }
.tag { display: inline-block; background: #333; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; margin-right: 4px; }
.entry { margin: 1.5em 0; padding: 1em; background: #222; border-radius: 8px; }
.source { color: #777; font-size: 0.85em; }
.source a { color: #6666cc; }
</style>
</head>
<body>
<h1>\U0001f4da Knowledge</h1>
<p class="date">\u6700\u7d42\u66f4\u65b0: {today}</p>
<p>\u60c5\u5831\u53ce\u96c6\u30fb\u52c9\u5f37\u30e1\u30e2\u3092\u84c4\u7a4d\u3057\u3066\u3044\u304f\u77e5\u8b58\u30d9\u30fc\u30b9\u3067\u3059\u3002</p>
<hr>
<div id="entries">
{entries_html}
  </div>
</body>
</html>"""


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
    entries_path = "entries.json"
    output_path = "index.html"

    with open(entries_path, "r") as f:
        entries = json.load(f)

    today = date.today().isoformat()
    entries_html = "\n".join(render_entry(e) for e in entries)
    html = HTML_TEMPLATE.replace("{today}", today).replace("{entries_html}", entries_html)

    with open(output_path, "w") as f:
        f.write(html)

    print(f"Built {output_path}: {len(entries)} entries, updated {today}")


if __name__ == "__main__":
    main()
