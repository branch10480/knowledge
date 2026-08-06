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
    assert "thinking" in req
    assert req["thinking"] == {"type": "disabled"}


def test_summarize_candidates_applies_output_token_limit():
    class RecordingClient:
        def chat(self, request: dict) -> bytes:
            assert request["max_tokens"] == 17
            assert request["thinking"] == {"type": "disabled"}
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


def test_validate_normalizes_scalar_evidence_quotes():
    # LLM が claims[].evidence_quotes を配列でなく単一文字列で返す場合の回帰テスト
    out = {
        "candidate_id": "c1", "title_ja": "タイトル", "summary_ja": "要約",
        "key_points": "k", "tags": "iOS", "insufficient_evidence": False,
        "claims": [{"text": "claim", "evidence_quotes": "Apple released iOS 26.6 today."}],
    }
    parsed = summarizer.validate_summary_output(
        json.dumps(out).encode("utf-8"), _cand(),
    )
    assert parsed.key_points == ("k",)
    assert parsed.tags == ("iOS",)
    assert parsed.claims[0]["evidence_quotes"] == ["Apple released iOS 26.6 today."]


def test_validate_keeps_existing_lists_unchanged():
    # 既存 list は正規化で変化しない
    out = {
        "candidate_id": "c1", "title_ja": "タイトル", "summary_ja": "要約",
        "key_points": ["k1", "k2"], "tags": ["a", "b"], "insufficient_evidence": False,
        "claims": [{"text": "claim", "evidence_quotes": ["q1", "q2"]}],
    }
    parsed = summarizer.validate_summary_output(
        json.dumps(out).encode("utf-8"), _cand(),
    )
    assert parsed.key_points == ("k1", "k2")
    assert parsed.tags == ("a", "b")
    assert parsed.claims[0]["evidence_quotes"] == ["q1", "q2"]


def test_validate_rejects_non_string_scalars():
    # dict/number/boolean/null は正規化せず Schema エラーにする（広く受け入れない）
    base = {"candidate_id": "c1", "title_ja": "t", "summary_ja": "s",
            "tags": [], "insufficient_evidence": False}
    cases = [
        {**base, "key_points": {"a": 1}},
        {**base, "key_points": 42},
        {**base, "key_points": True},
        {**base, "key_points": None},
        {**base, "tags": 3.5},
        {**base, "claims": {"text": "c", "evidence_quotes": ["q"]}},
        {**base, "claims": [{"text": "c", "evidence_quotes": 42}]},
        {**base, "claims": [{"text": "c", "evidence_quotes": {"x": 1}}]},
        {**base, "claims": [{"text": "c", "evidence_quotes": None}]},
        {**base, "key_points": [""]},  # 空文字列は拒否（minLength:1）
    ]
    for out in cases:
        with pytest.raises(summarizer.SummaryError):
            summarizer.validate_summary_output(json.dumps(out).encode("utf-8"), _cand())


def test_parse_chat_response_ok_and_classification():
    # 外側JSON正常・content 正常 → bytes を返す
    ok = {"choices": [{"finish_reason": "stop",
                       "message": {"content": "{\"candidate_id\":\"c1\"}"}}]}
    assert summarizer._parse_chat_response(json.dumps(ok).encode("utf-8")) == \
        b'{"candidate_id":"c1"}'
    # finish_reason=length → 出力上限不足として分類（非再試行）
    with pytest.raises(summarizer.SummaryError) as ei:
        summarizer._parse_chat_response(json.dumps({
            "choices": [{"finish_reason": "length", "message": {"content": "..."}}]}).encode("utf-8"))
    assert "finish_reason=length" in str(ei.value)
    assert not ei.value.retryable
    # 外側JSON破損 → 分類（非再試行）
    with pytest.raises(summarizer.SummaryError) as ei:
        summarizer._parse_chat_response(b"not json")
    assert "outer json corrupt" in str(ei.value)
    # content 空 → 分類（非再試行）
    with pytest.raises(summarizer.SummaryError) as ei:
        summarizer._parse_chat_response(json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": ""}}]}).encode("utf-8"))
    assert "empty llm response" in str(ei.value)


def test_retry_only_on_retryable_errors():
    # Schema違反・ID不一致（非再試行）は client を 1 回だけ呼ぶ
    class FailingSchema:
        calls = 0
        def chat(self, request: dict) -> bytes:
            FailingSchema.calls += 1
            raise summarizer.SummaryError("candidate_id mismatch")

    cfg = models.SummaryConfig(provider="local-openai-compatible",
                               base_url="http://127.0.0.1:18080/v1", model="m",
                               max_retries=3)
    with pytest.raises(summarizer.SummaryError):
        summarizer.summarize_candidates((_cand(),), cfg, client=FailingSchema())
    assert FailingSchema.calls == 1  # 再試行しない

    # timeout・接続（retryable=True）は max_retries+1 回再試行する
    class FailingTimeout:
        calls = 0
        def chat(self, request: dict) -> bytes:
            FailingTimeout.calls += 1
            raise summarizer.SummaryError("llm timeout/connect", retryable=True)

    with pytest.raises(summarizer.SummaryError):
        summarizer.summarize_candidates((_cand(),), cfg, client=FailingTimeout())
    assert FailingTimeout.calls == cfg.max_retries + 1

    # retryable エラー後に成功すれば成功する（blind retry で重ねない）
    class FlakyThenOk:
        calls = 0
        def chat(self, request: dict) -> bytes:
            FlakyThenOk.calls += 1
            if FlakyThenOk.calls == 1:
                raise summarizer.SummaryError("llm timeout/connect", retryable=True)
            return json.dumps({
                "candidate_id": "c1", "title_ja": "タイトル", "summary_ja": "要約",
                "key_points": [], "tags": [], "claims": [], "insufficient_evidence": False,
            }).encode("utf-8")

    outs = summarizer.summarize_candidates((_cand(),), cfg, client=FlakyThenOk())
    assert len(outs) == 1 and FlakyThenOk.calls == 2


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
