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
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import models
from .summarizer import SummaryOutput, TransientInferenceError


SCHEMA_VERSION = 1
RUNNER_LEASE_SECONDS = 300
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300
MAX_INFERENCE_RETRIES = 8
MAX_INFERENCE_RETRY_ELAPSED_SECONDS = 1800
LEASE_HEARTBEAT_SECONDS = 30
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


class RetryableInferenceError(JobError):
    """A proxy failure which is safe to retry without changing candidates."""


def is_retryable_inference_failure(error: BaseException) -> bool:
    """Accept only explicit typed transient inference failures."""

    return isinstance(error, (RetryableInferenceError, TransientInferenceError))


def _retry_delay(attempt: int) -> int:
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** min(max(attempt - 1, 0), 6)))


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


def _canonical_json_bytes(
    value: Mapping[str, Any] | list[Any], *, sort_keys: bool = True,
) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n"
    ).encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
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
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
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


def _atomic_json(
    path: Path,
    value: Mapping[str, Any] | list[Any],
    *,
    sort_keys: bool = True,
) -> None:
    _atomic_write_bytes(path, _canonical_json_bytes(value, sort_keys=sort_keys))


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


def read_regular_bytes(path: Path) -> bytes:
    """Read one regular, single-link file without following symlinks."""

    return _read_regular_bytes(path)


def write_json_file(path: Path, value: Mapping[str, Any] | list[Any]) -> None:
    """Write canonical durable JSON with fsync + atomic replace."""

    _atomic_json(path, value, sort_keys=False)


def write_bytes_file(path: Path, payload: bytes) -> None:
    """Write already-canonical bytes with fsync + atomic replace."""

    _atomic_write_bytes(path, payload)


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
    from . import repository

    try:
        return repository._git(repo_root, "rev-parse", "HEAD").strip()
    except repository.RepositoryError as error:
        raise JobError("could not resolve repository HEAD") from error


def _git_remote_url(repo_root: Path) -> str:
    from . import repository

    try:
        url = repository._git(repo_root, "remote", "get-url", "origin").strip()
    except repository.RepositoryError as error:
        raise JobError("could not resolve canonical repository remote URL") from error
    if not url:
        raise JobError("could not resolve canonical repository remote URL")
    return url


def _git_remote_oid(repo_root: Path) -> str:
    from . import repository

    try:
        oid = repository._remote_oid(repo_root, _git_remote_url(repo_root), "main")
    except repository.RepositoryError as error:
        raise JobError("could not resolve starting remote OID") from error
    if not oid:
        raise JobError("could not resolve starting remote OID")
    return oid


def _assert_start_snapshot_unchanged(repo_root: Path, state: Mapping[str, Any]) -> None:
    checks = (
        ("starting_head", _git_head(repo_root)),
        ("checkpoint_sha256", _sha256_file(repo_root / "data/checkpoint.json")),
        ("sources_config_sha256", _sha256_file(repo_root / "config/sources.yml")),
        ("summary_config_sha256", _sha256_file(repo_root / "config/summary.yml")),
        ("canonical_remote_url", _git_remote_url(repo_root)),
        ("starting_remote_oid", _git_remote_oid(repo_root)),
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


def validate_publication_manifest(publication: Mapping[str, Any]) -> None:
    """Validate the exact canonical capability binding shape."""

    if set(publication) != {
        "schema_version",
        "kind",
        "job_id",
        "starting_head",
        "merged_sha256",
        "checkpoint_output_sha256",
        "candidates_sha256",
        "summaries_sha256",
    }:
        raise JobError("publication manifest contract is invalid")
    if (
        publication.get("schema_version") != 1
        or publication.get("kind") != "knowledge-publication"
        or _JOB_ID.fullmatch(str(publication.get("job_id", ""))) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(publication.get("starting_head", "")))
        is None
    ):
        raise JobError("publication manifest identity is invalid")
    for field in (
        "merged_sha256",
        "checkpoint_output_sha256",
        "candidates_sha256",
        "summaries_sha256",
    ):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", str(publication.get(field, ""))) is None:
            raise JobError("publication manifest digest is invalid")


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


def _collected_candidate_metadata(candidate_data: Any) -> tuple[list[str], list[str]]:
    candidates = candidate_data.get("candidates") if isinstance(candidate_data, dict) else None
    selected = candidate_data.get("selected_candidate_ids") if isinstance(candidate_data, dict) else None
    if not isinstance(candidates, list) or not isinstance(selected, list):
        raise JobError("collect output does not satisfy the durable job contract")
    candidate_ids = [
        item.get("candidate_id") for item in candidates if isinstance(item, dict)
    ]
    if len(candidate_ids) != len(candidates) or any(
        not isinstance(item, str) or not item for item in candidate_ids
    ):
        raise JobError("collect output contains an invalid candidate id")
    if any(not isinstance(item, str) or not item for item in selected):
        raise JobError("collect output contains an invalid selected candidate id")
    if not set(selected).issubset(set(candidate_ids)):
        raise JobError("selected candidates are not present in collect output")
    return candidate_ids, selected


def _write_collect_receipt(
    *, job_dir: Path, job_id: str, run_started_at: str, candidates_sha256: str,
) -> dict[str, Any]:
    """Persist proof that collect returned success for this exact payload."""

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "job_id": _validate_job_id(job_id),
        "run_started_at": run_started_at,
        "candidates_sha256": candidates_sha256,
        "status": "succeeded",
        "written_at": utcnow(),
    }
    _atomic_json(job_dir / "collect-receipt.json", receipt)
    return receipt


def _validated_collect_receipt(
    *, job_dir: Path, state: Mapping[str, Any], candidates_sha256: str,
) -> dict[str, Any]:
    receipt = _read_json(job_dir / "collect-receipt.json")
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "job_id",
        "run_started_at",
        "candidates_sha256",
        "status",
        "written_at",
    }:
        raise JobError("collect receipt contract is invalid")
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("job_id") != state.get("job_id")
        or receipt.get("run_started_at") != state.get("run_started_at")
        or receipt.get("candidates_sha256") != candidates_sha256
        or receipt.get("status") != "succeeded"
    ):
        raise JobError("collect receipt identity mismatch")
    return receipt


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

    def all_states(self) -> Iterator[dict[str, Any]]:
        if not self.root.exists():
            return
        for state_path in sorted(self.root.glob("job_*/state.json")):
            try:
                yield self.load(state_path.parent.name)
            except JobError:
                continue

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
    origin_authority_kind: str = "direct_user",
    run_started_at: str | None = None,
    canonical_remote_url: str | None = None,
    starting_head: str | None = None,
    starting_remote_oid: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create/reuse a job and persist collect output without checking DS4."""

    idempotency_key = _validate_idempotency_key(idempotency_key)
    if origin_authority_kind not in {"direct_user", "scheduled"}:
        raise JobError("invalid origin authority kind")
    run_started_at = run_started_at or utcnow()
    with store.start_lock():
        existing = store.find_by_idempotency_key(idempotency_key)
        reused = existing is not None
        if existing is not None:
            if any((
                existing.get("origin_session_id") != origin_session_id,
                existing.get("origin_turn_id") != origin_turn_id,
                existing.get("origin_authority_kind", "direct_user")
                != origin_authority_kind,
            )):
                raise JobError("idempotency key origin binding mismatch")
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
                "origin_authority_kind": origin_authority_kind,
                "phase": "collecting",
                "created_at": utcnow(),
                "run_started_at": run_started_at,
                "starting_head": starting_head or _git_head(repo_root),
                "canonical_remote_url": canonical_remote_url or _git_remote_url(repo_root),
                "starting_remote_oid": starting_remote_oid or _git_remote_oid(repo_root),
                "checkpoint_sha256": _sha256_file(repo_root / "data/checkpoint.json"),
                "sources_config_sha256": _sha256_file(repo_root / "config/sources.yml"),
                "summary_config_sha256": _sha256_file(repo_root / "config/summary.yml"),
                "candidate_ids": [],
                "selected_candidate_ids": [],
                "completed_candidate_ids": [],
                "attempts": 0,
                "cancel_requested": False,
                "runner_pid": None,
                "runner_lease": None,
                "retry_at_epoch": 0,
                "inference_retry_count": 0,
                "inference_retry_started_at_epoch": None,
                "error": None,
                "result": None,
                "completion_event_id": None,
            })

        candidates_path = store.job_dir(state["job_id"]) / "candidates.json"
        try:
            status = collect(candidates_path, run_started_at)
            if status != 0:
                raise JobError(f"collect failed with status {status}")
            candidate_data = _read_json(candidates_path)
            candidate_ids, selected = _collected_candidate_metadata(candidate_data)
            # The collector may use an ordinary write. Re-persist its validated
            # payload with the same fsync + replace contract as state.json
            # before advertising waiting_for_inference.
            candidate_bytes = _canonical_json_bytes(candidate_data)
            _atomic_write_bytes(candidates_path, candidate_bytes)
            candidates_sha256 = "sha256:" + hashlib.sha256(candidate_bytes).hexdigest()
            _write_collect_receipt(
                job_dir=store.job_dir(state["job_id"]),
                job_id=state["job_id"],
                run_started_at=run_started_at,
                candidates_sha256=candidates_sha256,
            )
            state = store.update(
                state["job_id"],
                phase="waiting_for_inference",
                candidates_sha256=candidates_sha256,
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


def _completion_event_id(state: Mapping[str, Any]) -> str:
    payload = {
        "job_id": state["job_id"],
        "phase": state.get("phase"),
        "result": state.get("result"),
        "schema_version": SCHEMA_VERSION,
    }
    material = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "completion_" + hashlib.sha256(material).hexdigest()[:24]


def sweep_jobs(*, store: JobStore, now: float | None = None) -> list[str]:
    """Return jobs whose runner process is proven gone.

    The timestamp lease is advisory.  A long DS4 request may outlive it, so an
    active phase is changed only while this sweeper owns that job's nonblocking
    runner lock.  A live runner keeps the lock for inference, retry sleep, and
    finalization, closing the sweep-versus-inference race.
    """

    now = time.time() if now is None else now
    resumable: list[str] = []
    for snapshot in store.all_states() or ():
        if snapshot.get("phase") not in ACTIVE_PHASES:
            continue
        try:
            with store.runner_lock(snapshot["job_id"]):
                state = store.load(snapshot["job_id"])
                phase = state.get("phase")
                if phase not in ACTIVE_PHASES:
                    continue
                if phase == "collecting":
                    job_dir = store.job_dir(state["job_id"])
                    candidates_path = job_dir / "candidates.json"
                    receipt_path = job_dir / "collect-receipt.json"
                    if not candidates_path.exists() or not receipt_path.exists():
                        continue
                    candidate_data = _read_json(candidates_path)
                    candidate_ids, selected = _collected_candidate_metadata(candidate_data)
                    candidate_bytes = _canonical_json_bytes(candidate_data)
                    _atomic_write_bytes(candidates_path, candidate_bytes)
                    candidates_sha256 = (
                        "sha256:" + hashlib.sha256(candidate_bytes).hexdigest()
                    )
                    _validated_collect_receipt(
                        job_dir=job_dir,
                        state=state,
                        candidates_sha256=candidates_sha256,
                    )
                    recovered = store.update(
                        state["job_id"],
                        phase="waiting_for_inference",
                        candidates_sha256=candidates_sha256,
                        candidate_ids=candidate_ids,
                        selected_candidate_ids=selected,
                        runner_pid=None,
                        runner_lease=None,
                        retry_at_epoch=now,
                        error=None,
                    )
                    resumable.append(recovered["job_id"])
                    continue
                if phase == "waiting_for_inference":
                    # Owning runner.lock proves that no process is waiting for
                    # retry_at_epoch. Start a replacement now; run_job sleeps
                    # until the recorded retry boundary when necessary.
                    resumable.append(state["job_id"])
                    continue
                # runner.lock is the authoritative liveness proof. Timestamp
                # leases are diagnostics only and may survive a hard crash.
                store.update(
                    state["job_id"], phase="waiting_for_inference", runner_pid=None,
                    runner_lease=None, retry_at_epoch=now, error=None,
                )
                resumable.append(state["job_id"])
        except JobAlreadyRunning:
            continue
        except JobError:
            # One corrupt/partial job must not prevent recovery of every other
            # durable job. The original state remains fail-closed for an
            # idempotent start or bounded manual diagnosis.
            continue
    return resumable


@contextmanager
def _runner_lease_heartbeat(
    *, store: JobStore, job_id: str, interval: float, now: Callable[[], float],
) -> Iterator[None]:
    """Refresh the durable lease independently while the DS4 call blocks."""

    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(interval):
            try:
                state = store.load(job_id)
                if state.get("phase") != "summarizing":
                    return
                store.update(
                    job_id,
                    runner_lease={
                        "owner_pid": os.getpid(),
                        "expires_at": now() + RUNNER_LEASE_SECONDS,
                    },
                )
            except JobError:
                return

    thread = threading.Thread(
        target=heartbeat, name=f"knowledge-lease-{job_id}", daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=max(interval * 2, 1))


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
        "job_id": state.get("job_id"),
        "starting_head": state.get("starting_head"),
        "candidates_sha256": _sha256_file(job_dir / "final-candidates.json"),
        "summaries_sha256": _sha256_file(job_dir / "summaries.json"),
        "merged_sha256": _sha256_file(job_dir / "merged.json"),
        "checkpoint_output_sha256": _sha256_file(job_dir / "checkpoint.json"),
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
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    max_inference_retries: int = MAX_INFERENCE_RETRIES,
    max_retry_elapsed_seconds: float = MAX_INFERENCE_RETRY_ELAPSED_SECONDS,
    heartbeat_interval: float = LEASE_HEARTBEAT_SECONDS,
) -> dict[str, Any]:
    """Resume one job from durable receipts and finalize it transactionally."""

    job_id = _validate_job_id(job_id)
    with store.runner_lock(job_id):
        state = store.load(job_id)
        if state.get("phase") in {"completed", "cancelled", "ready_for_publish"}:
            return state
        current_time = now()
        retry_at = float(state.get("retry_at_epoch", 0))
        if state.get("phase") == "waiting_for_inference" and retry_at > current_time:
            sleep(retry_at - current_time)
            current_time = now()
        state = store.update(
            job_id,
            phase="summarizing",
            attempts=int(state.get("attempts", 0)) + 1,
            runner_pid=os.getpid(),
            retry_at_epoch=0,
            runner_lease={"owner_pid": os.getpid(), "expires_at": current_time + RUNNER_LEASE_SECONDS},
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
                    return store.update(
                        job_id, phase="cancelled", runner_pid=None,
                        runner_lease=None, retry_at_epoch=0, error=None,
                    )
                if candidate_id in completed:
                    continue
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    raise JobError("selected candidate is missing")
                while True:
                    try:
                        with _runner_lease_heartbeat(
                            store=store, job_id=job_id,
                            interval=heartbeat_interval, now=now,
                        ):
                            summary = summarize_one(candidate)
                        break
                    except Exception as error:
                        if not is_retryable_inference_failure(error):
                            raise
                        current = store.load(job_id)
                        retries = int(current.get("inference_retry_count", 0)) + 1
                        retry_started = current.get("inference_retry_started_at_epoch")
                        if retry_started is None:
                            retry_started = now()
                        elapsed = now() - float(retry_started)
                        delay = _retry_delay(retries)
                        if (
                            retries > max_inference_retries
                            or elapsed + delay > max_retry_elapsed_seconds
                        ):
                            raise JobError("inference retry limit exceeded") from error
                        retry_at = now() + delay
                        store.update(
                            job_id,
                            phase="waiting_for_inference",
                            runner_pid=os.getpid(),
                            runner_lease={
                                "owner_pid": os.getpid(),
                                "expires_at": retry_at + RUNNER_LEASE_SECONDS,
                            },
                            inference_retry_count=retries,
                            inference_retry_started_at_epoch=retry_started,
                            retry_at_epoch=retry_at,
                            error=_error_record(error),
                        )
                        sleep(delay)
                        current = store.load(job_id)
                        if current.get("cancel_requested"):
                            return store.update(
                                job_id, phase="cancelled", runner_pid=None,
                                runner_lease=None, retry_at_epoch=0, error=None,
                            )
                        store.update(
                            job_id,
                            phase="summarizing",
                            retry_at_epoch=0,
                            runner_lease={
                                "owner_pid": os.getpid(),
                                "expires_at": now() + RUNNER_LEASE_SECONDS,
                            },
                            error=None,
                        )
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
                return store.update(
                    job_id, phase="cancelled", runner_pid=None,
                    runner_lease=None, retry_at_epoch=0, error=None,
                )

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
                runner_lease=None,
                result=result,
                completion_event_id=_completion_event_id({**state, "phase": terminal_phase, "result": result}),
                error=None,
            )
        except JobCancelled:
            return store.update(
                job_id,
                phase="cancelled",
                runner_pid=None,
                runner_lease=None,
                error=None,
            )
        except Exception as error:
            store.update(
                job_id,
                phase="failed",
                runner_pid=None,
                runner_lease=None,
                error=_error_record(error),
            )
            raise


__all__ = [
    "ACTIVE_PHASES",
    "JobAlreadyRunning",
    "JobCancelled",
    "JobError",
    "JobStore",
    "RUNNER_LEASE_SECONDS",
    "MAX_INFERENCE_RETRIES",
    "MAX_INFERENCE_RETRY_ELAPSED_SECONDS",
    "RetryableInferenceError",
    "TERMINAL_PHASES",
    "cancel_job",
    "file_sha256",
    "read_regular_bytes",
    "write_json_file",
    "write_bytes_file",
    "prepare_job",
    "is_retryable_inference_failure",
    "validate_publication_manifest",
    "read_finalize_receipt",
    "run_job",
    "safe_error_message",
    "sweep_jobs",
    "utcnow",
    "write_finalize_receipt",
]
