#!/usr/bin/env python3
"""
entries.json + template.html から index.html を生成するビルドスクリプト。
P2: RSS/Atom フィード, 個別エントリページ, 月別アーカイブ, 関連エントリ表示に対応.
全ページ Toshi Design System v0.7.0 に準拠.
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

    tag_html = "".join(f'<span class="tag-pill">{escape(t)}</span>' for t in tags)
    source_html = ""
    if source:
        src_escaped = escape(source, quote=True)
        hostname = ""
        try:
            hostname = escape(source.split("//")[1].split("/")[0].replace("www.", ""))
        except (IndexError, ValueError):
            hostname = src_escaped
        source_html = f'<p class="entry-meta"><span class="entry-source"><a href="{src_escaped}" target="_blank" rel="noopener">{hostname}</a></span></p>'

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
    <article class="entry-card">
      <h2 class="entry-header"><a href="entry/{make_slug(entry['title'], date_str)}.html">{title}</a></h2>
      <p class="entry-meta"><time datetime="{date_str}">{date_str}</time>{tag_html}</p>
      {content_html}
      {source_html}
      {related_html}
    </article>"""


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
    items_html_parts = []
    for e in entries:
        title = escape(e["title"])
        slug = make_slug(e["title"], e["date"])
        tags = "".join(f'<span class="tag-pill">{escape(t)}</span>' for t in e.get("tags", []))
        content_raw = e["content"]
        if _is_html(content_raw):
            summary = content_raw.strip()[:300]
        else:
            summary = md_to_html(content_raw[:200])
        items_html_parts.append(f"""
    <article class="entry-card">
      <h2 class="entry-header"><a href="{slug}.html">{title}</a></h2>
      <div class="entry-meta">
        <time datetime="{e['date']}">{e['date']}</time>
      </div>
      <div class="entry-tags">{tags}</div>
      <div class="entry-body">{summary}</div>
    </article>""")

    items_html = "\n".join(items_html_parts)

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(month_title)} — Knowledge</title>
<script>
(() => {{
  const root = document.documentElement;
  const browserTheme = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  try {{
    const mode = localStorage.getItem("tds-theme") || "auto";
    if (mode === "light" || mode === "dark") {{ root.dataset.theme = mode; root.dataset.themeMode = mode; return; }}
    root.dataset.theme = browserTheme() === "dark" ? "dark" : "light";
  }} catch {{}}
}})();
</script>
<style>
@font-face {{ font-family: "UDEV Gothic 35LG"; src: url("https://branch10480.github.io/design-system/fonts/UDEVGothic35LG-Regular.woff2") format("woff2"); font-weight:400; font-display:swap; }}
@font-face {{ font-family: "UDEV Gothic 35LG"; src: url("https://branch10480.github.io/design-system/fonts/UDEVGothic35LG-Bold.woff2") format("woff2"); font-weight:700; font-display:swap; }}
@font-face {{ font-family: "UDEV Gothic 35LG"; src: url("https://branch10480.github.io/design-system/fonts/UDEVGothic35LG-Italic.woff2") format("woff2"); font-weight:400; font-style:italic; font-display:swap; }}
:root {{ color-scheme:light dark; --bg:#FFF; --bg-alt:#F5F5F7; --bg-elev:#FFF; --separator:#D2D2D7; --hairline:rgba(0,0,0,.10); --text:#1D1D1F; --text-2:#66666B; --text-3:#747479; --tint:#0066CC; --tint-fill:#0071E3; --on-tint:#FFF; --green:#008009; --orange:#B25000; --red:#D70000; --purple:#6846C7; --font-text:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic",Arial,sans-serif; --font-display:-apple-system,BlinkMacSystemFont,"SF Pro Display","Helvetica Neue","Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic",Arial,sans-serif; --font-mono:"UDEV Gothic 35LG",ui-monospace,"SF Mono",Menlo,monospace; --code-weight:400; --r-pill:980px; --r-lg:14px; --r-md:10px; --sh-1:0 1px 2px rgba(0,0,0,.04),0 4px 16px rgba(0,0,0,.06); --sh-2:0 11px 34px rgba(0,0,0,.14); --doc-max:1040px; }}
@media(prefers-color-scheme:dark) {{ :root:not([data-theme="light"]){{--bg:#181818;--bg-alt:#1B1E24;--bg-elev:#252932;--separator:#333842;--hairline:rgba(255,255,255,.14);--text:#D0D3D8;--text-2:#9EA3AA;--text-3:#8B9098;--tint:#4A94DC;--green:#86D7A3;--orange:#E2BE5A;--red:#E88980;--purple:#C2A3E5;--sh-1:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.5);--sh-2:0 11px 34px rgba(0,0,0,.6);}} }}
:root[data-theme="dark"]{{--bg:#181818;--bg-alt:#1B1E24;--bg-elev:#252932;--separator:#333842;--hairline:rgba(255,255,255,.14);--text:#D0D3D8;--text-2:#9EA3AA;--text-3:#8B9098;--tint:#4A94DC;--green:#86D7A3;--orange:#E2BE5A;--red:#E88980;--purple:#C2A3E5;--sh-1:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.5);--sh-2:0 11px 34px rgba(0,0,0,.6);}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-text);font-size:17px;line-height:1.47;letter-spacing:-.022em;-webkit-font-smoothing:antialiased}}a{{color:var(--tint);text-decoration:none}}a:hover{{text-decoration:underline}}h1,h2,h3{{font-family:var(--font-display);color:var(--text)}}code,pre{{font-family:var(--font-mono);letter-spacing:0}}
.globalnav{{position:sticky;top:0;z-index:50;background:color-mix(in srgb,var(--bg) 72%,transparent);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);border-bottom:1px solid var(--hairline)}}.gn-inner{{max-width:var(--doc-max);margin:0 auto;height:48px;padding:0 22px;display:flex;align-items:center;gap:28px}}.gn-brand{{font-size:17px;font-weight:600;color:var(--text);letter-spacing:-.02em}}.gn-brand:hover{{text-decoration:none}}
.theme-toggle{{font:500 12px var(--font-text);color:var(--text-2);background:var(--bg-alt);border:1px solid transparent;border-radius:var(--r-pill);padding:5px 14px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:color 120ms,border-color 120ms}}.theme-toggle:hover{{color:var(--text);border-color:var(--separator)}}.theme-toggle .dot{{width:8px;height:8px;border-radius:50%;background:var(--tint)}}
.hero{{background:var(--bg-alt);padding:88px 22px 72px;text-align:center}}.eyebrow{{font-size:14px;color:var(--text-2);margin:0 0 10px;font-weight:500}}.hero h1{{margin:0 auto;font-size:clamp(34px,5.2vw,52px);font-weight:600;letter-spacing:-.015em;line-height:1.14}}
.entries-list{{max-width:var(--doc-max);margin:0 auto;padding:12px 22px 96px}}.entry-card{{display:flex;flex-direction:column;background:var(--bg-elev);border:1px solid var(--hairline);border-radius:var(--r-lg);padding:20px 22px;margin-bottom:16px;box-shadow:var(--sh-1);transition:transform 180ms ease,box-shadow 180ms ease}}.entry-card:hover{{transform:translateY(-2px);box-shadow:var(--sh-2)}}.entry-header{{display:flex;align-items:baseline;gap:12px;margin-bottom:6px}}.entry-title{{font-size:18px;font-weight:600;letter-spacing:-.01em;margin:0}}.entry-title a{{color:var(--text)}}.entry-title a:hover{{color:var(--tint);text-decoration:none}}.entry-meta{{display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--text-3);margin-bottom:8px}}.entry-tags{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}}.tag-pill{{font:500 11px var(--font-text);color:var(--tint);padding:2px 8px;border-radius:var(--r-pill);background:color-mix(in srgb,var(--tint) 12%,transparent)}}.entry-body{{font-size:15px;line-height:1.6;color:var(--text)}}.entry-body p{{margin:0 0 8px}}.entry-body code{{font-size:.92em;background:var(--bg-alt);border-radius:4px;padding:1px 5px;color:var(--text)}}
.sitefooter{{border-top:1px solid var(--hairline);background:var(--bg-alt);padding:24px 0 34px;font-size:12px;color:var(--text-3)}}.sitefooter .inner{{max-width:var(--doc-max);margin:0 auto;padding:0 22px;display:flex;justify-content:center}}.sitefooter a{{color:var(--text-2)}}
@media(max-width:700px){{.hero h1 br{{display:none}}}}@media(max-width:600px){{.hero{{padding:48px 16px 40px}}.entries-list{{padding-left:16px;padding-right:16px}}}}
</style>
</head>
<body data-view="home">
<nav class="globalnav"><div class="gn-inner"><a class="gn-brand" href="index.html">Knowledge</a><button class="theme-toggle" id="theme-toggle" aria-label="テーマ切替"><span class="dot"></span> <span id="theme-label">自動</span></button></div></nav>
<header class="hero"><p class="eyebrow">{month}</p><h1>{escape(month_title)}</h1></header>
<main class="entries-list">\n{items_html}\n</main>
<footer class="sitefooter"><div class="inner"><p>Powered by <a href="index.html">Knowledge</a> · Toshi Design System v0.7.0</p></div></footer>
<script>
(() => {{
  const root = document.documentElement;
  const TT = document.getElementById('theme-toggle');
  const TL = document.getElementById('theme-label');
  const setTheme = (t) => {{
    if (t === 'dark') {{ root.dataset.theme='dark'; root.dataset.themeMode='dark'; }}
    else if (t === 'light') {{ root.dataset.theme='light'; root.dataset.themeMode='light'; }}
    else {{ root.removeAttribute('data-theme'); root.dataset.themeMode='auto'; }}
    localStorage.setItem('tds-theme', t); TL.textContent = t === 'auto' ? '自動' : t;
  }};
  setTheme(localStorage.getItem('tds-theme') || 'auto');
  TT.addEventListener('click', () => {{
    const m = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    setTheme(root.dataset.themeMode === 'auto' ? m : 'auto');
  }});
}})();
</script>
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

    tag_html = "".join(f'<span class="tag-pill">{escape(t)}</span>' for t in tags)
    source_html = ""
    if source:
        src_escaped = escape(source, quote=True)
        hostname = ""
        try:
            hostname = escape(source.split("//")[1].split("/")[0].replace("www.", ""))
        except (IndexError, ValueError):
            hostname = src_escaped
        source_html = f'<p class="entry-meta"><span class="entry-source"><a href="{src_escaped}" target="_blank" rel="noopener">{hostname}</a></span></p>'

    related_html = ""
    if related_entries:
        rel_items = []
        for r in related_entries:
            r_title = escape(r["title"])
            r_date = r["date"]
            r_slug = make_slug(r["title"], r_date)
            rel_items.append(f'<li><a href="entry/{r_slug}.html">{r_title}</a> <span class="date">({r_date})</span></li>')
        if rel_items:
            related_html = "\n    <div class=\"related-entries\">\n      <h3>関連エントリ</h3>\n      <ul>" + "".join(rel_items) + "\n    </div>"

    css = """@font-face { font-family: "UDEV Gothic 35LG"; src: url("https://branch10480.github.io/design-system/fonts/UDEVGothic35LG-Regular.woff2") format("woff2"); font-weight:400; font-display:swap; }
@font-face { font-family: "UDEV Gothic 35LG"; src: url("https://branch10480.github.io/design-system/fonts/UDEVGothic35LG-Bold.woff2") format("woff2"); font-weight:700; font-display:swap; }
@font-face { font-family: "UDEV Gothic 35LG"; src: url("https://branch10480.github.io/design-system/fonts/UDEVGothic35LG-Italic.woff2") format("woff2"); font-weight:400; font-style:italic; font-display:swap; }
:root { color-scheme:light dark; --bg:#FFF; --bg-alt:#F5F5F7; --bg-elev:#FFF; --separator:#D2D2D7; --hairline:rgba(0,0,0,.10); --text:#1D1D1F; --text-2:#66666B; --text-3:#747479; --tint:#0066CC; --tint-fill:#0071E3; --on-tint:#FFF; --green:#008009; --orange:#B25000; --red:#D70000; --purple:#6846C7; --font-text:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic",Arial,sans-serif; --font-display:-apple-system,BlinkMacSystemFont,"SF Pro Display","Helvetica Neue","Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic",Arial,sans-serif; --font-mono:"UDEV Gothic 35LG",ui-monospace,"SF Mono",Menlo,monospace; --code-weight:400; --r-pill:980px; --r-lg:14px; --r-md:10px; --sh-1:0 1px 2px rgba(0,0,0,.04),0 4px 16px rgba(0,0,0,.06); --sh-2:0 11px 34px rgba(0,0,0,.14); --doc-max:1040px; }
@media(prefers-color-scheme:dark) { :root:not([data-theme="light"]) { --bg:#181818;--bg-alt:#1B1E24;--bg-elev:#252932;--separator:#333842;--hairline:rgba(255,255,255,.14);--text:#D0D3D8;--text-2:#9EA3AA;--text-3:#8B9098;--tint:#4A94DC;--green:#86D7A3;--orange:#E2BE5A;--red:#E88980;--purple:#C2A3E5;--sh-1:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.5);--sh-2:0 11px 34px rgba(0,0,0,.6); } }
:root[data-theme="dark"] { --bg:#181818;--bg-alt:#1B1E24;--bg-elev:#252932;--separator:#333842;--hairline:rgba(255,255,255,.14);--text:#D0D3D8;--text-2:#9EA3AA;--text-3:#8B9098;--tint:#4A94DC;--green:#86D7A3;--orange:#E2BE5A;--red:#E88980;--purple:#C2A3E5;--sh-1:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.5);--sh-2:0 11px 34px rgba(0,0,0,.6); }
* { box-sizing:border-box } html { scroll-behavior:smooth } body { margin:0;background:var(--bg);color:var(--text);font-family:var(--font-text);font-size:17px;line-height:1.47;letter-spacing:-.022em;-webkit-font-smoothing:antialiased } a { color:var(--tint);text-decoration:none } a:hover { text-decoration:underline } h1,h2,h3 { font-family:var(--font-display);color:var(--text) } code,pre { font-family:var(--font-mono);letter-spacing:0 }
.globalnav { position:sticky;top:0;z-index:50;background:color-mix(in srgb,var(--bg) 72%,transparent);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);border-bottom:1px solid var(--hairline) } .gn-inner { max-width:var(--doc-max);margin:0 auto;height:48px;padding:0 22px;display:flex;align-items:center;gap:28px } .gn-brand { font-size:17px;font-weight:600;color:var(--text);letter-spacing:-.02em } .gn-brand:hover { text-decoration:none }
.theme-toggle { font:500 12px var(--font-text);color:var(--text-2);background:var(--bg-alt);border:1px solid transparent;border-radius:var(--r-pill);padding:5px 14px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:color 120ms,border-color 120ms } .theme-toggle:hover { color:var(--text);border-color:var(--separator) } .theme-toggle .dot { width:8px;height:8px;border-radius:50%;background:var(--tint) }
.hero { background:var(--bg-alt);padding:88px 22px 72px;text-align:center } .eyebrow { font-size:14px;color:var(--text-2);margin:0 0 10px;font-weight:500 } .hero h1 { margin:0 auto;font-size:clamp(34px,5.2vw,52px);font-weight:600;letter-spacing:-.015em;line-height:1.14 }
.content-area { max-width:min(var(--doc-max),calc(100vw - 2 * var(--otp-reserve)));margin:0 auto;padding:48px 22px 96px } .entry-card { display:flex;flex-direction:column;background:var(--bg-elev);border:1px solid var(--hairline);border-radius:var(--r-lg);padding:20px 22px;margin-bottom:16px;box-shadow:var(--sh-1) } .entry-header { display:flex;align-items:baseline;gap:12px;margin-bottom:6px } .entry-title { font-size:18px;font-weight:600;letter-spacing:-.01em;margin:0 } .entry-title a { color:var(--text) } .entry-title a:hover { color:var(--tint);text-decoration:none } .entry-meta { display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--text-3);margin-bottom:8px } .entry-tags { display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px } .tag-pill { font:500 11px var(--font-text);color:var(--tint);padding:2px 8px;border-radius:var(--r-pill);background:color-mix(in srgb,var(--tint) 12%,transparent) } .entry-body { font-size:15px;line-height:1.6;color:var(--text) } .entry-body p { margin:0 0 8px } .entry-body code { font-size:.92em;background:var(--bg-alt);border-radius:4px;padding:1px 5px;color:var(--text) } .entry-body pre { background:var(--bg-code);border:1px solid var(--hairline);border-radius:var(--r-lg);padding:18px 16px;overflow-x:auto;font-size:15px;line-height:1.62 }
.related-entries { margin-top:40px;padding-top:24px;border-top:1px solid var(--hairline) } .related-entries h3 { font-size:17px;font-weight:600;margin:0 0 12px } .related-entries ul { margin:0;padding-left:20px } .related-entries li { margin:6px 0;font-size:15px } .related-entries .date { color:var(--text-3);font-size:13px }
.back-link { display:inline-block;margin-bottom:24px;font-size:15px;color:var(--tint) } .back-link:hover { text-decoration:none }
.sitefooter { border-top:1px solid var(--hairline);background:var(--bg-alt);padding:24px 0 34px;font-size:12px;color:var(--text-3) } .sitefooter .inner { max-width:var(--doc-max);margin:0 auto;padding:0 22px;display:flex;justify-content:center } .sitefooter a { color:var(--text-2) }
@media(max-width:700px) { .hero h1 br { display:none } } @media(max-width:600px) { .hero { padding:48px 16px 40px } .content-area { padding-left:16px;padding-right:16px } }"""

    theme_init = """(() => {
  const root = document.documentElement;
  const browserTheme = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  try {
    const mode = localStorage.getItem("tds-theme") || "auto";
    if (mode === "light" || mode === "dark") { root.dataset.theme = mode; root.dataset.themeMode = mode; return; }
    root.dataset.theme = browserTheme() === "dark" ? "dark" : "light";
  } catch {}
})();"""

    theme_toggle_js = """(() => {
  const root = document.documentElement;
  const TT = document.getElementById('theme-toggle');
  const TL = document.getElementById('theme-label');
  const setTheme = (t) => {
    if (t === 'dark') { root.dataset.theme='dark'; root.dataset.themeMode='dark'; }
    else if (t === 'light') { root.dataset.theme='light'; root.dataset.themeMode='light'; }
    else { root.removeAttribute('data-theme'); root.dataset.themeMode='auto'; }
    localStorage.setItem('tds-theme', t); TL.textContent = t === 'auto' ? '自動' : t;
  };
  setTheme(localStorage.getItem('tds-theme') || 'auto');
  TT.addEventListener('click', () => {
    const m = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    setTheme(root.dataset.themeMode === 'auto' ? m : 'auto');
  });
})();"""

    body = (
        "<!doctype html>\n<html lang=\"ja\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>" + title + " — Knowledge</title>\n"
        "<script>\n" + theme_init + "\n</script>\n<style>\n" + css + "\n</style>\n"
        "</head>\n<body data-view=\"single\">\n"
        "<nav class=\"globalnav\"><div class=\"gn-inner\">"
        "<a class=\"gn-brand\" href=\"index.html\">Knowledge</a>"
        "<button class=\"theme-toggle\" id=\"theme-toggle\" aria-label=\"テーマ切替\">"
        "<span class=\"dot\"></span> <span id=\"theme-label\">自動</span></button>"
        "</div></nav>\n"
        "<div class=\"content-area\">\n"
        "<a href=\"index.html\" class=\"back-link\">\u2190 Back to all entries</a>\n"
        "<h1>" + title + "</h1>\n"
        "<p class=\"entry-meta\"><time datetime=\"" + date_str + "\">" + date_str + "</time> " + tag_html + "</p>\n"
        "<div class=\"entry-body\">" + content_html + "</div>\n"
    )
    footer = (
        source_html + "\n"
        + related_html + "\n"
        "</div>\n"
        "<footer class=\"sitefooter\"><div class=\"inner\">"
        "<p>Powered by <a href=\"index.html\">Knowledge</a> · Toshi Design System v0.7.0</p>"
        "</div></footer>\n"
        "<script>\n" + theme_toggle_js + "\n</script>\n"
        "</body>\n</html>"
    )
    return body + footer


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
