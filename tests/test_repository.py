"""repository のテスト：merge の重複排除・ソート、transaction の canonical 書き出し。"""
from __future__ import annotations
import json
import pytest

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


def test_commit_failure_restores_data_and_index(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old_entries = '{"schema_version":2,"entries":[]}\n'
    old_checkpoint = (
        '{"schema_version":1,"last_success_at":"1970-01-01T00:00:00Z",'
        '"sources":{}}\n'
    )
    (data_dir / "entries.json").write_text(old_entries, encoding="utf-8")
    (data_dir / "checkpoint.json").write_text(old_checkpoint, encoding="utf-8")
    prep = repository.prepare_transaction(
        repo_root=tmp_path,
        merged=models.EntriesDocument(2, (_entry("kn_a", "2026-08-01T00:00:00Z"),)),
        checkpoint=models.Checkpoint(1, "2026-08-02T00:00:00Z", {}),
        transaction_dir=tmp_path / "txn",
    )
    calls = []

    def fake_git(_repo_root, *args):
        calls.append(args)
        if args[0] == "commit":
            raise repository.RepositoryError("simulated commit failure")
        return ""

    monkeypatch.setattr(repository, "_git", fake_git)

    with pytest.raises(repository.RepositoryError, match="rolled back"):
        repository.commit_transaction(prep)

    assert (data_dir / "entries.json").read_text(encoding="utf-8") == old_entries
    assert (data_dir / "checkpoint.json").read_text(encoding="utf-8") == old_checkpoint
    assert (
        "reset",
        "--",
        "data/entries.json",
        "data/checkpoint.json",
    ) in calls
