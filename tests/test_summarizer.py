"""summarizer のテスト：リクエスト生成・出力検証。"""
from __future__ import annotations
import json

import pytest

from knowledge import models, summarizer


def _cand(cid: str = "c1") -> models.Candidate:
    return models.Candidate(
        candidate_id=cid, source_id="s1", source_kind="atom", external_id="g1",
        canonical_url="https://example.com/a", title="t", published_at="2026-08-03T00:00:00Z",
        updated_at="2026-08-03T00:00:00Z", retrieved_at="2026-08-03T00:10:00Z",
        source_text="Apple released iOS 26.6 today.",
    )


def test_build_request_uses_json_schema():
    req = summarizer.build_summary_request(_cand(), prompt_version="summary-v1")
    assert req["temperature"] == 0
    assert "source_text" in req["messages"][1]["content"]
    # response_format はローカル LLM が非対応のため外す。JSON 要求はシステムプロンプトで指示
    assert "response_format" not in req
    assert "JSON" in req["messages"][0]["content"]


def test_validate_output_ok():
    out = {
        "candidate_id": "c1", "title_ja": "タイトル", "summary_ja": "要約",
        "key_points": ["k"], "tags": ["iOS"], "claims": [], "insufficient_evidence": False,
    }
    parsed = summarizer.validate_summary_output(
        json.dumps(out).encode("utf-8"), _cand(),
    )
    assert parsed.title_ja == "タイトル"


def test_validate_rejects_malformed_and_mismatch():
    with pytest.raises(summarizer.SummaryError):
        summarizer.validate_summary_output(b"not json", _cand())
    bad = {"candidate_id": "OTHER", "title_ja": "t", "summary_ja": "s",
           "tags": [], "insufficient_evidence": False}
    with pytest.raises(summarizer.SummaryError):
        summarizer.validate_summary_output(json.dumps(bad).encode("utf-8"), _cand())


def test_validate_rejects_html():
    out = {"candidate_id": "c1", "title_ja": "<script>", "summary_ja": "s",
           "tags": [], "insufficient_evidence": False}
    with pytest.raises(summarizer.SummaryError):
        summarizer.validate_summary_output(json.dumps(out).encode("utf-8"), _cand())


def test_restricted_client_requires_loopback():
    with pytest.raises(summarizer.SummaryError):
        summarizer.RestrictedLlmClient("https://example.com/v1", "m")
    # loopback は OK
    summarizer.RestrictedLlmClient("http://127.0.0.1:18080/v1", "m")
