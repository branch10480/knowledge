"""Atom フィードと内部リンクのテスト。"""
from __future__ import annotations
import xml.etree.ElementTree as ET

from knowledge import atom, links, models


def _entry(iid: str, title: str) -> models.Entry:
    return models.Entry(
        id=iid, source_id="s1", external_id="x", canonical_url="https://example.com/a",
        published_at="2026-08-03T00:00:00Z", collected_at="2026-08-03T00:10:00Z",
        title=title, summary="summary text", tags=("Apple",),
    )


def test_atom_is_atom10_no_rss():
    e = _entry("kn_abc", "Title & <script>")
    xml = atom.render_atom((e,), updated_at="2026-08-03T00:00:00Z")
    root = ET.fromstring(xml)
    assert "http://www.w3.org/2005/Atom" in root.tag
    entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    assert len(entries) == 1
    # RSS 要素が混在しない
    assert root.findall(".//{http://www.w3.org/2005/Atom}item") == []
    ent = entries[0]
    assert ent.find("{http://www.w3.org/2005/Atom}id").text == "urn:knowledge:kn_abc"
    assert ent.find("{http://www.w3.org/2005/Atom}title").text is not None
    assert ent.find("{http://www.w3.org/2005/Atom}summary").text == "summary text"


def test_atom_escapes_special_chars():
    e = _entry("kn_abc", "A & B <x>")
    xml = atom.render_atom((e,), updated_at="2026-08-03T00:00:00Z").decode("utf-8")
    # XML として parse でき、& は実体参照になる
    root = ET.fromstring(xml)
    title = root.find("{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}title")
    assert title.text == "A & B <x>"


def test_link_checker_finds_real_links(tmp_path):
    (tmp_path / "index.html").write_text(
        '<a href="/knowledge/entry/kn_abc.html">x</a><script src="/knowledge/assets/app.js"></script>',
        encoding="utf-8",
    )
    (tmp_path / "entry").mkdir()
    (tmp_path / "entry" / "kn_abc.html").write_text("ok", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("", encoding="utf-8")
    rep = links.check_internal_links(tmp_path, base_path="/knowledge/")
    assert rep.ok


def test_link_checker_detects_broken(tmp_path):
    (tmp_path / "index.html").write_text(
        '<a href="/knowledge/entry/missing.html">x</a>', encoding="utf-8"
    )
    rep = links.check_internal_links(tmp_path, base_path="/knowledge/")
    assert not rep.ok
