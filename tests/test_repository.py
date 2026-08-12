"""repository のテスト：merge の重複排除・ソート、transaction の canonical 書き出し。"""
from __future__ import annotations
import json
import os
import subprocess
import pytest

from knowledge import models, repository


@pytest.fixture(autouse=True)
def _accept_test_publication_capability(monkeypatch):
    monkeypatch.setattr(
        repository,
        "_consume_bound_capability",
        lambda capability, authority_binding: None,
    )


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _reconcile(repo):
    """Exercise the private crash-recovery primitive under its required lock."""

    with repository.finalize_lock(repo):
        return repository._reconcile_finalize_journal_locked(
            repo_root=repo, remote="origin", branch="main",
        )


def _publication_repo(tmp_path):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    origin.mkdir()
    repo.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Knowledge Test")
    _git(repo, "config", "user.email", "knowledge@example.invalid")
    (repo / "data").mkdir()
    (repo / "data/entries.json").write_text(
        '{"schema_version":2,"entries":[]}\n', encoding="utf-8",
    )
    (repo / "data/checkpoint.json").write_text(
        '{"schema_version":1,"last_success_at":"1970-01-01T00:00:00Z","sources":{}}\n',
        encoding="utf-8",
    )
    _git(repo, "add", "data")
    _git(repo, "commit", "-m", "fixture")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    starting_head = _git(repo, "rev-parse", "HEAD")
    prepared = repository.prepare_transaction(
        repo_root=repo,
        merged=models.EntriesDocument(2, (_entry("kn_a", "2026-08-01T00:00:00Z"),)),
        checkpoint=models.Checkpoint(1, "2026-08-02T00:00:00Z", {}),
        transaction_dir=repo / ".work/transaction",
    )
    kwargs = {
        "job_id": "job_20260812T010203Z_0123456789ab",
        "starting_head": starting_head,
        "expected_output_digests": {
            "entries_sha256": repository._sha256(prepared.data_path),
            "checkpoint_sha256": repository._sha256(prepared.checkpoint_path),
        },
        "expected_remote_url": str(origin),
        "expected_upstream_oid": starting_head,
        "capability": "test-capability",
        "authority_binding": '{"kind":"test-publication"}',
        "publication_binding": '{"kind":"test-publication"}',
    }
    return repo, origin, prepared, kwargs


def _entry(iid: str, published: str) -> models.Entry:
    return models.Entry(
        id=iid, source_id="s1", external_id=f"e-{iid}", canonical_url=f"https://example.com/{iid}",
        published_at=published, collected_at="2026-08-03T00:00:00Z", title=f"t {iid}",
        summary="s", tags=("Apple",),
        source_digest="sha256:" + "0" * 64,
        summary_model={"provider": "p", "model": "m", "prompt_version": "v"},
        review={"factual_gate": "passed", "checked_at": "2026-08-03T00:00:00Z"},
    )


def test_merge_dedupes_and_sorts():
    existing = models.EntriesDocument(2, (_entry("kn_a", "2026-08-01T00:00:00Z"),))
    # 既存と重複（同じ id / source+external / canonical）
    dup = _entry("kn_a", "2026-08-02T00:00:00Z")
    new = _entry("kn_b", "2026-08-03T00:00:00Z")
    old = _entry("kn_c", "2026-07-30T00:00:00Z")
    merged = repository.merge_entries(existing, (dup, new, old))
    ids = [e.id for e in merged.entries]
    assert ids == ["kn_b", "kn_a", "kn_c"]  # 降順、dup は除外
    assert len(merged.entries) == 3


def test_prepare_transaction_writes_canonical(tmp_path):
    doc = models.EntriesDocument(2, (_entry("kn_a", "2026-08-01T00:00:00Z"),))
    cp = models.Checkpoint(1, "2026-08-02T00:00:00Z", {})
    prep = repository.prepare_transaction(
        repo_root=tmp_path, merged=doc, checkpoint=cp, transaction_dir=tmp_path / "txn",
    )
    raw = json.loads(prep.data_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert len(raw["entries"]) == 1


def test_commit_failure_restores_data_and_index(tmp_path, monkeypatch):
    repo, origin, prep, kwargs = _publication_repo(tmp_path)
    del origin
    data_dir = repo / "data"
    old_entries = (data_dir / "entries.json").read_text(encoding="utf-8")
    old_checkpoint = (data_dir / "checkpoint.json").read_text(encoding="utf-8")

    def fail_commit(**_kwargs):
        raise repository.RepositoryError("simulated commit failure")

    monkeypatch.setattr(repository, "_create_exact_commit", fail_commit)

    with pytest.raises(repository.RepositoryError, match="rolled back"):
        repository.commit_transaction(prep, **kwargs)

    assert (data_dir / "entries.json").read_text(encoding="utf-8") == old_entries
    assert (data_dir / "checkpoint.json").read_text(encoding="utf-8") == old_checkpoint
    assert _git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize(
    "crash_stage", ["canonical_replacing", "canonical_replaced", "commit_started"],
)
def test_precommit_crash_stages_roll_back_from_durable_backups(
    tmp_path, monkeypatch, crash_stage,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin
    old_entries = (repo / "data/entries.json").read_bytes()
    old_checkpoint = (repo / "data/checkpoint.json").read_bytes()
    original_write = repository._write_journal

    def crash_after_write(repo_root, record):
        original_write(repo_root, record)
        if record.get("stage") == crash_stage:
            raise KeyboardInterrupt(f"killed at {crash_stage}")

    monkeypatch.setattr(repository, "_write_journal", crash_after_write)
    with pytest.raises(KeyboardInterrupt, match=crash_stage):
        repository.commit_transaction(prepared, **kwargs)
    monkeypatch.setattr(repository, "_write_journal", original_write)

    reconciled = _reconcile(repo)
    assert reconciled["stage"] == "rolled_back"
    assert (repo / "data/entries.json").read_bytes() == old_entries
    assert (repo / "data/checkpoint.json").read_bytes() == old_checkpoint
    assert _git(repo, "rev-parse", "HEAD") == kwargs["starting_head"]
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_precommit_crash_recovery_preserves_later_canonical_edit(
    tmp_path, monkeypatch,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin
    original_write = repository._write_journal

    def crash_after_replacement(repo_root, record):
        original_write(repo_root, record)
        if record.get("stage") == "canonical_replaced":
            raise KeyboardInterrupt("killed after canonical replacement")

    monkeypatch.setattr(repository, "_write_journal", crash_after_replacement)
    with pytest.raises(KeyboardInterrupt, match="canonical replacement"):
        repository.commit_transaction(prepared, **kwargs)
    monkeypatch.setattr(repository, "_write_journal", original_write)

    user_edit = b'{"schema_version": 2, "entries": [], "note": "keep me"}\n'
    (repo / "data/entries.json").write_bytes(user_edit)
    with pytest.raises(repository.RepositoryError, match="preserved for manual recovery"):
        _reconcile(repo)

    assert (repo / "data/entries.json").read_bytes() == user_edit
    assert repository._read_journal(repo)["stage"] == "canonical_replaced"


def test_committed_crash_reconciles_only_exact_commit_and_pushes_same_oid(
    tmp_path, monkeypatch,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    original_write = repository._write_journal

    def crash_after_committed(repo_root, record):
        original_write(repo_root, record)
        if record.get("stage") == "committed":
            raise KeyboardInterrupt("killed after commit")

    monkeypatch.setattr(repository, "_write_journal", crash_after_committed)
    with pytest.raises(KeyboardInterrupt, match="after commit"):
        repository.commit_transaction(prepared, **kwargs)
    committed_oid = _git(repo, "rev-parse", "HEAD")
    assert committed_oid != kwargs["starting_head"]
    monkeypatch.setattr(repository, "_write_journal", original_write)

    reconciled = _reconcile(repo)
    assert reconciled["stage"] == "push_verified"
    assert reconciled["commit_oid"] == committed_oid
    assert _git(origin, "rev-parse", "refs/heads/main") == committed_oid
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == committed_oid
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert reconciled["tracking_ref_synced"] is True


def test_process_death_after_head_cas_keeps_exact_index_recoverable(
    tmp_path, monkeypatch,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    original_git = repository._git

    def die_after_update_ref(repo_root, *args):
        result = original_git(repo_root, *args)
        if args and args[0] == "update-ref":
            raise KeyboardInterrupt("killed after HEAD compare-and-swap")
        return result

    monkeypatch.setattr(repository, "_git", die_after_update_ref)
    with pytest.raises(KeyboardInterrupt, match="HEAD compare-and-swap"):
        repository.commit_transaction(prepared, **kwargs)
    committed_oid = original_git(repo, "rev-parse", "HEAD").strip()
    assert committed_oid != kwargs["starting_head"]
    assert set(_git(repo, "diff", "--cached", "--name-only").splitlines()) == {
        "data/checkpoint.json",
        "data/entries.json",
    }
    monkeypatch.setattr(repository, "_git", original_git)

    reconciled = _reconcile(repo)
    assert reconciled["stage"] == "push_verified"
    assert reconciled["commit_oid"] == committed_oid
    assert _git(origin, "rev-parse", "refs/heads/main") == committed_oid
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == committed_oid
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_reconcile_reacquires_unlinked_owned_index_lock(tmp_path, monkeypatch):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    original_git = repository._git

    def die_after_update_ref(repo_root, *args):
        result = original_git(repo_root, *args)
        if args and args[0] == "update-ref":
            raise KeyboardInterrupt("killed after HEAD compare-and-swap")
        return result

    monkeypatch.setattr(repository, "_git", die_after_update_ref)
    with pytest.raises(KeyboardInterrupt, match="HEAD compare-and-swap"):
        repository.commit_transaction(prepared, **kwargs)
    index_lock = repo / ".git/index.lock"
    assert index_lock.is_file()
    index_lock.unlink()
    monkeypatch.setattr(repository, "_git", original_git)

    reconciled = _reconcile(repo)

    assert reconciled["stage"] == "push_verified"
    assert _git(origin, "rev-parse", "refs/heads/main") == reconciled["commit_oid"]
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert not index_lock.exists()


def test_git_add_after_owned_index_promotion_is_preserved(tmp_path, monkeypatch):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin
    note = repo / "notes.txt"
    note.write_text("user staged this\n", encoding="utf-8")
    original_write = repository._write_journal
    staged = False

    def stage_after_promotion(repo_root, record):
        nonlocal staged
        original_write(repo_root, record)
        if record.get("stage") == "index_promoted" and not staged:
            staged = True
            _git(repo_root, "add", "notes.txt")

    monkeypatch.setattr(repository, "_write_journal", stage_after_promotion)
    commit_oid = repository.commit_transaction(prepared, **kwargs)

    assert _git(repo, "rev-parse", "HEAD") == commit_oid
    assert _git(repo, "diff", "--cached", "--name-only") == "notes.txt"
    assert subprocess.run(
        ["git", "show", f"{commit_oid}:notes.txt"], cwd=repo,
        capture_output=True, check=False,
    ).returncode != 0


def test_git_add_after_empty_index_snapshot_aborts_without_losing_staging(
    tmp_path, monkeypatch,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin
    note = repo / "notes.txt"
    note.write_text("user staged this\n", encoding="utf-8")
    original_snapshot = repository._snapshot_empty_index
    staged = False

    def stage_after_snapshot(repo_root):
        nonlocal staged
        digest = original_snapshot(repo_root)
        if not staged:
            staged = True
            _git(repo_root, "add", "notes.txt")
        return digest

    monkeypatch.setattr(repository, "_snapshot_empty_index", stage_after_snapshot)
    with pytest.raises(repository.RepositoryError, match="rolled back"):
        repository.commit_transaction(prepared, **kwargs)

    assert _git(repo, "rev-parse", "HEAD") == kwargs["starting_head"]
    assert _git(repo, "diff", "--cached", "--name-only") == "notes.txt"
    assert note.read_text(encoding="utf-8") == "user staged this\n"


def test_foreign_git_index_lock_is_preserved_and_publication_rolls_back(
    tmp_path, monkeypatch,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin
    old_entries = (repo / "data/entries.json").read_bytes()
    original_write = repository._write_journal
    foreign_lock = repo / ".git/index.lock"

    def create_foreign_lock(repo_root, record):
        original_write(repo_root, record)
        if record.get("stage") == "index_lock_intent":
            foreign_lock.write_bytes(b"foreign git process")

    monkeypatch.setattr(repository, "_write_journal", create_foreign_lock)
    with pytest.raises(repository.RepositoryError, match="rolled back"):
        repository.commit_transaction(prepared, **kwargs)

    assert foreign_lock.read_bytes() == b"foreign git process"
    assert (repo / "data/entries.json").read_bytes() == old_entries
    assert repository._read_journal(repo)["stage"] == "rolled_back"


def test_push_verified_crash_completes_callback_then_closes_journal(
    tmp_path,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin

    def crash_callback(_commit):
        raise KeyboardInterrupt("killed before job completion")

    with pytest.raises(KeyboardInterrupt, match="job completion"):
        repository.commit_transaction(
            prepared, on_push_verified=crash_callback, **kwargs,
        )
    assert repository._read_journal(repo)["stage"] == "push_verified"
    completed: list[str] = []
    repository.commit_transaction(
        prepared, on_push_verified=completed.append, **kwargs,
    )
    reconciled = repository._read_journal(repo)
    assert reconciled["stage"] == "closed"
    assert completed == [reconciled["commit_oid"]]


def test_remote_advance_blocks_committed_journal_reconcile(tmp_path, monkeypatch):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    original_push = repository._push_exact

    def fail_push(*args, **kwargs):
        raise repository.RepositoryError("simulated push failure")

    monkeypatch.setattr(repository, "_push_exact", fail_push)
    with pytest.raises(repository.RepositoryError, match="push failure"):
        repository.commit_transaction(prepared, **kwargs)
    monkeypatch.setattr(repository, "_push_exact", original_push)
    assert repository._read_journal(repo)["stage"] == "committed"

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(origin), str(other))
    _git(other, "config", "user.name", "Other Writer")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "remote.txt").write_text("advanced\n", encoding="utf-8")
    _git(other, "add", "remote.txt")
    _git(other, "commit", "-m", "remote advance")
    _git(other, "push", "origin", "main")

    with pytest.raises(repository.RepositoryError, match="remote advanced"):
        _reconcile(repo)


def test_finalize_lock_is_held_through_completion_callback_and_journal_close(tmp_path):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin

    def assert_locked(_commit):
        script = (
            "import fcntl,sys; "
            f"f=open({str(repo / '.work/finalize.lock')!r},'a+b'); "
            "\ntry: fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB); sys.exit(0)"
            "\nexcept BlockingIOError: sys.exit(7)"
        )
        result = subprocess.run([os.sys.executable, "-c", script], check=False)
        assert result.returncode == 7

    repository.commit_transaction(
        prepared, on_push_verified=assert_locked, **kwargs,
    )
    assert repository._read_journal(repo)["stage"] == "closed"


def test_closed_journal_allows_later_ordinary_commit_and_next_publication(tmp_path):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    repository.commit_transaction(prepared, **kwargs)
    first_publication = _git(repo, "rev-parse", "HEAD")

    (repo / "README.md").write_text("ordinary change\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "ordinary change")
    _git(repo, "push", "origin", "main")
    ordinary_head = _git(repo, "rev-parse", "HEAD")
    assert ordinary_head != first_publication
    assert _reconcile(repo)["stage"] == "closed"

    second = repository.prepare_transaction(
        repo_root=repo,
        merged=models.EntriesDocument(
            2,
            (
                _entry("kn_a", "2026-08-01T00:00:00Z"),
                _entry("kn_b", "2026-08-02T00:00:00Z"),
            ),
        ),
        checkpoint=models.Checkpoint(1, "2026-08-03T00:00:00Z", {}),
        transaction_dir=repo / ".work/transaction-2",
    )
    second_oid = repository.commit_transaction(
        second,
        job_id="job_20260812T010204Z_0123456789ab",
        starting_head=ordinary_head,
        expected_output_digests={
            "entries_sha256": repository._sha256(second.data_path),
            "checkpoint_sha256": repository._sha256(second.checkpoint_path),
        },
        expected_remote_url=str(origin),
        expected_upstream_oid=ordinary_head,
        capability="test-capability-2",
        authority_binding='{"kind":"test-publication-2"}',
        publication_binding='{"kind":"test-publication-2"}',
    )
    assert _git(origin, "rev-parse", "refs/heads/main") == second_oid


def test_different_job_cannot_replace_push_verified_journal(tmp_path):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)

    def crash_callback(_commit):
        raise KeyboardInterrupt("job state not completed")

    with pytest.raises(KeyboardInterrupt, match="not completed"):
        repository.commit_transaction(
            prepared, on_push_verified=crash_callback, **kwargs,
        )
    pushed_oid = _git(origin, "rev-parse", "refs/heads/main")
    assert repository._read_journal(repo)["stage"] == "push_verified"

    second = repository.prepare_transaction(
        repo_root=repo,
        merged=models.EntriesDocument(2, (_entry("kn_b", "2026-08-02T00:00:00Z"),)),
        checkpoint=models.Checkpoint(1, "2026-08-03T00:00:00Z", {}),
        transaction_dir=repo / ".work/transaction-2",
    )
    with pytest.raises(repository.RepositoryError, match="exact capability"):
        repository.commit_transaction(
            second,
            job_id="job_20260812T010204Z_0123456789ab",
            starting_head=pushed_oid,
            expected_output_digests={
                "entries_sha256": repository._sha256(second.data_path),
                "checkpoint_sha256": repository._sha256(second.checkpoint_path),
            },
            expected_remote_url=str(origin),
            expected_upstream_oid=pushed_oid,
            capability="test-capability-2",
            authority_binding='{"kind":"test-publication-2"}',
            publication_binding='{"kind":"test-publication-2"}',
        )
    assert repository._read_journal(repo)["publication_id"] == kwargs["job_id"]


def test_different_job_capability_cannot_reconcile_committed_journal(
    tmp_path, monkeypatch,
):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    starting_remote = _git(origin, "rev-parse", "refs/heads/main")
    original_push = repository._push_exact

    def stop_before_push(*_args, **_kwargs):
        raise repository.RepositoryError("simulated network stop")

    monkeypatch.setattr(repository, "_push_exact", stop_before_push)
    with pytest.raises(repository.RepositoryError, match="simulated network stop"):
        repository.commit_transaction(prepared, **kwargs)
    assert repository._read_journal(repo)["stage"] == "committed"
    assert _git(origin, "rev-parse", "refs/heads/main") == starting_remote

    consumed = []
    monkeypatch.setattr(repository, "_push_exact", original_push)
    monkeypatch.setattr(
        repository, "_consume_bound_capability",
        lambda capability, authority_binding: consumed.append(
            (capability, authority_binding)
        ),
    )
    second = repository.prepare_transaction(
        repo_root=repo,
        merged=models.EntriesDocument(2, (_entry("kn_b", "2026-08-02T00:00:00Z"),)),
        checkpoint=models.Checkpoint(1, "2026-08-03T00:00:00Z", {}),
        transaction_dir=repo / ".work/transaction-2",
    )
    with pytest.raises(repository.RepositoryError, match="exact capability"):
        repository.commit_transaction(
            second,
            job_id="job_20260812T010204Z_0123456789ab",
            starting_head=_git(repo, "rev-parse", "HEAD"),
            expected_output_digests={
                "entries_sha256": repository._sha256(second.data_path),
                "checkpoint_sha256": repository._sha256(second.checkpoint_path),
            },
            expected_remote_url=str(origin),
            expected_upstream_oid=starting_remote,
            capability="test-capability-2",
            authority_binding='{"kind":"test-publication-2"}',
            publication_binding='{"kind":"test-publication-2"}',
        )
    assert consumed == []
    assert _git(origin, "rev-parse", "refs/heads/main") == starting_remote


def test_publication_rejects_local_head_ahead_of_upstream(tmp_path):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    (repo / "local.txt").write_text("not published\n", encoding="utf-8")
    _git(repo, "add", "local.txt")
    _git(repo, "commit", "-m", "local only")
    local_head = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(repository.RepositoryError, match="equal upstream"):
        repository.commit_transaction(
            prepared,
            **{
                **kwargs,
                "starting_head": local_head,
                "expected_upstream_oid": kwargs["starting_head"],
            },
        )
    assert _git(origin, "rev-parse", "refs/heads/main") == kwargs["starting_head"]


def test_journal_and_backup_symlinks_are_rejected(tmp_path):
    repo, origin, prepared, kwargs = _publication_repo(tmp_path)
    del origin
    outside = tmp_path / "outside.json"
    outside.write_text("do not overwrite\n", encoding="utf-8")
    journal = repo / ".work/finalize-journal.json"
    journal.symlink_to(outside)
    with pytest.raises(repository.RepositoryError, match="durable"):
        _reconcile(repo)
    journal.unlink()

    backup_dir = repo / ".work/publications" / kwargs["job_id"]
    backup_dir.mkdir(parents=True)
    (backup_dir / "entries.before.json").symlink_to(outside)
    with pytest.raises(repository.RepositoryError, match="destination"):
        repository.commit_transaction(prepared, **kwargs)
    assert outside.read_text(encoding="utf-8") == "do not overwrite\n"
