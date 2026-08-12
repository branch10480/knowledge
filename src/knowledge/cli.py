"""knowledge CLI。

設計 5.1 の command 群。各 command は例外を握りつぶさず非 0 で終了する。
JSON log は stdout、診断は stderr。secret や記事全文を log に出さない。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import builder, collector, config, identity, inference, jobs, models, repository, summarizer, validate

REPO_ROOT = Path(__file__).resolve().parents[2]


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_document(entries_path: Path) -> models.EntriesDocument:
    with open(entries_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
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


def merge_command(*, entries_path: Path, checkpoint_path: Path,
                  candidates_path: Path, summaries_path: Path, config_path: Path, commit: bool,
                  merged_output: Path | None = None, job_id: str | None = None) -> int:
    doc = _load_document(entries_path)
    cp = _load_checkpoint(checkpoint_path)
    sources = config.load_sources(config_path)
    host_map = {s.id: s.allowed_hosts for s in sources}

    cdata = json.loads(candidates_path.read_text(encoding="utf-8"))
    cands = tuple(models.Candidate(**c) for c in cdata["candidates"])
    proposed = models.Checkpoint(
        schema_version=1, last_success_at=cp.last_success_at,
        sources={k: models.SourceCheckpoint(**v) for k, v in cdata["proposed_checkpoint"]["sources"].items()},
    )
    sdata = json.loads(summaries_path.read_text(encoding="utf-8"))
    smap = {s["candidate_id"]: summarizer.SummaryOutput(**s) for s in sdata}

    additions = []
    seen_by_source: dict[str, list] = {}
    for c in cands:
        s = smap.get(c.candidate_id)
        if s is None or s.insufficient_evidence:
            continue
        if identity.is_known(c, doc, cp):
            continue
        e = _entry_from(c, s, _utcnow(), host_map.get(c.source_id, ()))
        if e is None:
            # 公開日不明（published_at 空）は「最近収集」として追加しない
            continue
        additions.append(e)
        # 検証・追加まで完了した candidate を seen に記録（次の run で再処理しない）
        seen_by_source.setdefault(c.source_id, []).append({
            "external_id_hash": "sha256:" + hashlib.sha256(c.external_id.encode("utf-8")).hexdigest(),
            "canonical_url_hash": "sha256:" + hashlib.sha256(c.canonical_url.encode("utf-8")).hexdigest(),
            "first_seen_at": _utcnow(),
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
            schema_version=1, last_success_at=_utcnow(), sources=proposed.sources,
        )
    if commit:
        txn_dir = REPO_ROOT / ".work" / f"txn-{_utcnow().replace(':', '')}"
        prep = repository.prepare_transaction(repo_root=REPO_ROOT, merged=merged,
                                              checkpoint=new_cp, transaction_dir=txn_dir)
        message = "knowledge: 収集結果とcheckpointを更新"
        if job_id is not None:
            message += f"\n\nKnowledge-Job: {job_id}"
        sha = repository.commit_transaction(prep, message=message)
        print(json.dumps({"ok": True, "added": len(additions), "commit": sha}))
    else:
        if merged_output is not None:
            merged_output.parent.mkdir(parents=True, exist_ok=True)
            merged_output.write_text(json.dumps(merged.to_json(), ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"ok": True, "added": len(additions), "dry_run": True}))
    return 0


def build_command(*, entries_path: Path, output_dir: Path) -> int:
    doc = _load_document(entries_path)
    m = builder.build_site(
        doc,
        templates_dir=REPO_ROOT / "templates",
        static_dir=REPO_ROOT / "static",
        output_dir=output_dir,
        built_at=_utcnow(),
        repo_root=REPO_ROOT,
    )
    print(json.dumps({"ok": True, "entry_count": m.entry_count, "dist": str(output_dir)}))
    return 0


def check_command(*, entries_path: Path, dist_dir: Path) -> int:
    doc = _load_document(entries_path)
    pages = list(dist_dir.glob("entry/*.html"))
    if len(pages) != len(doc.entries):
        print(json.dumps({"ok": False, "error": f"entry pages {len(pages)} != {len(doc.entries)}"}))
        return 1
    manifest_path = dist_dir / "manifest.json"
    if manifest_path.exists():
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        on_disk = {
            p.relative_to(dist_dir).as_posix()
            for p in dist_dir.rglob("*")
            if p.is_file()
            and p.name != "manifest.json"
            and ".git" not in p.parts
        }
        in_man = set(man.get("files", {}).keys())
        missing = in_man - on_disk
        extra = on_disk - in_man
        if missing or extra:
            print(json.dumps({"ok": False, "error": {"missing": list(missing), "extra": list(extra)}}))
            return 1
    from .links import check_internal_links
    rep = check_internal_links(dist_dir)
    if not rep.ok:
        print(json.dumps({"ok": False, "error": {"broken": rep.broken}}))
        return 1
    print(json.dumps({"ok": True, "entry_count": len(doc.entries)}))
    return 0


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
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _assert_job_repo_ready() -> None:
    import subprocess

    branch = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        raise jobs.JobError("Knowledge job requires the main branch")
    if not _git_check("diff", "--quiet", "--"):
        raise jobs.JobError("Knowledge job requires a clean tracked worktree")
    if not _git_check("diff", "--cached", "--quiet", "--"):
        raise jobs.JobError("Knowledge job requires an empty staging area")
    status = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise jobs.JobError("Knowledge job requires a fully clean worktree")


def job_start_command(
    *,
    idempotency_key: str,
    origin_session_id: str,
    origin_turn_id: str,
    spawn: bool,
) -> int:
    store = _job_store()
    try:
        existing = store.find_by_idempotency_key(idempotency_key)
        if existing is None:
            _assert_job_repo_ready()

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
        )
        runner_pid = state.get("runner_pid")
        if (
            spawn
            and state.get("phase") == "waiting_for_inference"
            and not state.get("runner_pid")
        ):
            runner_pid = jobs.spawn_job_runner(
                repo_root=REPO_ROOT,
                store=store,
                job_id=state["job_id"],
            )
        print(json.dumps({
            "ok": True,
            "status": "deferred",
            "job_id": state["job_id"],
            "phase": state["phase"],
            "reused": reused,
            "runner_pid": runner_pid,
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
            "selected_candidate_ids",
            "completed_candidate_ids",
            "attempts",
            "cancel_requested",
            "result",
            "error",
            "delivery",
        )
    }


def job_status_command(*, job_id: str | None) -> int:
    store = _job_store()
    try:
        state = store.load(job_id) if job_id else store.latest()
        if state is None:
            print(json.dumps({"ok": True, "job": None}))
            return 0
        print(json.dumps({"ok": True, "job": _public_job_state(state)}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, ensure_ascii=False))
        return 1


def job_cancel_command(*, job_id: str) -> int:
    try:
        state = jobs.cancel_job(store=_job_store(), job_id=job_id)
        print(json.dumps({"ok": True, "job": _public_job_state(state)}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, ensure_ascii=False))
        return 1


def _finalize_job(job_dir: Path) -> dict:
    import subprocess

    store = _job_store()
    state = store.load(job_dir.name)
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    if head != state.get("starting_head"):
        raise jobs.JobError("repository HEAD changed while the job was running")

    _assert_job_repo_ready()
    checkpoint_digest = "sha256:" + hashlib.sha256(
        (REPO_ROOT / "data/checkpoint.json").read_bytes()
    ).hexdigest()
    if checkpoint_digest != state.get("checkpoint_sha256"):
        raise jobs.JobError("checkpoint changed while the job was running")

    candidates_path = job_dir / "final-candidates.json"
    candidates_digest = "sha256:" + hashlib.sha256(candidates_path.read_bytes()).hexdigest()
    if candidates_digest != state.get("final_candidates_sha256"):
        raise jobs.JobError("final candidates changed while the job was running")

    merged_path = job_dir / "merged.json"
    dist_dir = job_dir / "dist"
    merge_status = merge_command(
        entries_path=REPO_ROOT / "data/entries.json",
        checkpoint_path=REPO_ROOT / "data/checkpoint.json",
        candidates_path=candidates_path,
        summaries_path=job_dir / "summaries.json",
        config_path=REPO_ROOT / "config/sources.yml",
        commit=False,
        merged_output=merged_path,
    )
    if merge_status != 0:
        raise jobs.JobError("merge dry-run failed")
    if build_command(entries_path=merged_path, output_dir=dist_dir) != 0:
        raise jobs.JobError("build failed")
    if validate_atom_command(dist_dir / "feed.xml") != 0:
        raise jobs.JobError("Atom validation failed")
    if check_command(entries_path=merged_path, dist_dir=dist_dir) != 0:
        raise jobs.JobError("build checks failed")

    checks = (
        ([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest"], REPO_ROOT),
        (["git", "diff", "--check"], REPO_ROOT),
        ([str(REPO_ROOT / "scripts/scan-secrets.sh"), "--paths", "data", str(dist_dir)], REPO_ROOT),
    )
    for command, cwd in checks:
        completed = subprocess.run(command, cwd=cwd, timeout=900, check=False)
        if completed.returncode != 0:
            raise jobs.JobError(f"finalization check failed: {Path(command[0]).name}")

    if store.load(job_dir.name).get("cancel_requested"):
        raise jobs.JobCancelled("job was cancelled before publication readiness")
    result = {
        "ready_for_publish": True,
        "starting_head": head,
        "merged_sha256": jobs.file_sha256(merged_path),
        "candidates_sha256": jobs.file_sha256(candidates_path),
        "summaries_sha256": str(store.load(job_dir.name)["summaries_sha256"]),
    }
    jobs.write_finalize_receipt(
        job_dir=job_dir,
        job_id=job_dir.name,
        summaries_sha256=str(store.load(job_dir.name)["summaries_sha256"]),
        result=result,
    )
    return result


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
    p_merge.add_argument("--commit", action="store_true")
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
    p_job_start.add_argument("--no-spawn", action="store_true")

    p_job_status = sub.add_parser("job-status")
    p_job_status.add_argument("--job-id", default=None)

    p_job_cancel = sub.add_parser("job-cancel")
    p_job_cancel.add_argument("--job-id", required=True)

    p_job_run = sub.add_parser("job-run")
    p_job_run.add_argument("--job-id", required=True)

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
                             config_path=args.config, commit=args.commit,
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
            spawn=not args.no_spawn,
        )
    if cmd == "job-status":
        return job_status_command(job_id=args.job_id)
    if cmd == "job-cancel":
        return job_cancel_command(job_id=args.job_id)
    if cmd == "job-run":
        return job_run_command(job_id=args.job_id)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
