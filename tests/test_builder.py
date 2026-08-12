"""builder のテスト：決定性、XSS 無害化、index 30 件上限、関連エントリ。"""
from __future__ import annotations
from pathlib import Path

from knowledge import builder, cli, models

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
STATIC = REPO_ROOT / "static"


def _entries(n: int, malicious: str | None = None) -> tuple[models.Entry, ...]:
    es = []
    for i in range(n):
        title = f"title {i}"
        tags = ("t1", "t2")
        if malicious and i == 0:
            title = malicious
            tags = ("t1",)
        es.append(models.Entry(
            id=f"kn_{i:04d}", source_id="s1", external_id=f"e{i}",
            canonical_url=f"https://example.com/{i}",
            published_at=f"2026-07-{(i % 28) + 1:02d}T00:00:00Z",
            collected_at="2026-07-01T00:00:00Z",
            title=title, summary="summary text here", tags=tags,
            source_digest="sha256:" + "0" * 64,
        ))
    return tuple(es)


def _build(doc: models.EntriesDocument, out: Path, built_at: str):
    return builder.build_site(
        doc, templates_dir=TEMPLATES, static_dir=STATIC,
        output_dir=out, built_at=built_at, repo_root=REPO_ROOT,
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_build_is_deterministic(tmp_path):
    doc = models.EntriesDocument(2, _entries(12))
    a, b = tmp_path / "a", tmp_path / "b"
    _build(doc, a, "2026-08-03T00:00:00Z")
    _build(doc, b, "2026-08-03T00:00:00Z")
    assert _tree_hashes(a) == _tree_hashes(b)


def test_xss_is_neutralized(tmp_path):
    doc = models.EntriesDocument(2, _entries(3, malicious="</script><script>alert(1)</script>"))
    out = tmp_path / "out"
    _build(doc, out, "2026-08-03T00:00:00Z")
    html = (out / "index.html").read_text(encoding="utf-8")
    # 実スクリプトタグが生成されない（autoescape）
    assert "<script>alert(1)" not in html
    # エスケープされたリテラルとして存在
    assert "</script>" in html


def test_index_limits_to_30(tmp_path):
    doc = models.EntriesDocument(2, _entries(31))
    out = tmp_path / "out"
    _build(doc, out, "2026-08-03T00:00:00Z")
    html = (out / "index.html").read_text(encoding="utf-8")
    assert html.count('class="entry-card"') == 30
    # 全 31 件は月別 archive に分離される
    arch = sorted((out / "archive").glob("*.html"))
    assert len(arch) > 0


def test_related_entries(tmp_path):
    doc = models.EntriesDocument(2, _entries(10))
    out = tmp_path / "out"
    _build(doc, out, "2026-08-03T00:00:00Z")
    entry_html = (out / "entry" / "kn_0000.html").read_text(encoding="utf-8")
    assert "関連エントリ" in entry_html


def test_json_escape_for_script_is_parseable():
    import json
    payload = [{"title": "a</script><script>", "summary": "x&y<b>", "id": "kn_x"}]
    escaped = builder._escape_json_for_script(payload)
    # 実 script タグ終了を作らない
    assert "</script>" not in escaped
    # JSON として再パースでき、元の値に戻る
    parsed = json.loads(escaped)
    assert parsed[0]["title"] == "a</script><script>"
    assert parsed[0]["summary"] == "x&y<b>"


def test_embedded_index_data_is_parseable_json(tmp_path):
    import json
    import re

    doc = models.EntriesDocument(2, _entries(3))
    out = tmp_path / "out"
    _build(doc, out, "2026-08-03T00:00:00Z")

    html = (out / "index.html").read_text(encoding="utf-8")
    match = re.search(
        r'<script type="application/json" id="knowledge-data">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    assert len(json.loads(match.group(1))) == 3


def test_check_build_rejects_artifact_changed_after_manifest(tmp_path):
    doc = models.EntriesDocument(2, _entries(3))
    out = tmp_path / "out"
    _build(doc, out, "2026-08-03T00:00:00Z")
    (out / "index.html").write_text("tampered", encoding="utf-8")

    assert cli._check_document(doc, dist_dir=out) == 1
