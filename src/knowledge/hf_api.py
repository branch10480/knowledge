"""Hugging Face Hub API 収集（hf-model kind）。

モデルページの /api/models/{model_id} を叩き、lastModified（最終更新日時）を
「モデル更新」の候補として返す。リリースノート的な本文は無いため、
source_text はモデルカードの概要（tags / pipeline_tag）を簡潔に埋める。
checkpoint の seen は collector 側が candidates から生成する。
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Sequence

from .models import Candidate, SourceConfig, SourceCheckpoint
from .feeds import SafeHttpError

API = "https://huggingface.co/api/models"


@dataclass(frozen=True)
class SourceResult:
    candidates: tuple[Candidate, ...]
    new_etag: str | None = None
    new_last_commit_sha: str | None = None
    ok: bool = True
    error: str | None = None


def _hf_get(path: str, *, timeout: int) -> dict:
    headers = {"Accept": "application/json"}
    req = urllib.request.Request(f"{API}{path}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SafeHttpError(f"huggingface http {e.code}") from e
    except urllib.error.URLError as e:
        raise SafeHttpError(str(e)) from e


def collect_hf_model(
    source: SourceConfig, state: SourceCheckpoint,
    *, timeout: int = 20,
) -> SourceResult:
    if not source.model_id:
        raise SafeHttpError("hf-model source needs model_id")
    data = _hf_get(f"/{source.model_id}", timeout=timeout)
    if not isinstance(data, dict):
        raise SafeHttpError("huggingface model response is not an object")
    model_id = data.get("id") or source.model_id
    last_modified = data.get("lastModified") or ""
    created_at = data.get("createdAt") or ""
    tags = data.get("tags") or []
    pipeline_tag = data.get("pipeline_tag") or ""
    downloads = data.get("downloads") or 0
    likes = data.get("likes") or 0
    # モデル更新の「本文」は無いので、モデルカードの概要を簡潔に埋める
    summary_parts = []
    if pipeline_tag:
        summary_parts.append(f"pipeline: {pipeline_tag}")
    if tags:
        summary_parts.append("tags: " + ", ".join(str(t) for t in tags[:12]))
    if downloads or likes:
        summary_parts.append(f"downloads: {downloads}, likes: {likes}")
    source_text = " | ".join(summary_parts)
    canonical_url = f"https://huggingface.co/{model_id}"
    title = f"MiniMax-H3 モデル更新 ({last_modified or created_at})"
    if pipeline_tag:
        title += f" [{pipeline_tag}]"
    cand = Candidate(
        candidate_id="", source_id=source.id, source_kind="hf-model",
        external_id=model_id, canonical_url=canonical_url,
        title=title,
        published_at=last_modified or created_at,
        updated_at=last_modified or created_at,
        retrieved_at="", author="", source_text=source_text,
        priority=source.priority,
        metadata={"model_id": model_id, "pipeline_tag": pipeline_tag,
                  "downloads": downloads, "likes": likes},
    )
    return SourceResult(candidates=(cand,))
