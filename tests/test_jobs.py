from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from knowledge import jobs, models, summarizer
from knowledge import cli


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    (repo / "config").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "config/sources.yml").write_text("sources: []\n", encoding="utf-8")
    (repo / "config/summary.yml").write_text("provider: test\n", encoding="utf-8")
    (repo / "data/entries.json").write_text(
        '{"schema_version":2,"entries":[]}\n', encoding="utf-8",
    )
    (repo / "data/checkpoint.json").write_text(
        '{"schema_version":1,"last_success_at":"1970-01-01T00:00:00Z","sources":{}}\n',
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".work/\n", encoding="utf-8")
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Knowledge Test")
    _git(repo, "config", "user.email", "knowledge@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
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


def test_job_repo_ready_rejects_remote_main_ahead(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    origin = Path(_git(repo, "remote", "get-url", "origin"))
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(origin), str(other))
    _git(other, "config", "user.name", "Other Writer")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "remote.txt").write_text("remote ahead\n", encoding="utf-8")
    _git(other, "add", "remote.txt")
    _git(other, "commit", "-m", "advance remote")
    _git(other, "push", "origin", "main")

    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    with pytest.raises(jobs.JobError, match="local HEAD to match remote main"):
        cli._assert_job_repo_ready(expected_remote_url=str(origin))


def test_job_start_persists_one_attested_local_remote_snapshot(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    origin = _git(repo, "remote", "get-url", "origin")
    attested = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(cli, "EXPECTED_ORIGIN_URL", origin)
    monkeypatch.setattr(cli, "_assert_job_repo_ready", lambda: (attested, attested))
    monkeypatch.setattr(cli, "collect_command", lambda **kwargs: (
        Path(kwargs["output_path"]).write_text(
            json.dumps({
                "candidates": [], "selected_candidate_ids": [],
                "deferred_candidate_ids": [],
                "proposed_checkpoint": {"schema_version": 1, "sources": {}},
                "stats": [],
            }), encoding="utf-8",
        ) and 0
    ))

    assert cli.job_start_command(
        idempotency_key="session-1:attested-snapshot",
        origin_session_id="session-1",
        origin_turn_id="turn-1",
        origin_authority_kind="direct_user",
    ) == 0
    state = cli._job_store().find_by_idempotency_key("session-1:attested-snapshot")
    assert state is not None
    assert state["starting_head"] == attested
    assert state["starting_remote_oid"] == attested


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
    receipt = json.loads(
        (store.job_dir(state["job_id"]) / "collect-receipt.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["status"] == "succeeded"
    assert receipt["candidates_sha256"] == state["candidates_sha256"]


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


def test_idempotency_key_cannot_cross_origin_authority_or_turn(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    calls: list[str] = []
    base = {
        "repo_root": repo,
        "store": store,
        "idempotency_key": "session-1:turn-1",
        "collect": _collector(("candidate-1",), calls),
        "origin_session_id": "session-1",
        "origin_turn_id": "turn-1",
        "origin_authority_kind": "direct_user",
        "run_started_at": "2026-08-12T01:02:03Z",
    }
    jobs.prepare_job(**base)

    for changed in (
        {"origin_session_id": "session-2"},
        {"origin_turn_id": "turn-2"},
        {"origin_authority_kind": "scheduled"},
    ):
        with pytest.raises(jobs.JobError, match="origin binding mismatch"):
            jobs.prepare_job(**(base | changed))

    assert calls == ["2026-08-12T01:02:03Z"]
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
    assert completed["runner_pid"] is None
    assert completed["runner_lease"] is None
    assert completed["retry_at_epoch"] == 0


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


def test_retryable_inference_failure_waits_and_retries_in_same_runner(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-retry",
        collect=_collector(("candidate-1",), []), run_started_at="2026-08-12T01:02:03Z",
    )

    calls = 0
    sleeps: list[float] = []

    def summarize(candidate: models.Candidate):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise jobs.RetryableInferenceError("proxy unavailable")
        return _summary(candidate)

    returned = jobs.run_job(
        store=store, job_id=state["job_id"],
        summarize_one=summarize,
        finalize=lambda _: {"commit": "abc"},
        sleep=sleeps.append,
    )

    assert returned["phase"] == "completed"
    assert returned["runner_lease"] is None
    assert returned["inference_retry_count"] == 1
    assert calls == 2
    assert sleeps == [5]


def test_retryable_inference_failure_stops_at_attempt_cap(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-cap",
        collect=_collector(("candidate-1",), []), run_started_at="2026-08-12T01:02:03Z",
    )
    calls = 0

    def unavailable(_: models.Candidate):
        nonlocal calls
        calls += 1
        raise jobs.RetryableInferenceError("proxy unavailable")

    with pytest.raises(jobs.JobError, match="retry limit"):
        jobs.run_job(
            store=store, job_id=state["job_id"], summarize_one=unavailable,
            finalize=lambda _: pytest.fail("finalizer must not run"),
            sleep=lambda _: None, max_inference_retries=2,
        )
    assert calls == 3
    assert store.load(state["job_id"])["phase"] == "failed"


def test_retryable_inference_failure_stops_at_elapsed_cap(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-elapsed",
        collect=_collector(("candidate-1",), []), run_started_at="2026-08-12T01:02:03Z",
    )
    with pytest.raises(jobs.JobError, match="retry limit"):
        jobs.run_job(
            store=store, job_id=state["job_id"],
            summarize_one=lambda _: (_ for _ in ()).throw(
                jobs.RetryableInferenceError("proxy unavailable")
            ),
            finalize=lambda _: pytest.fail("finalizer must not run"),
            sleep=lambda _: None, now=lambda: 100.0,
            max_retry_elapsed_seconds=4,
        )
    assert store.load(state["job_id"])["phase"] == "failed"


def test_retry_classification_rejects_untyped_error_text():
    assert jobs.is_retryable_inference_failure(
        jobs.RetryableInferenceError("proxy unavailable")
    )
    assert not jobs.is_retryable_inference_failure(
        RuntimeError("503 timeout connection reset")
    )


def test_sweep_releases_expired_runner_lease(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-sweep",
        collect=_collector((), []), run_started_at="2026-08-12T01:02:03Z",
    )
    store.update(state["job_id"], phase="summarizing", runner_lease={"expires_at": 1})
    assert jobs.sweep_jobs(store=store, now=2) == [state["job_id"]]
    assert store.load(state["job_id"])["phase"] == "waiting_for_inference"

def test_sweep_skips_expired_lease_while_runner_lock_is_live(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-live-lock",
        collect=_collector(("candidate-1",), []), run_started_at="2026-08-12T01:02:03Z",
    )
    store.update(state["job_id"], phase="summarizing", runner_lease={"expires_at": 1})
    with store.runner_lock(state["job_id"]):
        assert jobs.sweep_jobs(store=store, now=2) == []
        assert store.load(state["job_id"])["phase"] == "summarizing"


def test_sweep_recovers_durable_collect_output_without_recollecting(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-collect-sweep",
        collect=_collector(("candidate-1",), []), run_started_at="2026-08-12T01:02:03Z",
    )
    store.update(
        state["job_id"], phase="collecting", candidates_sha256=None,
        candidate_ids=[], selected_candidate_ids=[], runner_lease={"expires_at": 1},
    )
    assert jobs.sweep_jobs(store=store, now=2) == [state["job_id"]]
    recovered = store.load(state["job_id"])
    assert recovered["phase"] == "waiting_for_inference"
    assert recovered["selected_candidate_ids"] == ["candidate-1"]
    assert recovered["candidates_sha256"].startswith("sha256:")


def test_sweep_does_not_promote_collect_output_without_success_receipt(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-partial-collect",
        collect=_collector(("candidate-1",), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    (store.job_dir(state["job_id"]) / "collect-receipt.json").unlink()
    store.update(
        state["job_id"],
        phase="collecting",
        candidates_sha256=None,
        candidate_ids=[],
        selected_candidate_ids=[],
    )

    assert jobs.sweep_jobs(store=store, now=2) == []
    assert store.load(state["job_id"])["phase"] == "collecting"


def test_sweep_recovers_orphan_even_when_retry_or_lease_time_is_in_future(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    waiting, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-future-retry",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    summarizing, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-future-lease",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:04Z",
    )
    store.update(waiting["job_id"], retry_at_epoch=10_000)
    store.update(
        summarizing["job_id"],
        phase="summarizing",
        runner_lease={"expires_at": 10_000},
    )

    assert jobs.sweep_jobs(store=store, now=2) == [
        waiting["job_id"],
        summarizing["job_id"],
    ]
    assert store.load(summarizing["job_id"])["phase"] == "waiting_for_inference"


def test_sweep_isolates_one_corrupt_collect_receipt(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    corrupt, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-corrupt",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    healthy, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-healthy",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:04Z",
    )
    store.update(corrupt["job_id"], phase="collecting")
    (store.job_dir(corrupt["job_id"]) / "collect-receipt.json").write_text(
        "not json\n", encoding="utf-8"
    )

    assert jobs.sweep_jobs(store=store, now=2) == [healthy["job_id"]]
    assert store.load(corrupt["job_id"])["phase"] == "collecting"


def test_sweep_skips_corrupt_state_and_recovers_later_job(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    corrupt, _ = jobs.prepare_job(
        repo_root=repo, store=store,
        idempotency_key="session-1:turn-corrupt-state",
        collect=_collector((), []), run_started_at="2026-08-12T01:02:03Z",
    )
    healthy, _ = jobs.prepare_job(
        repo_root=repo, store=store,
        idempotency_key="session-1:turn-after-corrupt-state",
        collect=_collector((), []), run_started_at="2026-08-12T01:02:04Z",
    )
    (store.job_dir(corrupt["job_id"]) / "state.json").write_text(
        "not json\n", encoding="utf-8",
    )

    assert jobs.sweep_jobs(store=store, now=2) == [healthy["job_id"]]
    assert store.load(healthy["job_id"])["phase"] == "waiting_for_inference"


def test_runner_lease_heartbeat_refreshes_during_blocking_inference(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-heartbeat",
        collect=_collector(("candidate-1",), []), run_started_at="2026-08-12T01:02:03Z",
    )
    observed: list[float] = []

    def blocking(candidate: models.Candidate):
        observed.append(store.load(state["job_id"])["runner_lease"]["expires_at"])
        time.sleep(0.05)
        observed.append(store.load(state["job_id"])["runner_lease"]["expires_at"])
        return _summary(candidate)

    jobs.run_job(
        store=store, job_id=state["job_id"], summarize_one=blocking,
        finalize=lambda _: {"commit": "abc"}, heartbeat_interval=0.01,
    )
    assert observed[1] > observed[0]


def test_completion_event_id_uses_canonical_json():
    left = jobs._completion_event_id({
        "job_id": "job_20260812T010203Z_0123456789ab",
        "phase": "completed",
        "result": {"commit": "abc", "added": 1},
    })
    right = jobs._completion_event_id({
        "result": {"added": 1, "commit": "abc"},
        "phase": "completed",
        "job_id": "job_20260812T010203Z_0123456789ab",
    })
    assert left == right


def test_model_cli_no_longer_exposes_merge_commit():
    with pytest.raises(SystemExit) as exited:
        cli.main(["merge", "--commit"])
    assert exited.value.code == 2


def test_build_reads_templates_and_static_from_runtime_root(
    tmp_path: Path, monkeypatch,
):
    repo = _repo(tmp_path)
    runtime = tmp_path / "runtime"
    (runtime / "templates").mkdir(parents=True)
    (runtime / "static").mkdir()
    observed = {}

    def fake_build_site(document, **kwargs):
        observed.update(kwargs)
        return type("Manifest", (), {"entry_count": len(document.entries)})()

    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(cli, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(cli.builder, "build_site", fake_build_site)
    assert cli.build_command(
        entries_path=repo / "data/entries.json",
        output_dir=repo / ".work/dist",
    ) == 0
    assert observed["templates_dir"] == runtime / "templates"
    assert observed["static_dir"] == runtime / "static"
    assert observed["repo_root"] == repo


def test_job_sweep_returns_only_recovery_identity(
    tmp_path: Path, monkeypatch, capsys,
):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-sweep-cli",
        collect=_collector((), []),
        origin_session_id="agent:main:cli:test",
        run_started_at="2026-08-12T01:02:03Z",
    )
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    assert cli.job_sweep_command() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "jobs": [{
            "job_id": state["job_id"],
            "origin_session_id": "agent:main:cli:test",
        }],
    }


def test_root_owned_finalizer_is_deterministic_for_exact_ready_bytes(
    tmp_path: Path, monkeypatch,
):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store,
        idempotency_key="session-1:turn-real-finalizer",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:03Z",
    )
    monkeypatch.setattr(cli, "REPO_ROOT", repo)

    ready = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=lambda _: pytest.fail("empty job must not call inference"),
        finalize=lambda job_dir: cli._finalize_job(
            job_dir, run_test_suite=False,
        ),
    )

    assert ready["phase"] == "ready_for_publish"
    repeated = cli._finalize_job(
        store.job_dir(state["job_id"]), run_test_suite=False,
    )
    assert repeated == ready["result"]


def test_publish_ready_job_reconciles_committed_journal_before_clean_check(
    tmp_path: Path, monkeypatch,
):
    repo = _repo(tmp_path)
    origin = tmp_path / "origin.git"
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store, idempotency_key="session-1:turn-publish-reconcile",
        collect=_collector((), []), run_started_at="2026-08-12T01:02:03Z",
    )

    def ready_result(job_dir: Path):
        jobs.write_json_file(
            job_dir / "merged.json", {"schema_version": 2, "entries": []},
        )
        jobs.write_json_file(
            job_dir / "checkpoint.json",
            json.loads((repo / "data/checkpoint.json").read_text(encoding="utf-8")),
        )
        return {
            "ready_for_publish": True,
            "job_id": state["job_id"],
            "starting_head": state["starting_head"],
            "merged_sha256": jobs.file_sha256(job_dir / "merged.json"),
            "checkpoint_output_sha256": jobs.file_sha256(job_dir / "checkpoint.json"),
            "candidates_sha256": jobs.file_sha256(job_dir / "final-candidates.json"),
            "summaries_sha256": jobs.file_sha256(job_dir / "summaries.json"),
            "remote_url": str(origin),
            "branch": "main",
            "upstream_oid": state["starting_remote_oid"],
        }

    ready = jobs.run_job(
        store=store, job_id=state["job_id"], summarize_one=_summary,
        finalize=ready_result,
    )
    assert ready["phase"] == "ready_for_publish"
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(cli, "EXPECTED_ORIGIN_URL", str(origin))
    monkeypatch.setattr(
        cli, "_finalize_job",
        lambda job_dir, run_test_suite=False: dict(ready["result"]),
    )
    monkeypatch.setattr(
        cli.repository,
        "_consume_bound_capability",
        lambda capability, binding: None,
    )
    original_push = cli.repository._push_exact

    def fail_push(*args, **kwargs):
        raise cli.repository.RepositoryError("simulated push failure")

    monkeypatch.setattr(cli.repository, "_push_exact", fail_push)
    with pytest.raises(cli.repository.RepositoryError, match="push failure"):
        cli.publish_ready_job(
            job_id=state["job_id"], capability="capability-1",
            authority_binding=cli.publication_binding(
                job_id=state["job_id"], state=ready, result=ready["result"],
            ),
        )
    assert _git(repo, "rev-parse", "HEAD") != state["starting_head"]
    assert store.load(state["job_id"])["phase"] == "ready_for_publish"

    monkeypatch.setattr(cli.repository, "_push_exact", original_push)
    original_update = jobs.JobStore.update

    def crash_completion(self, target_job_id, **changes):
        if changes.get("phase") == "completed":
            raise KeyboardInterrupt("killed after verified push")
        return original_update(self, target_job_id, **changes)

    monkeypatch.setattr(jobs.JobStore, "update", crash_completion)
    with pytest.raises(KeyboardInterrupt, match="after verified push"):
        cli.publish_ready_job(
            job_id=state["job_id"],
            capability="capability-2",
            authority_binding=cli.publication_binding(
                job_id=state["job_id"], state=ready, result=ready["result"],
            ),
        )
    assert store.load(state["job_id"])["phase"] == "ready_for_publish"
    assert cli.repository._read_journal(repo)["stage"] == "push_verified"

    monkeypatch.setattr(jobs.JobStore, "update", original_update)
    completed = cli.reconcile_push_verified_publication()
    assert completed is not None
    assert completed["phase"] == "completed"
    assert completed["result"]["commit"] == _git(repo, "rev-parse", "HEAD")
    assert cli.repository._read_journal(repo)["stage"] == "closed"


def test_push_verified_recovery_closes_completed_job_without_rewriting_state(
    tmp_path: Path, monkeypatch,
):
    repo = _repo(tmp_path)
    origin = tmp_path / "origin.git"
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store,
        idempotency_key="session-1:turn-completed-before-close",
        collect=_collector((), []), run_started_at="2026-08-12T01:02:04Z",
    )

    def ready_result(job_dir: Path):
        jobs.write_json_file(
            job_dir / "merged.json", {"schema_version": 2, "entries": []},
        )
        jobs.write_json_file(
            job_dir / "checkpoint.json",
            json.loads((repo / "data/checkpoint.json").read_text(encoding="utf-8")),
        )
        return {
            "ready_for_publish": True,
            "job_id": state["job_id"],
            "starting_head": state["starting_head"],
            "merged_sha256": jobs.file_sha256(job_dir / "merged.json"),
            "checkpoint_output_sha256": jobs.file_sha256(job_dir / "checkpoint.json"),
            "candidates_sha256": jobs.file_sha256(job_dir / "final-candidates.json"),
            "summaries_sha256": jobs.file_sha256(job_dir / "summaries.json"),
            "remote_url": str(origin),
            "branch": "main",
            "upstream_oid": state["starting_remote_oid"],
        }

    ready = jobs.run_job(
        store=store, job_id=state["job_id"], summarize_one=_summary,
        finalize=ready_result,
    )
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(cli, "EXPECTED_ORIGIN_URL", str(origin))
    monkeypatch.setattr(
        cli, "_finalize_job",
        lambda job_dir, run_test_suite=False: dict(ready["result"]),
    )
    monkeypatch.setattr(
        cli.repository, "_consume_bound_capability",
        lambda capability, binding: None,
    )
    original_write = cli.repository._write_journal

    def crash_before_close(repo_root, record):
        if record.get("stage") == "closed":
            raise KeyboardInterrupt("killed before journal close")
        original_write(repo_root, record)

    monkeypatch.setattr(cli.repository, "_write_journal", crash_before_close)
    with pytest.raises(KeyboardInterrupt, match="journal close"):
        cli.publish_ready_job(
            job_id=state["job_id"], capability="capability",
            authority_binding=cli.publication_binding(
                job_id=state["job_id"], state=ready, result=ready["result"],
            ),
        )
    completed_before = store.load(state["job_id"])
    assert completed_before["phase"] == "completed"
    assert cli.repository._read_journal(repo)["stage"] == "push_verified"

    monkeypatch.setattr(cli.repository, "_write_journal", original_write)
    original_update = jobs.JobStore.update

    def reject_duplicate_update(self, target_job_id, **changes):
        if changes.get("phase") == "completed":
            pytest.fail("completed state must not be rewritten during recovery")
        return original_update(self, target_job_id, **changes)

    monkeypatch.setattr(jobs.JobStore, "update", reject_duplicate_update)
    recovered = cli.reconcile_push_verified_publication()
    assert recovered == completed_before
    assert cli.repository._read_journal(repo)["stage"] == "closed"
    assert cli.reconcile_push_verified_publication() is None


def test_push_verified_recovery_rejects_remote_rewrite(
    tmp_path: Path, monkeypatch,
):
    repo = _repo(tmp_path)
    origin = tmp_path / "origin.git"
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo, store=store,
        idempotency_key="session-1:turn-remote-rewrite",
        collect=_collector((), []), run_started_at="2026-08-12T01:02:05Z",
    )

    def ready_result(job_dir: Path):
        jobs.write_json_file(job_dir / "merged.json", {"schema_version": 2, "entries": []})
        jobs.write_json_file(
            job_dir / "checkpoint.json",
            json.loads((repo / "data/checkpoint.json").read_text(encoding="utf-8")),
        )
        return {
            "ready_for_publish": True,
            "job_id": state["job_id"],
            "starting_head": state["starting_head"],
            "merged_sha256": jobs.file_sha256(job_dir / "merged.json"),
            "checkpoint_output_sha256": jobs.file_sha256(job_dir / "checkpoint.json"),
            "candidates_sha256": jobs.file_sha256(job_dir / "final-candidates.json"),
            "summaries_sha256": jobs.file_sha256(job_dir / "summaries.json"),
            "remote_url": str(origin),
            "branch": "main",
            "upstream_oid": state["starting_remote_oid"],
        }

    ready = jobs.run_job(
        store=store, job_id=state["job_id"], summarize_one=_summary,
        finalize=ready_result,
    )
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(cli, "EXPECTED_ORIGIN_URL", str(origin))
    monkeypatch.setattr(
        cli, "_finalize_job",
        lambda job_dir, run_test_suite=False: dict(ready["result"]),
    )
    monkeypatch.setattr(cli.repository, "_consume_bound_capability", lambda *_: None)
    original_update = jobs.JobStore.update

    def crash_completion(self, target_job_id, **changes):
        if changes.get("phase") == "completed":
            raise KeyboardInterrupt("killed after verified push")
        return original_update(self, target_job_id, **changes)

    monkeypatch.setattr(jobs.JobStore, "update", crash_completion)
    with pytest.raises(KeyboardInterrupt, match="verified push"):
        cli.publish_ready_job(
            job_id=state["job_id"], capability="capability",
            authority_binding=cli.publication_binding(
                job_id=state["job_id"], state=ready, result=ready["result"],
            ),
        )
    monkeypatch.setattr(jobs.JobStore, "update", original_update)

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(origin), str(other))
    _git(other, "config", "user.name", "Other Writer")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "remote.txt").write_text("rewritten\n", encoding="utf-8")
    _git(other, "add", "remote.txt")
    _git(other, "commit", "-m", "rewrite remote")
    _git(other, "push", "--force", "origin", "main")

    with pytest.raises(jobs.JobError, match="remote OID changed"):
        cli.reconcile_push_verified_publication()
    assert store.load(state["job_id"])["phase"] == "ready_for_publish"
    assert cli.repository._read_journal(repo)["stage"] == "push_verified"


def test_publication_binding_changes_with_exact_ready_manifest(tmp_path: Path):
    repo = _repo(tmp_path)
    store = jobs.JobStore(repo / ".work/jobs")
    state, _ = jobs.prepare_job(
        repo_root=repo,
        store=store,
        idempotency_key="session-1:turn-authority",
        collect=_collector((), []),
        run_started_at="2026-08-12T01:02:03Z",
    )

    def ready_result(job_dir: Path):
        jobs.write_json_file(
            job_dir / "merged.json", {"schema_version": 2, "entries": []},
        )
        jobs.write_json_file(
            job_dir / "checkpoint.json",
            json.loads((repo / "data/checkpoint.json").read_text(encoding="utf-8")),
        )
        return {
            "ready_for_publish": True,
            "job_id": state["job_id"],
            "starting_head": state["starting_head"],
            "merged_sha256": jobs.file_sha256(job_dir / "merged.json"),
            "checkpoint_output_sha256": jobs.file_sha256(job_dir / "checkpoint.json"),
            "candidates_sha256": jobs.file_sha256(job_dir / "final-candidates.json"),
            "summaries_sha256": jobs.file_sha256(job_dir / "summaries.json"),
            "remote_url": str(repo.parent / "origin.git"),
            "branch": "main",
            "upstream_oid": state["starting_remote_oid"],
        }

    ready = jobs.run_job(
        store=store,
        job_id=state["job_id"],
        summarize_one=_summary,
        finalize=ready_result,
    )
    original = cli.publication_binding(
        job_id=ready["job_id"], state=ready, result=ready["result"],
    )
    changed_result = {**ready["result"], "merged_sha256": "sha256:" + "0" * 64}
    changed = cli.publication_binding(
        job_id=ready["job_id"], state=ready, result=changed_result,
    )
    assert changed != original


def test_authority_binding_accepts_only_exact_direct_or_scheduled_schema():
    job_id = "job_20260812T010203Z_0123456789ab"
    state = {"starting_head": "1" * 40}
    result = {
        "merged_sha256": "sha256:" + "2" * 64,
        "checkpoint_output_sha256": "sha256:" + "3" * 64,
        "candidates_sha256": "sha256:" + "4" * 64,
        "summaries_sha256": "sha256:" + "5" * 64,
    }
    publication = cli.publication_binding(job_id=job_id, state=state, result=result)
    assert cli._validated_authority_binding(
        job_id=job_id, state=state, publication=publication,
        authority_binding=publication,
    ) == publication

    scheduled = json.dumps({
        "schema_version": 1,
        "kind": "knowledge-scheduled-job-grant",
        "job_id": job_id,
        "starting_head": state["starting_head"],
    }, sort_keys=True, separators=(",", ":"))
    assert cli._validated_authority_binding(
        job_id=job_id, state=state, publication=publication,
        authority_binding=scheduled,
    ) == scheduled
    with pytest.raises(jobs.JobError, match="not canonical"):
        cli._validated_authority_binding(
            job_id=job_id, state=state, publication=publication,
            authority_binding=json.dumps(json.loads(scheduled), indent=2),
        )
    forged = scheduled.replace(job_id, "job_20260812T010204Z_0123456789ab")
    with pytest.raises(jobs.JobError, match="grant changed"):
        cli._validated_authority_binding(
            job_id=job_id, state=state, publication=publication,
            authority_binding=forged,
        )


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
        (job_dir / "checkpoint.json").write_text("{}\n", encoding="utf-8")
        return {
            "ready_for_publish": True,
            "job_id": state["job_id"],
            "starting_head": state["starting_head"],
            "merged_sha256": jobs.file_sha256(job_dir / "merged.json"),
            "checkpoint_output_sha256": jobs.file_sha256(job_dir / "checkpoint.json"),
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
                "job_id": state["job_id"],
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
