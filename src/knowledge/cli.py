"""knowledge CLI。

設計 5.1 の command 群。各 command は例外を握りつぶさず非 0 で終了する。
JSON log は stdout、診断は stderr。secret や記事全文を log に出さない。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import builder, collector, config, identity, inference, jobs, models, repository, summarizer, validate

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT
EXPECTED_ORIGIN_URL = "https://github.com/branch10480/knowledge.git"


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _load_document(entries_path: Path) -> models.EntriesDocument:
    return _load_document_bytes(entries_path.read_bytes())


def _load_document_bytes(payload: bytes) -> models.EntriesDocument:
    raw = json.loads(payload.decode("utf-8"))
    report = validate.validate_entries_document(raw)
    report.raise_if_bad()
    return models.document_from_json(raw)


def _load_checkpoint(cp_path: Path) -> models.Checkpoint:
    raw = json.loads(cp_path.read_text(encoding="utf-8")) if cp_path.exists() else {
        "schema_version": 1, "last_success_at": "1970-01-01T00:00:00Z", "sources": {}
    }
    validate.validate_checkpoint(raw).raise_if_bad()
    return models.Checkpoint(
        schema_version=raw["schema_version"],
        last_success_at=raw["last_success_at"],
        sources={k: models.SourceCheckpoint(**v) for k, v in raw.get("sources", {}).items()},
    )


def _load_checkpoint_bytes(payload: bytes) -> models.Checkpoint:
    raw = json.loads(payload.decode("utf-8"))
    validate.validate_checkpoint(raw).raise_if_bad()
    return models.Checkpoint(
        schema_version=raw["schema_version"],
        last_success_at=raw["last_success_at"],
        sources={k: models.SourceCheckpoint(**v) for k, v in raw.get("sources", {}).items()},
    )


def collect_command(*, config_path: Path, checkpoint_path: Path, output_path: Path, run_started_at: str,
                    summary_config_path: Path) -> int:
    sources = config.load_sources(config_path)
    required_ids = {s.id for s in sources if s.required}
    cp = _load_checkpoint(checkpoint_path)
    http = collector.SafeHttpClient()
    # 要約枠（max_candidates_per_run）を summary.yml から読み、selected を確定する
    summary_cfg = config.load_summary(summary_config_path)
    res = collector.collect_all(
        sources, cp, run_started_at=run_started_at, http=http,
        summary_quota=summary_cfg.max_candidates_per_run,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({
            "candidates": [c.__dict__ for c in res.candidates],
            "proposed_checkpoint": res.proposed_checkpoint.to_json(),
            "stats": [s.__dict__ for s in res.source_stats],
            "selected_candidate_ids": list(res.selected_candidate_ids),
            "deferred_candidate_ids": list(res.deferred_candidate_ids),
        }, ensure_ascii=False), encoding="utf-8",
    )
    # required source の失敗は checkpoint を進めない（failure 不変性）
    failed_required = [s.source_id for s in res.source_stats if not s.ok and s.source_id in required_ids]
    if failed_required:
        print(json.dumps({"ok": False, "error": f"required source failed: {failed_required}"}))
        return 1
    print(json.dumps({"ok": True, "candidates": len(res.candidates),
                      "sources_ok": sum(1 for s in res.source_stats if s.ok)}))
    return 0


def summarize_command(*, candidates_path: Path, output_path: Path, config_path: Path) -> int:
    raw = json.loads(candidates_path.read_text(encoding="utf-8"))
    cands = tuple(models.Candidate(**c) for c in raw["candidates"])
    cfg = config.load_summary(config_path)
    client = summarizer.RestrictedLlmClient(cfg.base_url, cfg.model,
                                            timeout=cfg.request_timeout_seconds, seed=cfg.seed)
    outs = summarizer.summarize_candidates(cands, cfg, client=client)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([o.__dict__ for o in outs], ensure_ascii=False), encoding="utf-8",
    )
    print(json.dumps({"ok": True, "summaries": len(outs)}))
    return 0


def _entry_from(candidate: models.Candidate, s: summarizer.SummaryOutput, collected_at: str,
                allowed_hosts: tuple[str, ...]) -> models.Entry | None:
    from .validate import validate_url, factual_source_gate
    # 公開日不明の記事を「最近収集」として誤って表示しない（collected_at を公開日に偽装しない）
    if not candidate.published_at:
        return None
    # 生の日付文字列（"July 20, 2026" 等）を ISO8601 UTC に正規化（validate が要求）
    from .collector import _utc_parse, _utc_str
    published_at = _utc_str(_utc_parse(candidate.published_at))
    canonical = candidate.canonical_url
    if not canonical:
        raise ValueError("candidate has no canonical_url")
    validate_url(canonical, allowed_hosts=allowed_hosts or None)
    # factual gate: claims の evidence が source_text に正規化一致するか実際に検証する
    gate = factual_source_gate(s.__dict__, candidate)
    if not gate.ok:
        raise ValueError(f"factual gate failed: {gate.reason}")
    import hashlib
    digest = "sha256:" + hashlib.sha256(
        (s.summary_ja + "\n" + candidate.source_text[:2000]).encode("utf-8")).hexdigest()
    return models.Entry(
        id=identity.make_entry_id(candidate),
        source_id=candidate.source_id,
        external_id=candidate.external_id,
        canonical_url=canonical,
        published_at=published_at,
        collected_at=collected_at,
        title=s.title_ja,
        summary=s.summary_ja,
        key_points=s.key_points,
        tags=s.tags,
        language="ja",
        source_digest=digest,
        summary_model={"provider": "local-openai-compatible", "model": "deepseek-v4-flash",
                       "prompt_version": "summary-v1"},
        review={"factual_gate": "passed", "checked_at": collected_at},
    )


def _merge_documents(
    *,
    doc: models.EntriesDocument,
    cp: models.Checkpoint,
    cdata: dict,
    sdata: list,
    sources: tuple[models.SourceConfig, ...],
    merged_at: str | None = None,
) -> tuple[models.EntriesDocument, models.Checkpoint, int]:
    """Merge already-opened, validated inputs without reopening workspace paths."""

    host_map = {s.id: s.allowed_hosts for s in sources}
    cands = tuple(models.Candidate(**c) for c in cdata["candidates"])
    proposed = models.Checkpoint(
        schema_version=1, last_success_at=cp.last_success_at,
        sources={k: models.SourceCheckpoint(**v) for k, v in cdata["proposed_checkpoint"]["sources"].items()},
    )
    summary_fields = {
        "candidate_id",
        "title_ja",
        "summary_ja",
        "key_points",
        "tags",
        "claims",
        "insufficient_evidence",
    }
    smap = {
        item["candidate_id"]: summarizer.SummaryOutput(**{
            field: item[field] for field in summary_fields
        })
        for item in sdata
    }

    additions = []
    seen_by_source: dict[str, list] = {}
    merged_at = merged_at or _utcnow()
    for c in cands:
        s = smap.get(c.candidate_id)
        if s is None or s.insufficient_evidence:
            continue
        if identity.is_known(c, doc, cp):
            continue
        e = _entry_from(c, s, merged_at, host_map.get(c.source_id, ()))
        if e is None:
            # 公開日不明（published_at 空）は「最近収集」として追加しない
            continue
        additions.append(e)
        # 検証・追加まで完了した candidate を seen に記録（次の run で再処理しない）
        seen_by_source.setdefault(c.source_id, []).append({
            "external_id_hash": "sha256:" + hashlib.sha256(c.external_id.encode("utf-8")).hexdigest(),
            "canonical_url_hash": "sha256:" + hashlib.sha256(c.canonical_url.encode("utf-8")).hexdigest(),
            "first_seen_at": merged_at,
        })

    merged = repository.merge_entries(doc, additions)

    # 検証済み candidate を proposed.sources の seen に追加
    proposed_src = dict(proposed.sources)
    for sid, items in seen_by_source.items():
        cur = proposed_src.get(sid)
        if cur is None:
            proposed_src[sid] = models.SourceCheckpoint(seen=tuple(items))
        else:
            proposed_src[sid] = models.SourceCheckpoint(
                etag=cur.etag, last_modified=cur.last_modified,
                last_commit_sha=cur.last_commit_sha, seen=tuple(cur.seen) + tuple(items),
            )
    proposed = models.Checkpoint(
        schema_version=1, last_success_at=cp.last_success_at, sources=proposed_src,
    )

    # deferred（未処理候補）がある場合は watermark を進めない（再処理保証）
    deferred_ids = cdata.get("deferred_candidate_ids", [])
    if deferred_ids:
        new_cp = models.Checkpoint(
            schema_version=1, last_success_at=cp.last_success_at, sources=proposed.sources,
        )
    else:
        new_cp = models.Checkpoint(
            schema_version=1, last_success_at=merged_at, sources=proposed.sources,
        )
    return merged, new_cp, len(additions)


def merge_command(*, entries_path: Path, checkpoint_path: Path,
                  candidates_path: Path, summaries_path: Path, config_path: Path,
                  merged_output: Path | None = None, checkpoint_output: Path | None = None,
                  ) -> int:
    merged, new_cp, additions = _merge_documents(
        doc=_load_document(entries_path),
        cp=_load_checkpoint(checkpoint_path),
        cdata=json.loads(candidates_path.read_text(encoding="utf-8")),
        sdata=json.loads(summaries_path.read_text(encoding="utf-8")),
        sources=config.load_sources(config_path),
    )
    if merged_output is not None:
        jobs.write_json_file(merged_output, merged.to_json())
    if checkpoint_output is not None:
        jobs.write_json_file(checkpoint_output, new_cp.to_json())
    print(json.dumps({"ok": True, "added": additions, "dry_run": True}))
    return 0


def _build_document(doc: models.EntriesDocument, *, output_dir: Path) -> int:
    m = builder.build_site(
        doc,
        templates_dir=RUNTIME_ROOT / "templates",
        static_dir=RUNTIME_ROOT / "static",
        output_dir=output_dir,
        built_at=_utcnow(),
        repo_root=REPO_ROOT,
    )
    print(json.dumps({"ok": True, "entry_count": m.entry_count, "dist": str(output_dir)}))
    return 0


def build_command(*, entries_path: Path, output_dir: Path) -> int:
    return _build_document(_load_document(entries_path), output_dir=output_dir)


def _check_document(doc: models.EntriesDocument, *, dist_dir: Path) -> int:
    pages = list(dist_dir.glob("entry/*.html"))
    if len(pages) != len(doc.entries):
        print(json.dumps({"ok": False, "error": f"entry pages {len(pages)} != {len(doc.entries)}"}))
        return 1
    manifest_path = dist_dir / "manifest.json"
    if manifest_path.exists():
        try:
            man = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_files = man["files"]
            if not isinstance(manifest_files, dict) or any(
                not isinstance(name, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for name, digest in manifest_files.items()
            ):
                raise ValueError("invalid manifest file map")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            print(json.dumps({"ok": False, "error": f"invalid build manifest: {error}"}))
            return 1
        on_disk = {
            p.relative_to(dist_dir).as_posix()
            for p in dist_dir.rglob("*")
            if p.is_file()
            and p.name != "manifest.json"
            and ".git" not in p.parts
        }
        in_man = set(manifest_files)
        missing = in_man - on_disk
        extra = on_disk - in_man
        if missing or extra:
            print(json.dumps({"ok": False, "error": {"missing": list(missing), "extra": list(extra)}}))
            return 1
        mismatched = sorted(
            relative
            for relative, expected in manifest_files.items()
            if hashlib.sha256((dist_dir / relative).read_bytes()).hexdigest() != expected
        )
        if mismatched:
            print(json.dumps({"ok": False, "error": {"digest_mismatch": mismatched}}))
            return 1
    from .links import check_internal_links
    rep = check_internal_links(dist_dir)
    if not rep.ok:
        print(json.dumps({"ok": False, "error": {"broken": rep.broken}}))
        return 1
    print(json.dumps({"ok": True, "entry_count": len(doc.entries)}))
    return 0


def check_command(*, entries_path: Path, dist_dir: Path) -> int:
    return _check_document(_load_document(entries_path), dist_dir=dist_dir)


def validate_atom_command(feed_path: Path) -> int:
    from xml.etree import ElementTree as ET
    root = ET.fromstring(feed_path.read_bytes())
    if "http://www.w3.org/2005/Atom" not in root.tag:
        print(json.dumps({"ok": False, "error": "root not Atom namespace"}))
        return 1
    entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    rss = root.findall(".//{http://www.w3.org/2005/Atom}item")
    if rss:
        print(json.dumps({"ok": False, "error": "RSS item element present in Atom feed"}))
        return 1
    print(json.dumps({"ok": True, "atom_entries": len(entries)}))
    return 0


def validate_data_command(entries_path: Path) -> int:
    doc = _load_document(entries_path)
    print(json.dumps({"ok": True, "entries": len(doc.entries)}))
    return 0


def check_inference_idle_command() -> int:
    try:
        busy_ports = inference.busy_inference_ports()
    except inference.InferenceCheckError:
        print(json.dumps({"ok": False, "error": "inference idle check failed"}))
        return 1
    if busy_ports:
        print(json.dumps({
            "ok": False,
            "error": "local inference is busy; retry later",
            "busy_ports": list(busy_ports),
        }))
        return 75
    print(json.dumps({"ok": True, "inference_idle": True}))
    return 0


def _job_store() -> jobs.JobStore:
    return jobs.JobStore(REPO_ROOT / ".work/jobs")


def _git_check(*args: str) -> bool:
    status, _ = repository._git_optional(REPO_ROOT, *args)
    return status == 0


def _assert_job_repo_ready(
    *, expected_remote_url: str = EXPECTED_ORIGIN_URL,
    expected_remote_oid: str | None = None,
) -> tuple[str, str]:
    try:
        branch = repository._git(REPO_ROOT, "symbolic-ref", "--short", "HEAD").strip()
    except repository.RepositoryError as error:
        raise jobs.JobError("Knowledge job could not resolve its branch") from error
    if branch != "main":
        raise jobs.JobError("Knowledge job requires the main branch")
    if not _git_check("diff", "--quiet", "--"):
        raise jobs.JobError("Knowledge job requires a clean tracked worktree")
    if not _git_check("diff", "--cached", "--quiet", "--"):
        raise jobs.JobError("Knowledge job requires an empty staging area")
    status_code, status_output = repository._git_optional(
        REPO_ROOT, "status", "--porcelain=v1", "--untracked-files=normal",
    )
    if status_code != 0 or status_output:
        raise jobs.JobError("Knowledge job requires a fully clean worktree")
    try:
        remote_url = repository._git(
            REPO_ROOT, "remote", "get-url", "origin",
        ).strip()
    except repository.RepositoryError as error:
        raise jobs.JobError("Knowledge job could not resolve canonical remote") from error
    if remote_url != expected_remote_url:
        raise jobs.JobError("Knowledge job canonical remote URL changed")
    try:
        remote_oid = repository._remote_oid(REPO_ROOT, expected_remote_url, "main")
    except repository.RepositoryError as error:
        raise jobs.JobError("Knowledge job could not resolve remote main OID") from error
    if not remote_oid:
        raise jobs.JobError("Knowledge job could not resolve remote main OID")
    try:
        local_oid = repository._git(REPO_ROOT, "rev-parse", "HEAD").strip()
    except repository.RepositoryError as error:
        raise jobs.JobError("Knowledge job could not resolve local HEAD OID") from error
    if local_oid != remote_oid:
        raise jobs.JobError("Knowledge job requires local HEAD to match remote main")
    if expected_remote_oid is not None and remote_oid != expected_remote_oid:
        raise jobs.JobError("Knowledge job remote main OID changed")
    return local_oid, remote_oid


def job_start_command(
    *,
    idempotency_key: str,
    origin_session_id: str,
    origin_turn_id: str,
    origin_authority_kind: str,
) -> int:
    store = _job_store()
    try:
        existing = store.find_by_idempotency_key(idempotency_key)
        starting_head = None
        starting_remote_oid = None
        if existing is None:
            starting_head, starting_remote_oid = _assert_job_repo_ready()

        def collect_for_job(output_path: Path, run_started_at: str) -> int:
            return collect_command(
                config_path=REPO_ROOT / "config/sources.yml",
                checkpoint_path=REPO_ROOT / "data/checkpoint.json",
                output_path=output_path,
                run_started_at=run_started_at,
                summary_config_path=REPO_ROOT / "config/summary.yml",
            )

        state, reused = jobs.prepare_job(
            repo_root=REPO_ROOT,
            store=store,
            idempotency_key=idempotency_key,
            collect=collect_for_job,
            origin_session_id=origin_session_id,
            origin_turn_id=origin_turn_id,
            origin_authority_kind=origin_authority_kind,
            canonical_remote_url=EXPECTED_ORIGIN_URL,
            starting_head=starting_head,
            starting_remote_oid=starting_remote_oid,
        )
        print(json.dumps({
            "ok": True,
            "status": "deferred",
            "job_id": state["job_id"],
            "phase": state["phase"],
            "reused": reused,
            "runner_pid": state.get("runner_pid"),
        }, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({
            "ok": False,
            "error": type(error).__name__,
            "message": jobs.safe_error_message(error),
        }, ensure_ascii=False))
        return 1


def _public_job_state(state: dict) -> dict:
    return {
        key: state.get(key)
        for key in (
            "job_id",
            "phase",
            "created_at",
            "updated_at",
            "run_started_at",
            "origin_authority_kind",
            "selected_candidate_ids",
            "completed_candidate_ids",
            "attempts",
            "cancel_requested",
            "result",
            "error",
        )
    }


def _session_job(store: jobs.JobStore, job_id: str, origin_session_id: str) -> dict:
    state = store.load(job_id)
    if not origin_session_id or state.get("origin_session_id") != origin_session_id:
        raise jobs.JobError("job does not belong to the requesting session")
    return state


def job_status_command(*, job_id: str | None, origin_session_id: str | None) -> int:
    store = _job_store()
    try:
        state = (
            _session_job(store, job_id, str(origin_session_id or ""))
            if job_id
            else store.latest()
        )
        if state is not None and origin_session_id and state.get("origin_session_id") != origin_session_id:
            state = None
        if state is None:
            print(json.dumps({"ok": True, "job": None}))
            return 0
        print(json.dumps({"ok": True, "job": _public_job_state(state)}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, ensure_ascii=False))
        return 1


def job_cancel_command(*, job_id: str, origin_session_id: str) -> int:
    try:
        _session_job(_job_store(), job_id, origin_session_id)
        state = jobs.cancel_job(store=_job_store(), job_id=job_id)
        print(json.dumps({"ok": True, "job": _public_job_state(state)}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, ensure_ascii=False))
        return 1


def _finalize_job(job_dir: Path, *, run_test_suite: bool = True) -> dict:
    import subprocess

    store = _job_store()
    state = store.load(job_dir.name)
    head = repository._git(REPO_ROOT, "rev-parse", "HEAD").strip()
    if head != state.get("starting_head"):
        raise jobs.JobError("repository HEAD changed while the job was running")

    _assert_job_repo_ready(
        expected_remote_url=str(state.get("canonical_remote_url")),
        expected_remote_oid=str(state.get("starting_remote_oid")),
    )
    entries_bytes = jobs.read_regular_bytes(REPO_ROOT / "data/entries.json")
    checkpoint_source_bytes = jobs.read_regular_bytes(REPO_ROOT / "data/checkpoint.json")
    sources_bytes = jobs.read_regular_bytes(REPO_ROOT / "config/sources.yml")
    collected_candidates_path = job_dir / "candidates.json"
    candidates_path = job_dir / "final-candidates.json"
    summaries_path = job_dir / "summaries.json"
    collected_candidates_bytes = jobs.read_regular_bytes(collected_candidates_path)
    candidates_bytes = jobs.read_regular_bytes(candidates_path)
    summaries_bytes = jobs.read_regular_bytes(summaries_path)

    checkpoint_digest = "sha256:" + hashlib.sha256(checkpoint_source_bytes).hexdigest()
    if checkpoint_digest != state.get("checkpoint_sha256"):
        raise jobs.JobError("checkpoint changed while the job was running")
    if "sha256:" + hashlib.sha256(sources_bytes).hexdigest() != state.get(
        "sources_config_sha256"
    ):
        raise jobs.JobError("sources config changed while the job was running")
    candidates_digest = "sha256:" + hashlib.sha256(candidates_bytes).hexdigest()
    if candidates_digest != state.get("final_candidates_sha256"):
        raise jobs.JobError("final candidates changed while the job was running")
    summaries_digest = "sha256:" + hashlib.sha256(summaries_bytes).hexdigest()
    if summaries_digest != state.get("summaries_sha256"):
        raise jobs.JobError("summaries changed while the job was running")

    collected_digest = "sha256:" + hashlib.sha256(collected_candidates_bytes).hexdigest()
    if collected_digest != state.get("candidates_sha256"):
        raise jobs.JobError("collected candidates changed while the job was running")
    jobs._validated_collect_receipt(
        job_dir=job_dir, state=state, candidates_sha256=collected_digest,
    )
    collected_data = json.loads(collected_candidates_bytes.decode("utf-8"))
    final_candidate_data = json.loads(candidates_bytes.decode("utf-8"))
    candidate_ids, selected = jobs._collected_candidate_metadata(collected_data)
    final_candidate_ids, final_selected = jobs._collected_candidate_metadata(
        final_candidate_data
    )
    if (
        candidate_ids != final_candidate_ids
        or selected != final_selected
        or selected != list(state.get("selected_candidate_ids", []))
        or any(
            collected_data.get(field) != final_candidate_data.get(field)
            for field in ("candidates", "proposed_checkpoint", "stats")
        )
    ):
        raise jobs.JobError("final candidates do not derive from collected candidates")

    summaries_data = json.loads(summaries_bytes.decode("utf-8"))
    if not isinstance(summaries_data, list) or len(summaries_data) != len(selected):
        raise jobs.JobError("summary aggregate does not match selected candidates")
    candidates_by_id = {
        item["candidate_id"]: models.Candidate(**item)
        for item in collected_data["candidates"]
    }
    expected_summaries = []
    for candidate_id in selected:
        receipt = json.loads(jobs.read_regular_bytes(
            job_dir / "summaries" / jobs._receipt_name(candidate_id)
        ).decode("utf-8"))
        candidate = candidates_by_id[candidate_id]
        if (
            receipt.get("candidate_id") != candidate_id
            or receipt.get("candidate_sha256") != jobs._candidate_sha256(candidate)
            or receipt.get("summary_config_sha256") != state.get("summary_config_sha256")
        ):
            raise jobs.JobError("summary receipt binding changed before finalization")
        expected_summaries.append(receipt)
    if summaries_data != expected_summaries:
        raise jobs.JobError("summary aggregate differs from durable receipts")
    insufficient_ids = [
        item["candidate_id"]
        for item in summaries_data
        if item.get("insufficient_evidence") is True
    ]
    expected_deferred = list(dict.fromkeys([
        *collected_data.get("deferred_candidate_ids", []), *insufficient_ids,
    ]))
    if final_candidate_data.get("deferred_candidate_ids", []) != expected_deferred:
        raise jobs.JobError("final deferred candidates do not match summary receipts")

    merged_path = job_dir / "merged.json"
    checkpoint_path = job_dir / "checkpoint.json"
    dist_dir = job_dir / "dist"
    merged, checkpoint, additions = _merge_documents(
        doc=_load_document_bytes(entries_bytes),
        cp=_load_checkpoint_bytes(checkpoint_source_bytes),
        cdata=final_candidate_data,
        sdata=summaries_data,
        sources=config.parse_sources(sources_bytes.decode("utf-8")),
        merged_at=str(state["run_started_at"]),
    )
    merged_bytes = _canonical_json_bytes(merged.to_json())
    checkpoint_bytes = _canonical_json_bytes(checkpoint.to_json())
    jobs.write_bytes_file(merged_path, merged_bytes)
    jobs.write_bytes_file(checkpoint_path, checkpoint_bytes)
    print(json.dumps({"ok": True, "added": additions, "dry_run": True}))
    if _build_document(merged, output_dir=dist_dir) != 0:
        raise jobs.JobError("build failed")
    if validate_atom_command(dist_dir / "feed.xml") != 0:
        raise jobs.JobError("Atom validation failed")
    if _check_document(merged, dist_dir=dist_dir) != 0:
        raise jobs.JobError("build checks failed")

    git_status, _ = repository._git_optional(REPO_ROOT, "diff", "--check")
    if git_status != 0:
        raise jobs.JobError("finalization check failed: git diff --check")
    if run_test_suite:
        test_command = [sys.executable, "-m", "pytest", "-q", str(RUNTIME_ROOT / "tests")]
        completed = subprocess.run(test_command, cwd=RUNTIME_ROOT, timeout=900, check=False)
        if completed.returncode != 0:
            raise jobs.JobError("finalization check failed: pytest")

    scan_command = [
        str(RUNTIME_ROOT / "scripts/scan-secrets.sh"),
        "--json",
        "--paths",
        str(merged_path),
        str(checkpoint_path),
        str(dist_dir),
    ]
    completed = subprocess.run(
        scan_command, cwd=RUNTIME_ROOT, timeout=900, check=False,
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise jobs.JobError("finalization check failed: scan-secrets.sh")
    try:
        scan_manifest = json.loads(completed.stdout.splitlines()[-1])
        scanned = {
            item["label"]: item["sha256"]
            for item in scan_manifest["files"]
            if isinstance(item, dict)
        }
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise jobs.JobError("secret scanner returned an invalid exact-byte manifest") from error
    expected_scanned = {
        str(merged_path): "sha256:" + hashlib.sha256(merged_bytes).hexdigest(),
        str(checkpoint_path): "sha256:" + hashlib.sha256(checkpoint_bytes).hexdigest(),
    }
    if any(scanned.get(path) != digest for path, digest in expected_scanned.items()):
        raise jobs.JobError("secret scanner did not inspect the exact publication bytes")

    if store.load(job_dir.name).get("cancel_requested"):
        raise jobs.JobCancelled("job was cancelled before publication readiness")
    result = {
        "ready_for_publish": True,
        "job_id": job_dir.name,
        "starting_head": head,
        "merged_sha256": "sha256:" + hashlib.sha256(merged_bytes).hexdigest(),
        "checkpoint_output_sha256": "sha256:" + hashlib.sha256(checkpoint_bytes).hexdigest(),
        "candidates_sha256": candidates_digest,
        "summaries_sha256": summaries_digest,
        "remote_url": state.get("canonical_remote_url"),
        "branch": "main",
        "upstream_oid": state.get("starting_remote_oid"),
    }
    jobs.write_finalize_receipt(
        job_dir=job_dir,
        job_id=job_dir.name,
        summaries_sha256=str(store.load(job_dir.name)["summaries_sha256"]),
        result=result,
    )
    return result


def publication_binding(*, job_id: str, state: dict, result: dict) -> str:
    """Return the canonical manifest consumed by Hermes core exactly once."""

    publication = {
        "schema_version": 1,
        "kind": "knowledge-publication",
        "job_id": job_id,
        "starting_head": state.get("starting_head"),
        "merged_sha256": result.get("merged_sha256"),
        "checkpoint_output_sha256": result.get("checkpoint_output_sha256"),
        "candidates_sha256": result.get("candidates_sha256"),
        "summaries_sha256": result.get("summaries_sha256"),
    }
    jobs.validate_publication_manifest(publication)
    return json.dumps(publication, sort_keys=True, separators=(",", ":"))


def _validated_authority_binding(
    *, job_id: str, state: dict, publication: str, authority_binding: str,
) -> str:
    try:
        decoded = json.loads(authority_binding)
    except (TypeError, json.JSONDecodeError) as error:
        raise jobs.JobError("publication authority binding is invalid") from error
    if not isinstance(decoded, dict) or json.dumps(
        decoded, sort_keys=True, separators=(",", ":")
    ) != authority_binding:
        raise jobs.JobError("publication authority binding is not canonical")
    if decoded.get("kind") == "knowledge-publication":
        if authority_binding != publication:
            raise jobs.JobError("direct publication authority manifest changed")
        return authority_binding
    if (
        set(decoded) == {"schema_version", "kind", "job_id", "starting_head"}
        and decoded.get("schema_version") == 1
        and decoded.get("kind") == "knowledge-scheduled-job-grant"
        and decoded.get("job_id") == job_id
        and decoded.get("starting_head") == state.get("starting_head")
    ):
        return authority_binding
    raise jobs.JobError("scheduled publication authority grant changed")


def _complete_publication_state(
    *, store: jobs.JobStore, job_id: str, ready_result: dict, commit: str,
) -> dict:
    current = store.load(job_id)
    if current.get("phase") == "completed":
        if (current.get("result") or {}).get("commit") != commit:
            raise jobs.JobError("completed job commit does not match journal")
        return current
    if current.get("phase") != "ready_for_publish":
        raise jobs.JobError("publication recovery requires the exact READY job")
    published = {**ready_result, "commit": commit, "published_at": _utcnow()}
    return store.update(
        job_id, phase="completed", result=published,
        completion_event_id=jobs._completion_event_id({
            "job_id": job_id, "phase": "completed", "result": published,
        }),
    )


def reconcile_push_verified_publication() -> dict | None:
    """Close exact post-push bookkeeping without issuing new authority or pushing."""

    store = _job_store()
    with repository.finalize_lock(REPO_ROOT):
        journal = repository._read_journal(REPO_ROOT)
        if journal is None or journal.get("stage") != "push_verified":
            return None
        job_id = str(journal.get("publication_id", ""))
        state = store.load(job_id)
        if state.get("phase") not in {"ready_for_publish", "completed"}:
            raise jobs.JobError("push-verified journal has no recoverable exact job")
        result = state.get("result")
        if not isinstance(result, dict):
            raise jobs.JobError("push-verified job has no publication manifest")
        commit = str(journal.get("commit_oid", ""))
        output_digests = journal.get("output_digests")
        if not commit or not isinstance(output_digests, dict):
            raise jobs.JobError("push-verified journal identity is invalid")
        binding = publication_binding(job_id=job_id, state=state, result=result)
        expected_binding_digest = "sha256:" + hashlib.sha256(
            binding.encode("utf-8")
        ).hexdigest()
        if any((
            journal.get("remote_oid") != commit,
            journal.get("publication_binding_sha256") != expected_binding_digest,
            journal.get("starting_head") != state.get("starting_head"),
            journal.get("expected_remote_url") != result.get("remote_url"),
            journal.get("expected_upstream_oid") != result.get("upstream_oid"),
            journal.get("branch") != result.get("branch"),
            output_digests.get("data/entries.json") != result.get("merged_sha256"),
            output_digests.get("data/checkpoint.json")
            != result.get("checkpoint_output_sha256"),
        )):
            raise jobs.JobError("push-verified publication binding changed")
        if result.get("remote_url") != EXPECTED_ORIGIN_URL or result.get("branch") != "main":
            raise jobs.JobError("push-verified repository binding changed")
        repository._validate_committed_record(
            REPO_ROOT, journal, require_active_head=False,
        )
        if repository._remote_oid(
            REPO_ROOT, str(result["remote_url"]), str(result["branch"]),
        ) != commit:
            raise jobs.JobError("push-verified publication remote OID changed")
        completed = _complete_publication_state(
            store=store, job_id=job_id, ready_result=result, commit=commit,
        )
        repository._write_journal(REPO_ROOT, {**journal, "stage": "closed"})
        return completed


def publish_ready_job(
    *, job_id: str, capability: str, authority_binding: str,
) -> dict:
    """Publish one exact READY job after a core-owned direct-user authority claim.

    This is intentionally a Python API, not a public ``knowledge`` subcommand.
    The castle plugin must pass only an opaque authority it obtained from Hermes
    core for this direct user turn; the plugin owns validation of that opaque
    claim and calls this function once.  Knowledge binds the job, starting HEAD,
    generated outputs, origin URL, main branch and current upstream OID again.
    """
    store = _job_store()
    state = store.load(job_id)
    if state.get("phase") not in {"ready_for_publish", "completed"}:
        raise jobs.JobError("job is not ready for publication")
    job_dir = store.job_dir(job_id)
    result = state.get("result")
    if not isinstance(result, dict):
        raise jobs.JobError("ready job has no publication manifest")
    journal = repository._read_journal(REPO_ROOT)
    recovering_exact_journal = bool(
        isinstance(journal, dict)
        and journal.get("publication_id") == job_id
        and journal.get("stage") not in {"rolled_back", "closed"}
    )
    if state.get("phase") == "ready_for_publish" and not recovering_exact_journal:
        verified_result = _finalize_job(job_dir, run_test_suite=False)
        if verified_result != result:
            raise jobs.JobError("READY publication failed deterministic re-finalization")
    binding = publication_binding(job_id=job_id, state=state, result=result)
    authority_binding = _validated_authority_binding(
        job_id=job_id,
        state=state,
        publication=binding,
        authority_binding=authority_binding,
    )
    checkpoint_output = job_dir / "checkpoint.json"
    completed_state: dict | None = None

    def complete_publication(commit: str) -> None:
        nonlocal completed_state
        completed_state = _complete_publication_state(
            store=store, job_id=job_id, ready_result=result, commit=commit,
        )
    remote_url = result.get("remote_url")
    upstream_oid = result.get("upstream_oid")
    if (
        remote_url != EXPECTED_ORIGIN_URL
        or upstream_oid != state.get("starting_remote_oid")
        or result.get("branch") != "main"
    ):
        raise jobs.JobError("ready publication repository binding mismatch")
    candidates_bytes = jobs.read_regular_bytes(job_dir / "final-candidates.json")
    summaries_bytes = jobs.read_regular_bytes(job_dir / "summaries.json")
    merged_bytes = jobs.read_regular_bytes(job_dir / "merged.json")
    checkpoint_bytes = jobs.read_regular_bytes(checkpoint_output)
    artifact_digests = {
        "candidates_sha256": "sha256:" + hashlib.sha256(candidates_bytes).hexdigest(),
        "summaries_sha256": "sha256:" + hashlib.sha256(summaries_bytes).hexdigest(),
        "merged_sha256": "sha256:" + hashlib.sha256(merged_bytes).hexdigest(),
        "checkpoint_output_sha256": "sha256:"
        + hashlib.sha256(checkpoint_bytes).hexdigest(),
    }
    for field, digest in artifact_digests.items():
        if result.get(field) != digest:
            raise jobs.JobError(f"ready-for-publish artifact changed: {field}")
    if state.get("final_candidates_sha256") != artifact_digests["candidates_sha256"]:
        raise jobs.JobError("ready-for-publish candidates state digest mismatch")
    if state.get("summaries_sha256") != artifact_digests["summaries_sha256"]:
        raise jobs.JobError("ready-for-publish summaries state digest mismatch")
    merged = _load_document_bytes(merged_bytes)
    checkpoint = _load_checkpoint_bytes(checkpoint_bytes)
    prepared = repository.prepare_transaction(
        repo_root=REPO_ROOT, merged=merged, checkpoint=checkpoint,
        transaction_dir=job_dir / "publish-transaction",
    )
    prepared_digests = {
        "entries_sha256": "sha256:" + hashlib.sha256(prepared.data_bytes).hexdigest(),
        "checkpoint_sha256": "sha256:"
        + hashlib.sha256(prepared.checkpoint_bytes).hexdigest(),
    }
    if prepared_digests != {
        "entries_sha256": result.get("merged_sha256"),
        "checkpoint_sha256": result.get("checkpoint_output_sha256"),
    }:
        raise jobs.JobError("READY bytes are not the exact canonical commit outputs")
    commit = repository.commit_transaction(
        prepared,
        message=f"knowledge: publish durable job\n\nKnowledge-Job: {job_id}",
        job_id=job_id,
        starting_head=str(state["starting_head"]),
        expected_output_digests=prepared_digests,
        expected_remote_url=str(remote_url),
        expected_upstream_oid=str(upstream_oid),
        capability=capability,
        authority_binding=authority_binding,
        publication_binding=binding,
        on_push_verified=complete_publication,
    )
    if completed_state is None:
        raise jobs.JobError("publication commit was not durably completed")
    return completed_state


def _job_summary_callback(job_id: str):
    state = _job_store().load(job_id)
    summary_config_path = REPO_ROOT / "config/summary.yml"
    expected_summary_config = state.get("summary_config_sha256")
    runtime: dict[str, object] = {}

    def summarize_one(candidate: models.Candidate) -> summarizer.SummaryOutput:
        if jobs.file_sha256(summary_config_path) != expected_summary_config:
            raise jobs.JobError("summary config changed while the job was running")
        if not runtime:
            summary_cfg = config.load_summary(summary_config_path)
            if jobs.file_sha256(summary_config_path) != expected_summary_config:
                raise jobs.JobError("summary config changed while it was loaded")
            runtime["config"] = summary_cfg
            runtime["client"] = summarizer.RestrictedLlmClient(
                summary_cfg.base_url,
                summary_cfg.model,
                timeout=summary_cfg.request_timeout_seconds,
                seed=summary_cfg.seed,
            )
        summary_cfg = runtime["config"]
        client = runtime["client"]
        outputs = summarizer.summarize_candidates(
            (candidate,), summary_cfg, client=client
        )
        if jobs.file_sha256(summary_config_path) != expected_summary_config:
            raise jobs.JobError("summary config changed during model inference")
        if len(outputs) != 1:
            raise jobs.JobError("summary runner did not return exactly one result")
        return outputs[0]

    return summarize_one


def job_run_command(*, job_id: str) -> int:

    try:
        state = jobs.run_job(
            store=_job_store(),
            job_id=job_id,
            summarize_one=_job_summary_callback(job_id),
            finalize=_finalize_job,
        )
        print(json.dumps({"ok": True, "job": _public_job_state(state)}, ensure_ascii=False))
        return 0 if state.get("phase") in {
            "completed",
            "cancelled",
            "ready_for_publish",
        } else 1
    except jobs.JobAlreadyRunning:
        print(json.dumps({"ok": True, "status": "already_running", "job_id": job_id}))
        return 0
    except Exception as error:
        print(json.dumps({
            "ok": False,
            "job_id": job_id,
            "error": type(error).__name__,
            "message": jobs.safe_error_message(error),
        }, ensure_ascii=False))
        return 1


def job_sweep_command() -> int:
    store = _job_store()
    resumed = []
    for job_id in jobs.sweep_jobs(store=store):
        state = store.load(job_id)
        origin_session_id = state.get("origin_session_id")
        if not isinstance(origin_session_id, str) or not origin_session_id:
            continue
        resumed.append({
            "job_id": job_id,
            "origin_session_id": origin_session_id,
        })
    print(json.dumps({"ok": True, "jobs": resumed}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge")
    sub = parser.add_subparsers(dest="cmd")

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--config", type=Path, default=REPO_ROOT / "config/sources.yml")
    p_collect.add_argument("--checkpoint", type=Path, default=REPO_ROOT / "data/checkpoint.json")
    p_collect.add_argument("--output", type=Path, default=REPO_ROOT / ".work/candidates.json")
    p_collect.add_argument("--run-started-at", default=_utcnow())
    p_collect.add_argument("--summary-config", type=Path, default=REPO_ROOT / "config/summary.yml")

    p_sum = sub.add_parser("summarize")
    p_sum.add_argument("--candidates", type=Path, default=REPO_ROOT / ".work/candidates.json")
    p_sum.add_argument("--output", type=Path, default=REPO_ROOT / ".work/summaries.json")
    p_sum.add_argument("--config", type=Path, default=REPO_ROOT / "config/summary.yml")

    p_merge = sub.add_parser("merge")
    p_merge.add_argument("--entries", type=Path, default=REPO_ROOT / "data/entries.json")
    p_merge.add_argument("--checkpoint", type=Path, default=REPO_ROOT / "data/checkpoint.json")
    p_merge.add_argument("--candidates", type=Path, default=REPO_ROOT / ".work/candidates.json")
    p_merge.add_argument("--summaries", type=Path, default=REPO_ROOT / ".work/summaries.json")
    p_merge.add_argument("--config", type=Path, default=REPO_ROOT / "config/sources.yml")
    p_merge.add_argument("--output-merged", type=Path, default=None)

    p_build = sub.add_parser("build")
    p_build.add_argument("--entries", type=Path, default=REPO_ROOT / "data/entries.json")
    p_build.add_argument("--output", type=Path, default=REPO_ROOT / "dist")

    p_check = sub.add_parser("check-build")
    p_check.add_argument("--entries", type=Path, default=REPO_ROOT / "data/entries.json")
    p_check.add_argument("--dist", type=Path, default=REPO_ROOT / "dist")

    p_atom = sub.add_parser("validate-atom")
    p_atom.add_argument("feed", type=Path)

    p_data = sub.add_parser("validate-data")
    p_data.add_argument("--entries", type=Path, default=REPO_ROOT / "data/entries.json")

    sub.add_parser("check-inference-idle")

    p_job_start = sub.add_parser("job-start")
    p_job_start.add_argument("--idempotency-key", required=True)
    p_job_start.add_argument("--origin-session-id", default="")
    p_job_start.add_argument("--origin-turn-id", default="")
    p_job_start.add_argument(
        "--origin-authority-kind",
        choices=("direct_user", "scheduled"),
        required=True,
    )

    p_job_status = sub.add_parser("job-status")
    p_job_status.add_argument("--job-id", default=None)
    p_job_status.add_argument("--origin-session-id", required=True)

    p_job_cancel = sub.add_parser("job-cancel")
    p_job_cancel.add_argument("--job-id", required=True)
    p_job_cancel.add_argument("--origin-session-id", required=True)

    p_job_run = sub.add_parser("job-run")
    p_job_run.add_argument("--job-id", required=True)

    sub.add_parser("job-sweep")

    args = parser.parse_args(argv)
    cmd = args.cmd
    if cmd == "collect":
        return collect_command(config_path=args.config, checkpoint_path=args.checkpoint,
                               output_path=args.output, run_started_at=args.run_started_at,
                               summary_config_path=args.summary_config)
    if cmd == "summarize":
        return summarize_command(candidates_path=args.candidates, output_path=args.output,
                                 config_path=args.config)
    if cmd == "merge":
        return merge_command(entries_path=args.entries, checkpoint_path=args.checkpoint,
                             candidates_path=args.candidates, summaries_path=args.summaries,
                             config_path=args.config,
                             merged_output=args.output_merged)
    if cmd == "build":
        return build_command(entries_path=args.entries, output_dir=args.output)
    if cmd == "check-build":
        return check_command(entries_path=args.entries, dist_dir=args.dist)
    if cmd == "validate-atom":
        return validate_atom_command(args.feed)
    if cmd == "validate-data":
        return validate_data_command(args.entries)
    if cmd == "check-inference-idle":
        return check_inference_idle_command()
    if cmd == "job-start":
        return job_start_command(
            idempotency_key=args.idempotency_key,
            origin_session_id=args.origin_session_id,
            origin_turn_id=args.origin_turn_id,
            origin_authority_kind=args.origin_authority_kind,
        )
    if cmd == "job-status":
        return job_status_command(
            job_id=args.job_id, origin_session_id=args.origin_session_id,
        )
    if cmd == "job-cancel":
        return job_cancel_command(
            job_id=args.job_id, origin_session_id=args.origin_session_id,
        )
    if cmd == "job-run":
        return job_run_command(job_id=args.job_id)
    if cmd == "job-sweep":
        return job_sweep_command()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
