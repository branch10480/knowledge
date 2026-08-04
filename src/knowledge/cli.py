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

from . import builder, collector, config, identity, models, repository, summarizer, validate

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
                allowed_hosts: tuple[str, ...]) -> models.Entry:
    from .validate import validate_url, factual_source_gate
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
        published_at=candidate.published_at or collected_at,
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
                  merged_output: Path | None = None) -> int:
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
        additions.append(_entry_from(c, s, _utcnow(), host_map.get(c.source_id, ())))
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
        sha = repository.commit_transaction(prep)
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
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
