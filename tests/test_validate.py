"""validate のテスト（sanitize・schema・factual gate）。"""
from __future__ import annotations
import pytest

from knowledge import models, validate


def test_sanitize_plain_text_rejects_html_and_script():
    with pytest.raises(validate.ValidationError):
        validate.sanitize_plain_text("<b>bold</b>", max_chars=100)
    with pytest.raises(validate.ValidationError):
        validate.sanitize_plain_text("x</script><script>alert(1)</script>", max_chars=100)
    with pytest.raises(validate.ValidationError):
        validate.sanitize_plain_text("onclick=alert(1)", max_chars=100)


def test_sanitize_removes_control_and_bidi():
    out = validate.sanitize_plain_text("a\x00b\u202e\u202ac", max_chars=100)
    assert out == "abc"
    assert validate.sanitize_plain_text("\ufdd0x", max_chars=100) == "x"


def test_sanitize_normalizes_nfc():
    # 合成済み（NFC）の 'é' はそのまま
    out = validate.sanitize_plain_text("\u00e9", max_chars=100)
    assert out == "\u00e9"


def test_validate_entries_document_ok():
    e = {
        "id": "kn_abc123", "source_id": "s1", "external_id": "x",
        "canonical_url": "https://example.com/a", "published_at": "2026-08-03T00:00:00Z",
        "collected_at": "2026-08-03T00:10:00Z", "title": "t", "summary": "s",
        "tags": ["iOS"], "language": "ja", "source_digest": "sha256:" + "0" * 64,
        "summary_model": {"provider": "p", "model": "m", "prompt_version": "v"},
        "review": {"factual_gate": "passed", "checked_at": "2026-08-03T00:11:00Z"},
    }
    rep = validate.validate_entries_document({"schema_version": 2, "entries": [e]})
    assert rep.ok


def test_validate_entries_document_rejects_bad_id():
    e = {
        "id": "bad id!", "source_id": "s1", "external_id": "x",
        "canonical_url": "https://example.com/a", "published_at": "2026-08-03T00:00:00Z",
        "collected_at": "2026-08-03T00:10:00Z", "title": "t", "summary": "s",
        "tags": [], "language": "ja", "source_digest": "sha256:" + "0" * 64,
        "summary_model": {"provider": "p", "model": "m", "prompt_version": "v"},
        "review": {"factual_gate": "passed", "checked_at": "2026-08-03T00:11:00Z"},
    }
    rep = validate.validate_entries_document({"schema_version": 2, "entries": [e]})
    assert not rep.ok


def test_validate_entry_rejects_html_title():
    e = models.Entry(
        id="kn_abc", source_id="s1", external_id="x", canonical_url="https://example.com/a",
        published_at="2026-08-03T00:00:00Z", collected_at="2026-08-03T00:10:00Z",
        title="<script>", summary="s",
        source_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(validate.ValidationError):
        validate.validate_entry(e)


def test_factual_gate_requires_evidence():
    cand = models.Candidate(
        candidate_id="", source_id="s1", source_kind="atom", external_id="g1",
        canonical_url="https://example.com/a", title="t",
        published_at="2026-08-03T00:00:00Z", updated_at="2026-08-03T00:00:00Z",
        retrieved_at="2026-08-03T00:10:00Z", source_text="Apple released iOS 26.6 today.",
    )
    ok = validate.factual_source_gate(
        {"claims": [{"text": "c", "evidence_quotes": ["Apple released iOS 26.6 today."]}]}, cand
    )
    assert ok.ok
    bad = validate.factual_source_gate(
        {"claims": [{"text": "c", "evidence_quotes": ["invented claim"]}]}, cand
    )
    assert not bad.ok
