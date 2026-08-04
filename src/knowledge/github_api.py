"""GitHub REST API 収集（release / commit、allowlist repo のみ）。

read-only fine-grained token または無認証枠を使い、repository contents の
書き込み権限を持たない。全 ghq repository の fetch は行わない。
checkpoint の seen（external_id_hash 等）は collector 側が candidates から生成する。
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Sequence

from .models import Candidate, SourceConfig, SourceCheckpoint
from .feeds import SafeHttpError

API = "https://api.github.com"


@dataclass(frozen=True)
class SourceResult:
    candidates: tuple[Candidate, ...]
    new_etag: str | None = None
    new_last_commit_sha: str | None = None
    ok: bool = True
    error: str | None = None


def _gh_get(path: str, *, token: str | None, timeout: int) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = "application/vnd.github+json"
    req = urllib.request.Request(f"{API}{path}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SafeHttpError(f"github http {e.code}") from e
    except urllib.error.URLError as e:
        raise SafeHttpError(str(e)) from e


def collect_github_releases(
    source: SourceConfig, state: SourceCheckpoint,
    *, token: str | None = None, timeout: int = 20,
) -> SourceResult:
    if not source.repository:
        raise SafeHttpError("github-releases source needs repository")
    data = _gh_get(f"/repos/{source.repository}/releases?per_page=100", token=token, timeout=timeout)
    candidates: list[Candidate] = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        html = r.get("html_url") or ""
        node = r.get("node_id") or ""
        body = r.get("body") or ""
        if len(body.encode("utf-8")) > source.max_enrichment_bytes:
            body = body.encode("utf-8")[: source.max_enrichment_bytes].decode("utf-8", errors="replace")
        cand = Candidate(
            candidate_id="", source_id=source.id, source_kind="github-releases",
            external_id=node or html, canonical_url=html,
            title=r.get("name") or (r.get("tag_name") or ""),
            published_at=r.get("published_at") or "",
            updated_at=r.get("published_at") or "",
            retrieved_at="", source_text=body, priority=source.priority,
        )
        if cand.title or cand.canonical_url:
            candidates.append(cand)
    return SourceResult(candidates=tuple(candidates))


def collect_github_commits(
    source: SourceConfig, state: SourceCheckpoint,
    *, token: str | None = None, timeout: int = 20, since: str | None = None,
) -> SourceResult:
    if not source.repository:
        raise SafeHttpError("github-commits source needs repository")
    path = f"/repos/{source.repository}/commits?per_page=100"
    if since:
        path += f"&since={since}"
    data = _gh_get(path, token=token, timeout=timeout)
    candidates: list[Candidate] = []
    last_sha = state.last_commit_sha
    for c in data if isinstance(data, list) else []:
        if not isinstance(c, dict):
            continue
        sha = c.get("sha") or ""
        html = c.get("html_url") or ""
        commit = c.get("commit") or {}
        msg = ((commit.get("message") or "").splitlines() or [""])[0]
        date = ((commit.get("committer") or {}).get("date")) or ""
        cand = Candidate(
            candidate_id="", source_id=source.id, source_kind="github-commits",
            external_id=sha, canonical_url=html, title=msg,
            published_at=date, updated_at=date, retrieved_at="",
            priority=source.priority,
        )
        if cand.title or cand.canonical_url:
            candidates.append(cand)
        if sha:
            last_sha = sha
    return SourceResult(candidates=tuple(candidates), new_last_commit_sha=last_sha)
