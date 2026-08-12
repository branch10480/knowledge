"""Durable orchestration for Knowledge collection and summary jobs.

The collector is deliberately outside inference admission control.  A job first
persists candidates, then a detached runner submits one summary at a time to the
existing worker proxy.  Candidate receipts make retries idempotent across
runner crashes; canonical data is touched only by the finalizer.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import models
from .summarizer import SummaryOutput


SCHEMA_VERSION = 1
TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled", "ready_for_publish"})
ACTIVE_PHASES = frozenset({
    "collecting",
    "waiting_for_inference",
    "summarizing",
    "finalizing",
})
_JOB_ID = re.compile(r"^job_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:@+-]{1,200}$")


class JobError(RuntimeError):
    pass


class JobAlreadyRunning(JobError):
    pass


class JobCancelled(JobError):
    pass


def safe_error_message(error: BaseException) -> str:
    """Return state/log text that cannot echo candidate or model content."""

    if isinstance(error, JobError):
        message = re.sub(r"[\x00-\x1f\x7f]", " ", str(error)).strip()
        return message[:500] or "Knowledge job failed"
    return "Knowledge job operation failed; inspect the local bounded diagnostics"


def _error_record(error: BaseException) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "message": safe_error_message(error),
    }


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _open_directory_without_symlinks(path: Path) -> int:
    """Open one absolute directory by walking every component with NOFOLLOW."""

    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            component_flags = flags
            if hasattr(os, "O_NOFOLLOW"):
                component_flags |= os.O_NOFOLLOW
            child = os.open(component, component_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise JobError(f"unsafe durable directory: {absolute.name}")
        return descriptor
    except JobError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise JobError(f"unsafe durable directory: {absolute.name}") from error


def _atomic_json(path: Path, value: Mapping[str, Any] | list[Any]) -> None:
    path = Path(os.path.abspath(path))
    directory_fd = _open_directory_without_symlinks(path.parent)
    temporary_name = f".{path.name}.{secrets.token_hex(12)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    except Exception:
        os.close(directory_fd)
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        try:
            os.fsync(directory_fd)
        except OSError:
            # Some filesystems do not support directory fsync. The file itself
            # is already durable and os.replace remains atomic.
            pass
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _read_regular_bytes(path: Path) -> bytes:
    path = Path(os.path.abspath(path))
    directory_fd = _open_directory_without_symlinks(path.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_fd)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise JobError(f"unsafe durable file: {path.name}")
            return handle.read()
    except JobError:
        raise
    except OSError as error:
        raise JobError(f"could not read durable job file: {path.name}") from error
    finally:
        os.close(directory_fd)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JobError(f"could not read durable job file: {path.name}") from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256(_read_regular_bytes(path)).hexdigest()
    return f"sha256:{digest}"


def file_sha256(path: Path) -> str:
    """Hash one regular, single-link file without following a symlink."""

    return _sha256_file(path)


def _absolute_without_symlinks(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise JobError(f"durable job path contains a symlink: {current.name}")
    return absolute


def _open_lock_file(path: Path):
    path = Path(os.path.abspath(path))
    directory_fd = _open_directory_without_symlinks(path.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise JobError(f"unsafe durable lock file: {path.name}")
        return os.fdopen(descriptor, "a+b")
    except JobError:
        raise
    except OSError as error:
        raise JobError(f"could not open durable lock file: {path.name}") from error
    finally:
        os.close(directory_fd)


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise JobError("could not resolve repository HEAD")
    return result.stdout.strip()


def _assert_start_snapshot_unchanged(repo_root: Path, state: Mapping[str, Any]) -> None:
    checks = (
        ("starting_head", _git_head(repo_root)),
        ("checkpoint_sha256", _sha256_file(repo_root / "data/checkpoint.json")),
        ("sources_config_sha256", _sha256_file(repo_root / "config/sources.yml")),
        ("summary_config_sha256", _sha256_file(repo_root / "config/summary.yml")),
    )
    for field, current in checks:
        if state.get(field) != current:
            raise JobError(f"job start snapshot changed: {field}")


def _validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise JobError("invalid job id")
    return job_id


def _validate_idempotency_key(key: str) -> str:
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise JobError("invalid idempotency key")
    return key


def _receipt_name(candidate_id: str) -> str:
    return hashlib.sha256(candidate_id.encode("utf-8")).hexdigest() + ".json"


def _candidate_sha256(candidate: models.Candidate) -> str:
    payload = json.dumps(
        candidate.__dict__, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _summary_json(
    summary: SummaryOutput,
    *,
    candidate_sha256: str,
    summary_config_sha256: str,
) -> dict[str, Any]:
    return {
        "candidate_id": summary.candidate_id,
        "candidate_sha256": candidate_sha256,
        "summary_config_sha256": summary_config_sha256,
        "title_ja": summary.title_ja,
        "summary_ja": summary.summary_ja,
        "key_points": list(summary.key_points),
        "tags": list(summary.tags),
        "claims": list(summary.claims),
        "insufficient_evidence": summary.insufficient_evidence,
    }


class JobStore:
    def __init__(self, root: Path):
        self.root = _absolute_without_symlinks(root)

    def job_dir(self, job_id: str) -> Path:
        path = self.root / _validate_job_id(job_id)
        if path.is_symlink():
            raise JobError("durable job directory must not be a symlink")
        return path

    def state_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "state.json"

    def load(self, job_id: str) -> dict[str, Any]:
        state = _read_json(self.state_path(job_id))
        if not isinstance(state, dict) or state.get("job_id") != job_id:
            raise JobError("job state identity mismatch")
        return state

    def write(self, state: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _validate_job_id(str(state.get("job_id", "")))
        updated = dict(state)
        updated["schema_version"] = SCHEMA_VERSION
        updated.setdefault("generation", 0)
        updated["updated_at"] = utcnow()
        _atomic_json(self.state_path(job_id), updated)
        return updated

    def update(
        self,
        job_id: str,
        *,
        expected_generation: int | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        with self.state_lock(job_id):
            state = self.load(job_id)
            generation = int(state.get("generation", 0))
            if expected_generation is not None and generation != expected_generation:
                raise JobError("job state generation changed")
            state.update(changes)
            state["generation"] = generation + 1
            return self.write(state)

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        key = _validate_idempotency_key(key)
        if not self.root.exists():
            return None
        for state_path in sorted(self.root.glob("job_*/state.json"), reverse=True):
            try:
                state = self.load(state_path.parent.name)
            except JobError:
                continue
            if isinstance(state, dict) and state.get("idempotency_key") == key:
                return state
        return None

    def latest(self) -> dict[str, Any] | None:
        if not self.root.exists():
            return None
        paths = sorted(self.root.glob("job_*/state.json"), reverse=True)
        return self.load(paths[0].parent.name) if paths else None

    @contextmanager
    def start_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with _open_lock_file(self.root / ".start.lock") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def runner_lock(self, job_id: str) -> Iterator[None]:
        path = self.job_dir(job_id) / "runner.lock"
        with _open_lock_file(path) as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise JobAlreadyRunning(f"job already running: {job_id}") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def state_lock(self, job_id: str) -> Iterator[None]:
        path = self.job_dir(job_id) / "state.lock"
        with _open_lock_file(path) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prepare_job(
    *,
    repo_root: Path,
    store: JobStore,
    idempotency_key: str,
    collect: Callable[[Path, str], int],
    origin_session_id: str = "",
    origin_turn_id: str = "",
    run_started_at: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create/reuse a job and persist collect output without checking DS4."""

    idempotency_key = _validate_idempotency_key(idempotency_key)
    run_started_at = run_started_at or utcnow()
    with store.start_lock():
        existing = store.find_by_idempotency_key(idempotency_key)
        reused = existing is not None
        if existing is not None:
            recoverable_collect = existing.get("phase") == "collecting" or (
                existing.get("phase") == "failed"
                and not existing.get("candidates_sha256")
            )
            if not recoverable_collect:
                return existing, True
            _assert_start_snapshot_unchanged(repo_root, existing)
            state = store.update(
                existing["job_id"],
                phase="collecting",
                error=None,
            )
            run_started_at = str(state["run_started_at"])
        else:
            stamp = run_started_at.replace("-", "").replace(":", "")
            stamp = stamp.split(".", 1)[0].replace("+0000", "Z")
            if not stamp.endswith("Z"):
                stamp += "Z"
            suffix = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
            job_id = f"job_{stamp}_{suffix}"
            if not _JOB_ID.fullmatch(job_id):
                raise JobError("run_started_at must be an ISO8601 UTC timestamp")
            job_dir = store.job_dir(job_id)
            job_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            state = store.write({
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "idempotency_key": idempotency_key,
                "origin_session_id": origin_session_id,
                "origin_turn_id": origin_turn_id,
                "phase": "collecting",
                "created_at": utcnow(),
                "run_started_at": run_started_at,
                "starting_head": _git_head(repo_root),
                "checkpoint_sha256": _sha256_file(repo_root / "data/checkpoint.json"),
                "sources_config_sha256": _sha256_file(repo_root / "config/sources.yml"),
                "summary_config_sha256": _sha256_file(repo_root / "config/summary.yml"),
                "candidate_ids": [],
                "selected_candidate_ids": [],
                "completed_candidate_ids": [],
                "attempts": 0,
                "cancel_requested": False,
                "runner_pid": None,
                "error": None,
                "result": None,
                "delivery": {"status": "pending"},
            })

        candidates_path = store.job_dir(state["job_id"]) / "candidates.json"
        try:
            status = collect(candidates_path, run_started_at)
            if status != 0:
                raise JobError(f"collect failed with status {status}")
            candidate_data = _read_json(candidates_path)
            candidates = (
                candidate_data.get("candidates")
                if isinstance(candidate_data, dict)
                else None
            )
            selected = (
                candidate_data.get("selected_candidate_ids")
                if isinstance(candidate_data, dict)
                else None
            )
            if not isinstance(candidates, list) or not isinstance(selected, list):
                raise JobError("collect output does not satisfy the durable job contract")
            candidate_ids = [
                item.get("candidate_id")
                for item in candidates
                if isinstance(item, dict)
            ]
            if any(not isinstance(item, str) or not item for item in candidate_ids):
                raise JobError("collect output contains an invalid candidate id")
            if any(not isinstance(item, str) or not item for item in selected):
                raise JobError("collect output contains an invalid selected candidate id")
            if not set(selected).issubset(set(candidate_ids)):
                raise JobError("selected candidates are not present in collect output")
            # The collector may use an ordinary write. Re-persist its validated
            # payload with the same fsync + replace contract as state.json
            # before advertising waiting_for_inference.
            _atomic_json(candidates_path, candidate_data)
            state = store.update(
                state["job_id"],
                phase="waiting_for_inference",
                candidates_sha256=_sha256_file(candidates_path),
                candidate_ids=candidate_ids,
                selected_candidate_ids=selected,
            )
            return state, reused
        except Exception as error:
            store.update(
                state["job_id"],
                phase="failed",
                error=_error_record(error),
            )
            raise


def spawn_job_runner(*, repo_root: Path, store: JobStore, job_id: str) -> int:
    """Start a detached runner for non-Hermes callers.

    Hermes' plugin uses its managed background process registry instead so the
    originating session receives ``notify_on_complete``.
    """

    job_id = _validate_job_id(job_id)
    log_path = store.job_dir(job_id) / "runner.log"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "knowledge.cli", "job-run", "--job-id", job_id],
            cwd=repo_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    store.update(job_id, runner_pid=process.pid)
    return process.pid


def cancel_job(*, store: JobStore, job_id: str) -> dict[str, Any]:
    state = store.load(job_id)
    if state.get("phase") in TERMINAL_PHASES:
        return state
    return store.update(job_id, cancel_requested=True)


def write_finalize_receipt(
    *, job_dir: Path, job_id: str, summaries_sha256: str, result: Mapping[str, Any]
) -> dict[str, Any]:
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "job_id": _validate_job_id(job_id),
        "summaries_sha256": summaries_sha256,
        "result": dict(result),
        "written_at": utcnow(),
    }
    _atomic_json(job_dir / "finalize-receipt.json", receipt)
    return receipt


def read_finalize_receipt(
    *, job_dir: Path, job_id: str, summaries_sha256: str
) -> dict[str, Any] | None:
    path = job_dir / "finalize-receipt.json"
    if not path.exists():
        return None
    receipt = _read_json(path)
    if not isinstance(receipt, dict) or receipt.get("job_id") != _validate_job_id(job_id):
        raise JobError("finalize receipt identity mismatch")
    if receipt.get("summaries_sha256") != summaries_sha256:
        raise JobError("finalize receipt summaries digest mismatch")
    if not isinstance(receipt.get("result"), dict):
        raise JobError("finalize receipt result is invalid")
    return receipt


def _validate_ready_for_publish_result(
    *,
    job_dir: Path,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Recompute every READY manifest digest before trusting a receipt."""

    expected = {
        "starting_head": state.get("starting_head"),
        "candidates_sha256": _sha256_file(job_dir / "final-candidates.json"),
        "summaries_sha256": _sha256_file(job_dir / "summaries.json"),
        "merged_sha256": _sha256_file(job_dir / "merged.json"),
    }
    for field, value in expected.items():
        if not isinstance(result.get(field), str) or result.get(field) != value:
            raise JobError(f"ready-for-publish manifest mismatch: {field}")


def run_job(
    *,
    store: JobStore,
    job_id: str,
    summarize_one: Callable[[models.Candidate], SummaryOutput],
    finalize: Callable[[Path], Mapping[str, Any]],
) -> dict[str, Any]:
    """Resume one job from durable receipts and finalize it transactionally."""

    job_id = _validate_job_id(job_id)
    with store.runner_lock(job_id):
        state = store.load(job_id)
        if state.get("phase") in {"completed", "cancelled", "ready_for_publish"}:
            return state
        state = store.update(
            job_id,
            phase="summarizing",
            attempts=int(state.get("attempts", 0)) + 1,
            runner_pid=os.getpid(),
            error=None,
        )
        job_dir = store.job_dir(job_id)
        try:
            candidates_path = job_dir / "candidates.json"
            if _sha256_file(candidates_path) != state.get("candidates_sha256"):
                raise JobError("candidate digest mismatch")
            candidate_data = _read_json(candidates_path)
            candidates = {
                item["candidate_id"]: models.Candidate(**item)
                for item in candidate_data["candidates"]
            }
            selected = list(state.get("selected_candidate_ids", []))
            receipts_dir = job_dir / "summaries"
            receipts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

            completed: list[str] = []
            for candidate_id in selected:
                receipt_path = receipts_dir / _receipt_name(candidate_id)
                if receipt_path.exists():
                    receipt = _read_json(receipt_path)
                    if receipt.get("candidate_id") != candidate_id:
                        raise JobError("summary receipt identity mismatch")
                    candidate = candidates.get(candidate_id)
                    if candidate is None:
                        raise JobError("selected candidate is missing")
                    if receipt.get("candidate_sha256") != _candidate_sha256(candidate):
                        raise JobError("summary receipt candidate digest mismatch")
                    if (
                        receipt.get("summary_config_sha256")
                        != state.get("summary_config_sha256")
                    ):
                        raise JobError("summary receipt config digest mismatch")
                    completed.append(candidate_id)

            state = store.update(job_id, completed_candidate_ids=completed)
            for candidate_id in selected:
                state = store.load(job_id)
                if state.get("cancel_requested"):
                    return store.update(job_id, phase="cancelled", runner_pid=None)
                if candidate_id in completed:
                    continue
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    raise JobError("selected candidate is missing")
                summary = summarize_one(candidate)
                if summary.candidate_id != candidate_id:
                    raise JobError("summary candidate id mismatch")
                _atomic_json(
                    receipts_dir / _receipt_name(candidate_id),
                    _summary_json(
                        summary,
                        candidate_sha256=_candidate_sha256(candidate),
                        summary_config_sha256=str(state["summary_config_sha256"]),
                    ),
                )
                completed.append(candidate_id)
                store.update(job_id, completed_candidate_ids=list(completed))

            state = store.load(job_id)
            if state.get("cancel_requested"):
                return store.update(job_id, phase="cancelled", runner_pid=None)

            summaries = [
                _read_json(receipts_dir / _receipt_name(candidate_id))
                for candidate_id in selected
            ]
            insufficient_ids = [
                item["candidate_id"]
                for item in summaries
                if item.get("insufficient_evidence") is True
            ]
            deferred = list(candidate_data.get("deferred_candidate_ids", []))
            candidate_data["deferred_candidate_ids"] = list(dict.fromkeys(
                [*deferred, *insufficient_ids]
            ))
            final_candidates_path = job_dir / "final-candidates.json"
            _atomic_json(final_candidates_path, candidate_data)
            store.update(
                job_id,
                final_candidates_sha256=_sha256_file(final_candidates_path),
            )
            _atomic_json(job_dir / "summaries.json", summaries)
            state = store.update(
                job_id,
                phase="finalizing",
                summaries_sha256=_sha256_file(job_dir / "summaries.json"),
            )
            receipt = read_finalize_receipt(
                job_dir=job_dir,
                job_id=job_id,
                summaries_sha256=state["summaries_sha256"],
            )
            reused_finalize_receipt = receipt is not None
            if receipt is None:
                result = dict(finalize(job_dir))
                receipt = write_finalize_receipt(
                    job_dir=job_dir,
                    job_id=job_id,
                    summaries_sha256=state["summaries_sha256"],
                    result=result,
                )
            else:
                result = dict(receipt["result"])
            if reused_finalize_receipt and result.get("ready_for_publish") is True:
                # READY has no publication side effect, so crash resume reruns
                # full finalization/QA instead of trusting writable job state.
                result = dict(finalize(job_dir))
                receipt = write_finalize_receipt(
                    job_dir=job_dir,
                    job_id=job_id,
                    summaries_sha256=state["summaries_sha256"],
                    result=result,
                )
            if result.get("ready_for_publish") is True:
                _validate_ready_for_publish_result(
                    job_dir=job_dir,
                    state=state,
                    result=result,
                )
            terminal_phase = (
                "ready_for_publish"
                if result.get("ready_for_publish") is True
                else "completed"
            )
            return store.update(
                job_id,
                phase=terminal_phase,
                runner_pid=None,
                result=result,
                error=None,
            )
        except JobCancelled:
            return store.update(
                job_id,
                phase="cancelled",
                runner_pid=None,
                error=None,
            )
        except Exception as error:
            store.update(
                job_id,
                phase="failed",
                runner_pid=None,
                error=_error_record(error),
            )
            raise


__all__ = [
    "ACTIVE_PHASES",
    "JobAlreadyRunning",
    "JobCancelled",
    "JobError",
    "JobStore",
    "TERMINAL_PHASES",
    "cancel_job",
    "file_sha256",
    "prepare_job",
    "read_finalize_receipt",
    "run_job",
    "safe_error_message",
    "spawn_job_runner",
    "utcnow",
    "write_finalize_receipt",
]
