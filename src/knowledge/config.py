"""設定ローダー（config/sources.yml, config/summary.yml）。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import SourceConfig, SummaryConfig


def _defaults(d: Mapping[str, Any]) -> Mapping[str, Any]:
    return d.get("defaults", {}) or {}


def load_sources(path: Path) -> tuple[SourceConfig, ...]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    de = _defaults(raw)
    out: list[SourceConfig] = []
    for s in raw.get("sources", []) or []:
        kind = s["kind"]
        if kind in ("atom", "feed", "html-index"):
            if not s.get("url"):
                raise ValueError(f"source {s['id']}: url required for kind {kind}")
        elif kind == "github-releases":
            if not s.get("repository"):
                raise ValueError(f"source {s['id']}: repository required for github-releases")
        elif kind == "github-commits":
            if not s.get("repository"):
                raise ValueError(f"source {s['id']}: repository required for github-commits")
        elif kind == "hf-model":
            if not s.get("model_id"):
                raise ValueError(f"source {s['id']}: model_id required for hf-model")
        else:
            raise ValueError(f"source {s['id']}: unknown kind {kind!r}")
        out.append(SourceConfig(
            id=s["id"], kind=kind, url=s.get("url", ""),
            allowed_hosts=tuple(s.get("allowed_hosts", [])),
            priority=s.get("priority", de.get("priority", 0)),
            required=s.get("required", de.get("required", False)),
            repository=s.get("repository"),
            model_id=s.get("model_id"),
            events=tuple(s.get("events", [])),
            adapter=s.get("adapter"),
            timeout_seconds=s.get("timeout_seconds", de.get("timeout_seconds", 20)),
            max_response_bytes=s.get("max_response_bytes", de.get("max_response_bytes", 2 * 1024 * 1024)),
            lookback_hours=s.get("lookback_hours", de.get("lookback_hours", 72)),
            max_items_per_source=s.get("max_items_per_source", de.get("max_items_per_source", 100)),
            bootstrap_lookback_days=s.get("bootstrap_lookback_days", de.get("bootstrap_lookback_days", 30)),
            max_enrichment_bytes=s.get("max_enrichment_bytes", de.get("max_enrichment_bytes", 24576)),
            max_enrichment_items=s.get("max_enrichment_items", de.get("max_enrichment_items", 40)),
        ))
    return tuple(out)


def load_summary(path: Path) -> SummaryConfig:
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SummaryConfig(
        provider=d.get("provider", "local-openai-compatible"),
        base_url=d.get("base_url", "http://127.0.0.1:18082/v1"),
        model=d.get("model", ""),
        fallback_model=d.get("fallback_model"),
        allow_fallback=d.get("allow_fallback", False),
        temperature=d.get("temperature", 0.0),
        seed=d.get("seed"),
        max_candidates_per_run=d.get("max_candidates_per_run", 40),
        max_input_bytes_per_candidate=d.get("max_input_bytes_per_candidate", 24576),
        max_total_input_bytes=d.get("max_total_input_bytes", 524288),
        max_output_tokens_per_candidate=d.get("max_output_tokens_per_candidate", 700),
        request_timeout_seconds=d.get("request_timeout_seconds", 120),
        max_retries=d.get("max_retries", 2),
    )
