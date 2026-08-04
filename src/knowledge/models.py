"""不変データモデル（frozen dataclass）。

設計は「dataclass(frozen=True) または Pydantic strict model」を許容しており、
依存を最小化するため frozen dataclass を採用する。時刻は timezone-aware UTC の
ISO8601 文字列（末尾 Z）に統一する。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Entry:
    id: str
    source_id: str
    external_id: str
    canonical_url: str
    published_at: str
    collected_at: str
    title: str
    summary: str
    key_points: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    language: str = "ja"
    source_digest: str = ""
    summary_model: Mapping[str, str] = field(default_factory=dict)
    review: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "external_id": self.external_id,
            "canonical_url": self.canonical_url,
            "published_at": self.published_at,
            "collected_at": self.collected_at,
            "title": self.title,
            "summary": self.summary,
            "key_points": list(self.key_points),
            "tags": list(self.tags),
            "language": self.language,
            "source_digest": self.source_digest,
            "summary_model": dict(self.summary_model),
            "review": dict(self.review),
        }


@dataclass(frozen=True)
class EntriesDocument:
    schema_version: int
    entries: tuple[Entry, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": [e.to_json() for e in self.entries],
        }


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    source_id: str
    source_kind: str
    external_id: str
    canonical_url: str
    title: str
    published_at: str
    updated_at: str
    retrieved_at: str
    author: str = ""
    source_text: str = ""
    source_digest: str = ""
    priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceCheckpoint:
    etag: str | None = None
    last_modified: str | None = None
    last_commit_sha: str | None = None
    seen: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True)
class Checkpoint:
    schema_version: int
    last_success_at: str
    sources: Mapping[str, SourceCheckpoint]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "last_success_at": self.last_success_at,
            "sources": {
                k: {
                    "etag": v.etag,
                    "last_modified": v.last_modified,
                    "last_commit_sha": v.last_commit_sha,
                    "seen": list(v.seen),
                }
                for k, v in self.sources.items()
            },
        }


@dataclass(frozen=True)
class SourceConfig:
    id: str
    kind: str
    url: str
    allowed_hosts: tuple[str, ...]
    priority: int
    required: bool
    repository: str | None = None
    events: tuple[str, ...] = ()
    adapter: str | None = None
    timeout_seconds: int = 20
    max_response_bytes: int = 2 * 1024 * 1024
    lookback_hours: int = 72
    max_items_per_source: int = 100
    bootstrap_lookback_days: int = 30
    max_enrichment_bytes: int = 24576
    max_enrichment_items: int = 40


@dataclass(frozen=True)
class SummaryConfig:
    provider: str
    base_url: str
    model: str
    fallback_model: str | None = None
    allow_fallback: bool = False
    temperature: float = 0.0
    seed: int | None = None
    max_candidates_per_run: int = 40
    max_input_bytes_per_candidate: int = 24576
    max_total_input_bytes: int = 524288
    max_output_tokens_per_candidate: int = 700
    request_timeout_seconds: int = 120
    max_retries: int = 2


def entry_from_json(d: Mapping[str, Any]) -> Entry:
    return Entry(
        id=d["id"],
        source_id=d["source_id"],
        external_id=d["external_id"],
        canonical_url=d["canonical_url"],
        published_at=d["published_at"],
        collected_at=d["collected_at"],
        title=d["title"],
        summary=d["summary"],
        key_points=tuple(d.get("key_points", [])),
        tags=tuple(d.get("tags", [])),
        language=d.get("language", "ja"),
        source_digest=d.get("source_digest", ""),
        summary_model=dict(d.get("summary_model", {})),
        review=dict(d.get("review", {})),
    )


def document_from_json(d: Mapping[str, Any]) -> EntriesDocument:
    return EntriesDocument(
        schema_version=d["schema_version"],
        entries=tuple(entry_from_json(e) for e in d.get("entries", [])),
    )
