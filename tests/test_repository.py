"""repository のテスト：merge の重複排除・ソート、transaction の canonical 書き出し。"""
from __future__ import annotations
import json

from knowledge import models, repository


def _entry(iid: str, published: str) -> models.Entry:
    return models.Entry(
        id=iid, source_id="s1", external_id=f"e-{iid}", canonical_url=f"https://example.com/{iid}",
        published_at=published, collected_at="2026-08-03T00:00:00Z", title=f"t {iid}",
        summary="s", tags=("Apple",),
        source_digest="sha256:" + "0" * 64,
        summary_model={"provider": "p", "model": "m", "prompt_version": "v"},
        review={"factual_gate": "passed", "checked_at": "2026-08-03T00:00:00Z"},
    )


def test_merge_dedupes_and_sorts():
    existing = models.EntriesDocument(2, (_entry("kn_a", "2026-08-01T00:00:00Z"),))
    # 既存と重複（同じ id / source+external / canonical）
    dup = _entry("kn_a", "2026-08-02T00:00:00Z")
    new = _entry("kn_b", "2026-08-03T00:00:00Z")
    old = _entry("kn_c", "2026-07-30T00:00:00Z")
    merged = repository.merge_entries(existing, (dup, new, old))
    ids = [e.id for e in merged.entries]
    assert ids == ["kn_b", "kn_a", "kn_c"]  # 降順、dup は除外
    assert len(merged.entries) == 3


def test_prepare_transaction_writes_canonical(tmp_path):
    doc = models.EntriesDocument(2, (_entry("kn_a", "2026-08-01T00:00:00Z"),))
    cp = models.Checkpoint(1, "2026-08-02T00:00:00Z", {})
    prep = repository.prepare_transaction(
        repo_root=tmp_path, merged=doc, checkpoint=cp, transaction_dir=tmp_path / "txn",
    )
    raw = json.loads(prep.data_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert len(raw["entries"]) == 1
