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
    system_prompt = req["messages"][0]["content"]
    assert "JSON" in system_prompt
    assert "summary_ja は300文字以内" in system_prompt
    assert "claims は最大2件" in system_prompt
    assert "tags は最大5件" in system_prompt


def test_build_request_contains_output_limit_instructions():
    req = summarizer.build_summary_request(_cand(), prompt_version="summary-v1")
    system_prompt = req["messages"][0]["content"]
    # system promptにoutput token制限の指示が含まれていること
    assert "max_tokens" in system_prompt
    assert "output token" in system_prompt
    # requestにmax_tokensとchat_template_kwargsが含まれていること
    assert "max_tokens" in req
    assert req["max_tokens"] is None  # 初期値はNone
    assert "chat_template_kwargs" in req
    assert req["chat_template_kwargs"] == {"enable_thinking": False}


def test_summarize_candidates_applies_output_token_limit():
    class RecordingClient:
        def chat(self, request: dict) -> bytes:
            assert request["max_tokens"] == 17
            assert request["chat_template_kwargs"] == {"enable_thinking": False}
            return json.dumps({
                "candidate_id": "c1", "title_ja": "タイトル", "summary_ja": "要約",
                "key_points": [], "tags": [], "claims": [], "insufficient_evidence": False,
            }).encode("utf-8")

    cfg = models.SummaryConfig(
        provider="local-openai-compatible",
        base_url="http://127.0.0.1:18080/v1",
        model="m",
        max_output_tokens_per_candidate=17,
    )
    outputs = summarizer.summarize_candidates((_cand(),), cfg, client=RecordingClient())
    assert len(outputs) == 1


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
