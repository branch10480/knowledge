"""Atom 1.0 フィード生成。

Atom namespace の `<feed>` に `<entry>` を入れ、`id`/`title`/`updated`/`published`/
alternate `link`/escaped `summary` を生成する。RSS 要素（item/guid/pubDate/description）を
混在させない。XML 文字は明示的に escape する。
"""
from __future__ import annotations
from xml.sax.saxutils import escape

from .models import Entry

ATOM_NS = "http://www.w3.org/2005/Atom"


def _iso_utc(dt: str) -> str:
    return dt if dt.endswith("Z") else dt + "Z"


def render_atom(
    entries: tuple[Entry, ...],
    *,
    updated_at: str,
    base_url: str = "https://branch10480.github.io/knowledge",
    feed_title: str = "Knowledge",
) -> bytes:
    feed_id = base_url + "/"
    items: list[str] = []
    for e in entries:
        link = f"{base_url}/entry/{e.id}.html"
        items.append(
            "  <entry>\n"
            f"    <id>urn:knowledge:{escape(e.id)}</id>\n"
            f"    <title>{escape(e.title)}</title>\n"
            f"    <published>{_iso_utc(e.published_at)}</published>\n"
            f"    <updated>{_iso_utc(e.published_at)}</updated>\n"
            f'    <link rel="alternate" href="{escape(link)}"/>\n'
            f'    <summary type="text">{escape(e.summary)}</summary>\n'
            "  </entry>"
        )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<feed xmlns="{ATOM_NS}">\n'
        f"  <id>{escape(feed_id)}</id>\n"
        f"  <title>{escape(feed_title)}</title>\n"
        f"  <updated>{_iso_utc(updated_at)}</updated>\n"
        f'  <link rel="self" href="{escape(base_url + "/feed.xml")}"/>\n'
        f'  <link rel="alternate" href="{escape(base_url + "/")}"/>\n'
        + "\n".join(items)
        + "\n</feed>\n"
    )
    return xml.encode("utf-8")
