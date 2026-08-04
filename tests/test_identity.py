"""identity のテスト（URL 正規化・永続 ID・既知判定）。"""
from __future__ import annotations
import hashlib

import pytest

from knowledge import identity, models


def _src(**kw) -> models.SourceConfig:
    kw.setdefault("id", "s1")
    kw.setdefault("kind", "atom")
    kw.setdefault("url", "https://example.com/feed")
    kw.setdefault("allowed_hosts", ("example.com",))
    kw.setdefault("priority", 100)
    kw.setdefault("required", True)
    return models.SourceConfig(**kw)


def test_normalize_lowercases_host_and_drops_default_port():
    u = identity.normalize_canonical_url(
        "https://EXAMPLE.COM:443/a/../b?x=1", allowed_hosts=("example.com",)
    )
    assert u == "https://example.com/b?x=1"


def test_normalize_rejects_non_https():
    with pytest.raises(ValueError):
        identity.normalize_canonical_url("http://example.com/a", allowed_hosts=("example.com",))


def test_normalize_rejects_userinfo_localhost_ip():
    with pytest.raises(ValueError):
        identity.normalize_canonical_url("https://u:p@example.com/a", allowed_hosts=("example.com",))
    with pytest.raises(ValueError):
        identity.normalize_canonical_url("https://localhost/a", allowed_hosts=("localhost",))
    with pytest.raises(ValueError):
        identity.normalize_canonical_url("https://127.0.0.1/a", allowed_hosts=("127.0.0.1",))


def test_normalize_drops_tracking_params():
    u = identity.normalize_canonical_url(
        "https://example.com/x?utm_source=a&keep=1&utm_campaign=b",
        allowed_hosts=("example.com",),
    )
    assert u == "https://example.com/x?keep=1"


def test_normalize_rejects_host_outside_allowlist():
    with pytest.raises(ValueError):
        identity.normalize_canonical_url("https://evil.com/a", allowed_hosts=("example.com",))


def test_stable_external_id_prefers_guid():
    item = {"guid": "g1", "id": "i1", "node_id": "n1"}
    assert identity.stable_external_id(item, "https://example.com/a") == "g1"


def test_candidate_and_entry_id_stable():
    item = {"guid": "g1"}
    c = models.Candidate(
        candidate_id="", source_id="s1", source_kind="atom", external_id="g1",
        canonical_url="https://example.com/a", title="t", published_at="2026-08-03T00:00:00Z",
        updated_at="2026-08-03T00:00:00Z", retrieved_at="2026-08-03T00:10:00Z",
    )
    cid1 = identity.make_candidate_id(c.source_id, c.external_id)
    cid2 = identity.make_candidate_id(c.source_id, c.external_id)
    assert cid1 == cid2
    eid1 = identity.make_entry_id(c)
    eid2 = identity.make_entry_id(c)
    assert eid1 == eid2
    assert eid1.startswith("kn_")


def test_is_known_against_entries_and_checkpoint():
    c = models.Candidate(
        candidate_id="", source_id="s1", source_kind="atom", external_id="g1",
        canonical_url="https://example.com/a", title="t", published_at="2026-08-03T00:00:00Z",
        updated_at="2026-08-03T00:00:00Z", retrieved_at="2026-08-03T00:10:00Z",
    )
    eid = identity.make_entry_id(c)
    e = models.Entry(
        id=eid, source_id="s1", external_id="g1", canonical_url="https://example.com/a",
        published_at="2026-08-03T00:00:00Z", collected_at="2026-08-03T00:10:00Z",
        title="t", summary="s",
    )
    doc = models.EntriesDocument(2, (e,))
    cp = models.Checkpoint(1, "2026-08-01T00:00:00Z", {})
    assert identity.is_known(c, doc, cp)

    cp2 = models.Checkpoint(
        1, "2026-08-01T00:00:00Z",
        {"s1": models.SourceCheckpoint(seen=({
            "external_id_hash": "sha256:" + hashlib.sha256(b"g1").hexdigest(),
            "canonical_url_hash": "sha256:" + hashlib.sha256(b"https://example.com/a").hexdigest(),
            "first_seen_at": "2026-08-02T00:00:00Z",
        },))},
    )
    empty = models.EntriesDocument(2, ())
    assert identity.is_known(c, empty, cp2)
