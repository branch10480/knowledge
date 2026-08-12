"""hf-model kind のテスト：Hugging Face API 収集。"""
from __future__ import annotations

import pytest

from knowledge import hf_api
from knowledge.models import SourceConfig, SourceCheckpoint


def _src(model_id="MiniMaxAI/MiniMax-H3") -> SourceConfig:
    return SourceConfig(
        id="m1", kind="hf-model", url="", allowed_hosts=(),
        priority=70, required=False, model_id=model_id,
    )


def test_collect_hf_model_returns_candidate():
    # 実 API を叩かず、_hf_get を差し替えて決定的に検証する
    captured = {}

    def fake_get(path, *, timeout):
        captured["path"] = path
        return {
            "id": "MiniMaxAI/MiniMax-H3",
            "lastModified": "2026-08-10T10:31:07.000Z",
            "createdAt": "2026-07-28T10:45:18.000Z",
            "pipeline_tag": "text-to-video",
            "tags": ["diffusers", "safetensors", "text-to-video"],
            "downloads": 47468,
            "likes": 3472,
        }

    orig = hf_api._hf_get
    hf_api._hf_get = fake_get
    try:
        res = hf_api.collect_hf_model(_src(), SourceCheckpoint(), timeout=20)
    finally:
        hf_api._hf_get = orig

    assert captured["path"] == "/MiniMaxAI/MiniMax-H3"
    assert res.ok
    assert len(res.candidates) == 1
    c = res.candidates[0]
    assert c.source_kind == "hf-model"
    assert c.external_id == "MiniMaxAI/MiniMax-H3"
    assert c.canonical_url == "https://huggingface.co/MiniMaxAI/MiniMax-H3"
    assert c.published_at == "2026-08-10T10:31:07.000Z"
    assert "text-to-video" in c.title
    assert "pipeline: text-to-video" in c.source_text
    assert c.metadata["downloads"] == 47468


def test_collect_hf_model_requires_model_id():
    with pytest.raises(hf_api.SafeHttpError):
        hf_api.collect_hf_model(_src(model_id=""), SourceCheckpoint(), timeout=20)


def test_collect_hf_model_http_error():
    def fake_get(path, *, timeout):
        raise hf_api.SafeHttpError("huggingface http 404")

    orig = hf_api._hf_get
    hf_api._hf_get = fake_get
    try:
        with pytest.raises(hf_api.SafeHttpError):
            hf_api.collect_hf_model(_src(), SourceCheckpoint(), timeout=20)
    finally:
        hf_api._hf_get = orig
