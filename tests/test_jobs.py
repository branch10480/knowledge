from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from knowledge import jobs, models, summarizer


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "config/sources.yml").write_text("sources: []\n", encoding="utf-8")
    (repo / "config/summary.yml").write_text("provider: test\n", encoding="utf-8")
    (repo / "data/checkpoint.json").write_text(
        '{"schema_version":1,"last_success_at":"1970-01-01T00:00:00Z","sources":{}}\n',
        encoding="utf-8",
    )
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Knowledge Test")
    _git(repo, "config", "user.email", "knowledge@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _candidate(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "source_id": "source",
        "source_kind": "rss",
        "external_id": candidate_id,
        "canonical_url": f"https://example.com/{candidate_id}",
        "title": f"title {candidate_id}",
        "published_at": "2026-08-12T00:00:00Z",
        "updated_at": "2026-08-12T00:00:00Z",
        "retrieved_at": "2026-08-12T00:00:00Z",
        "source_text": f"evidence {candidate_id}",
    }


def _collector(candidate_ids: tuple[str, ...], calls: list[str]):
    def collect(output: Path, run_started_at: str) -> int:
        calls.append(run_started_at)
        output.write_text(
            json.dumps({
                "candidates": [_candidate(item) for item in candidate_ids],
                "selected_candidate_ids": list(candidate_ids),
                "deferred_candidate_ids": [],
                "proposed_checkpoint": {"schema_version": 1, "sources": {}},
                "stats": [],
            }),
            encoding="utf-8",
        )
        return 0

    return collect


def _summary(candidate: models.Candidate) -> summarizer.SummaryOutput:
    return summarizer.SummaryOutput(
        candidate_id=candidate.candidate_id,
        title_ja=f"要約 {candidate.candidate_id}",
        summary_ja="要約本文",
        key_points=("要点",),
        tags=("test",),
        claims=({"text": "claim", "evidence_quotes": [candidate.source_text]},),
        insufficient_evidence=False,
    )


def test_prepare_job_collects_without_an_inference_idle_gate(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    calls: list[str] = []

    state, reused = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), calls),
        run_started_at="2026-08-12T01:02:03Z",
    )

    assert reused is False
    assert calls == ["2026-08-12T01:02:03Z"]
    assert state["phase"] == "waiting_for_inference"
    assert state["selected_candidate_ids"] == ["candidate-1"]
    assert (store.job_dir(state["job_id"]) / "candidates.json").is_file()


def test_same_idempotency_key_reuses_one_job_and_does_not_recollect(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    calls: list[str] = []
    kwargs = {
        "repo_root": repo,
        "store": store,
        "idempotency_key": "session-1:turn-1",
        "collect": _collector(("candidate-1",), calls),
        "run_started_at": "2026-08-12T01:02:03Z",
    }

    first, first_reused = jobs.prepare_job(**kwargs)
    second, second_reused = jobs.prepare_job(**kwargs)

    assert first_reused is False
    assert second_reused is True
    assert second["job_id"] == first["job_id"]
    assert len(calls) == 1
    assert len(list(store.root.glob("job_*/state.json"))) == 1


def test_same_idempotency_key_recovers_collect_after_hard_crash(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    calls: list[str] = []

    def crash_collect(output_path: Path, run_started_at: str) -> int:
        del output_path, run_started_at
        calls.append("crash")
        raise KeyboardInterrupt("simulated process death")

    with pytest.raises(KeyboardInterrupt, match="process death"):
        jobs.prepare_job(
            repo_root=repo,
            store=store,
            idempotency_key="session-1:turn-1",
            collect=crash_collect,
            run_started_at="2026-08-12T01:02:03Z",
        )

    stranded = store.find_by_idempotency_key("session-1:turn-1")
    assert stranded is not None
    assert stranded["phase"] == "collecting"

    recovered, reused = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), calls),
        run_started_at="2026-08-12T01:02:03Z",
    )

    assert reused is True
    assert recovered["job_id"] == stranded["job_id"]
    assert recovered["phase"] == "waiting_for_inference"
    assert calls == ["crash", "2026-08-12T01:02:03Z"]


def test_job_id_rejects_path_traversal(tmp_path: Path):
    store = jobs.JobStore(tmp_path / "jobs")

    with pytest.raises(jobs.JobError, match="invalid job id"):
        store.load("../../data/checkpoint.json")


def test_job_store_rejects_symlinked_root(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-jobs"
    linked_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(jobs.JobError, match="contains a symlink"):
        jobs.JobStore(linked_root)


def test_job_store_rejects_symlinked_state_file(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    state_path = store.state_path(state["job_id"])
    outside = tmp_path / "outside-state.json"
    outside.write_text(state_path.read_text(encoding="utf-8"), encoding="utf-8")
    state_path.unlink()
    state_path.symlink_to(outside)

    with pytest.raises(jobs.JobError, match="could not read durable job file"):
        store.load(state["job_id"])


def test_runner_rejects_symlinked_summary_receipt_directory(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    outside = tmp_path / "outside-summaries"
    outside.mkdir()
    (store.job_dir(state["job_id"]) / "summaries").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(jobs.JobError, match="unsafe durable directory"):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=_summary,
            finalize=lambda _: pytest.fail("finalizer must not run"),
        )

    assert list(outside.iterdir()) == []


def test_runner_resumes_from_receipts_without_repeating_completed_candidate(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1", "candidate-2"), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    summarized: list[str] = []

    def fail_on_second(candidate: models.Candidate) -> summarizer.SummaryOutput:
        summarized.append(candidate.candidate_id)
        if candidate.candidate_id == "candidate-2":
            raise RuntimeError("simulated runner crash")
        return _summary(candidate)

    with pytest.raises(RuntimeError, match="simulated runner crash"):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=fail_on_second,
            finalize=lambda _: {"commit": "never"},
        )

    assert store.load(state["job_id"])["phase"] == "failed"
    summarized.clear()
    finalized: list[Path] = []

    completed = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=lambda candidate: summarized.append(candidate.candidate_id) or _summary(candidate),
        finalize=lambda job_dir: finalized.append(job_dir) or {"commit": "abc123"},
    )

    assert summarized == ["candidate-2"]
    assert len(finalized) == 1
    assert completed["phase"] == "completed"
    assert completed["completed_candidate_ids"] == ["candidate-1", "candidate-2"]
    assert completed["result"] == {"commit": "abc123"}


def test_cancelled_job_does_not_call_model_or_finalizer(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    jobs.cancel_job(store=store, job_id=state["job_id"])

    completed = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=lambda _: pytest.fail("model must not be called"),
        finalize=lambda _: pytest.fail("finalizer must not be called"),
    )

    assert completed["phase"] == "cancelled"
    assert completed["completed_candidate_ids"] == []


def test_model_failure_text_is_not_written_to_job_state(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    private_model_text = "PRIVATE MODEL OUTPUT MUST NOT ENTER STATE"

    with pytest.raises(RuntimeError, match="PRIVATE MODEL OUTPUT"):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=lambda _: (_ for _ in ()).throw(
                RuntimeError(private_model_text)
            ),
            finalize=lambda _: pytest.fail("finalizer must not run"),
        )

    failure = store.load(state["job_id"])["error"]
    assert failure["type"] == "RuntimeError"
    assert private_model_text not in failure["message"]


def test_summary_receipt_filename_never_contains_candidate_path_data():
    name = jobs._receipt_name("../../outside/候補")

    assert name.endswith(".json")
    assert "/" not in name
    assert ".." not in name


def test_tampered_candidate_invalidates_existing_summary_receipt(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )

    with pytest.raises(RuntimeError):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=lambda candidate: _summary(candidate),
            finalize=lambda _: (_ for _ in ()).throw(RuntimeError("stop after receipt")),
        )

    candidates_path = store.job_dir(state["job_id"]) / "candidates.json"
    data = json.loads(candidates_path.read_text(encoding="utf-8"))
    data["candidates"][0]["source_text"] = "tampered"
    candidates_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(jobs.JobError, match="candidate digest mismatch"):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=lambda _: pytest.fail("tampered receipt must not be reused"),
            finalize=lambda _: pytest.fail("finalizer must not run"),
        )


def test_insufficient_evidence_keeps_candidate_deferred(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )

    def insufficient(candidate: models.Candidate) -> summarizer.SummaryOutput:
        value = _summary(candidate)
        return summarizer.SummaryOutput(
            candidate_id=value.candidate_id,
            title_ja=value.title_ja,
            summary_ja=value.summary_ja,
            key_points=value.key_points,
            tags=value.tags,
            claims=value.claims,
            insufficient_evidence=True,
        )

    completed = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=insufficient,
        finalize=lambda _: {"commit": "abc123"},
    )

    data = json.loads(
        (store.job_dir(state["job_id"]) / "final-candidates.json").read_text(
            encoding="utf-8"
        )
    )
    assert completed["phase"] == "completed"
    assert data["deferred_candidate_ids"] == ["candidate-1"]


def test_state_update_supports_compare_and_swap_generation(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    current = store.load(state["job_id"])
    updated = store.update(
        state["job_id"],
        expected_generation=current["generation"],
        cancel_requested=True,
    )

    assert updated["generation"] == current["generation"] + 1
    with pytest.raises(jobs.JobError, match="generation changed"):
        store.update(
            state["job_id"],
            expected_generation=current["generation"],
            phase="completed",
        )


def test_finalize_receipt_prevents_duplicate_side_effect_after_crash(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    finalizer_calls = []

    def finalize_then_crash(job_dir: Path):
        finalizer_calls.append(job_dir)
        current = store.load(state["job_id"])
        jobs.write_finalize_receipt(
            job_dir=job_dir,
            job_id=state["job_id"],
            summaries_sha256=current["summaries_sha256"],
            result={"commit": "abc123"},
        )
        raise RuntimeError("crash after durable commit receipt")

    with pytest.raises(RuntimeError, match="durable commit receipt"):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=_summary,
            finalize=finalize_then_crash,
        )

    completed = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=lambda _: pytest.fail("summary receipt must be reused"),
        finalize=lambda _: pytest.fail("finalizer must not run twice"),
    )

    assert len(finalizer_calls) == 1
    assert completed["phase"] == "completed"
    assert completed["result"] == {"commit": "abc123"}


def test_ready_for_publish_is_terminal_and_does_not_repeat_finalizer(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )

    def ready_result(job_dir: Path):
        (job_dir / "merged.json").write_text("{}\n", encoding="utf-8")
        return {
            "ready_for_publish": True,
            "starting_head": state["starting_head"],
            "merged_sha256": jobs.file_sha256(job_dir / "merged.json"),
            "candidates_sha256": jobs.file_sha256(job_dir / "final-candidates.json"),
            "summaries_sha256": jobs.file_sha256(job_dir / "summaries.json"),
        }

    ready = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=_summary,
        finalize=ready_result,
    )

    assert ready["phase"] == "ready_for_publish"
    repeated = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=lambda _: pytest.fail("summary must not run twice"),
        finalize=lambda _: pytest.fail("finalizer must not run twice"),
    )
    assert repeated == ready


def test_tampered_ready_receipt_is_not_trusted(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-1",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )

    def write_bad_receipt(job_dir: Path):
        current = store.load(state["job_id"])
        (job_dir / "merged.json").write_text("{}\n", encoding="utf-8")
        jobs.write_finalize_receipt(
            job_dir=job_dir,
            job_id=state["job_id"],
            summaries_sha256=current["summaries_sha256"],
            result={
                "ready_for_publish": True,
                "starting_head": state["starting_head"],
                "merged_sha256": "sha256:" + "0" * 64,
                "candidates_sha256": jobs.file_sha256(job_dir / "final-candidates.json"),
                "summaries_sha256": jobs.file_sha256(job_dir / "summaries.json"),
            },
        )
        raise RuntimeError("crash after forged receipt")

    with pytest.raises(RuntimeError, match="forged receipt"):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=_summary,
            finalize=write_bad_receipt,
        )

    with pytest.raises(jobs.JobError, match="QA rerun rejected forged receipt"):
        jobs.run_job(
            store=store,
            job_id=state["job_id"],
            summarize_one=lambda _: pytest.fail("summary receipt must be reused"),
            finalize=lambda _: (_ for _ in ()).throw(
                jobs.JobError("QA rerun rejected forged receipt")
            ),
        )
