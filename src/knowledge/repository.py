"""Transactional canonical publication with crash-safe reconciliation.

The public data files are replaced and committed under one repository-wide
lock.  Job publication records durable stages so a killed process can only
roll back to the recorded starting HEAD or push the exact recorded commit.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from . import validate
from .models import Checkpoint, EntriesDocument, Entry


CANONICAL_PATHS = ("data/entries.json", "data/checkpoint.json")
_PUBLICATION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_GIT = "/usr/bin/git"


def _trusted_gh_path() -> str:
    username = pwd.getpwuid(os.getuid()).pw_name
    candidate = Path(f"/etc/profiles/per-user/{username}/bin/gh")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise RepositoryError("trusted GitHub credential helper is unavailable") from error
    if (
        not str(resolved).startswith("/nix/store/")
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise RepositoryError("GitHub credential helper is not immutable")
    return str(resolved)


def _git_environment(**extra: str) -> dict[str, str]:
    environment = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
    }
    environment.update(extra)
    return environment


def _git_command(repo_root: Path, *args: str) -> list[str]:
    return [
        _GIT,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "credential.helper=",
        "-C",
        str(repo_root),
        *args,
    ]


class RepositoryError(Exception):
    pass


def _open_directory_without_symlinks(path: Path) -> int:
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
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise RepositoryError(f"unsafe durable directory: {absolute.name}")
        return descriptor
    except RepositoryError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise RepositoryError(f"unsafe durable directory: {absolute.name}") from error


def _read_regular_bytes(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    directory_fd = _open_directory_without_symlinks(absolute.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute.name, flags, dir_fd=directory_fd)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RepositoryError(f"unsafe durable file: {absolute.name}")
            return handle.read()
    except RepositoryError:
        raise
    except OSError as error:
        raise RepositoryError(f"could not read durable file: {absolute.name}") from error
    finally:
        os.close(directory_fd)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    absolute = Path(os.path.abspath(path))
    directory_fd = _open_directory_without_symlinks(absolute.parent)
    temporary_name = f".{absolute.name}.{os.getpid()}.{os.urandom(8).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            existing = os.stat(absolute.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise RepositoryError(f"unsafe durable destination: {absolute.name}")
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name, absolute.name,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, payload)


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(_read_regular_bytes(path))


def _git_optional(repo_root: Path, *args: str) -> tuple[int, str]:
    out = subprocess.run(
        _git_command(repo_root, *args), capture_output=True,
        text=True, timeout=60, check=False, env=_git_environment(),
    )
    return out.returncode, out.stdout.strip()


def _git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(
        _git_command(repo_root, *args), capture_output=True,
        text=True, timeout=60, check=False, env=_git_environment(),
    )
    if out.returncode != 0:
        raise RepositoryError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def _git_blob(repo_root: Path, revision_path: str) -> bytes:
    out = subprocess.run(
        _git_command(repo_root, "show", revision_path),
        capture_output=True, timeout=60, check=False, env=_git_environment(),
    )
    if out.returncode != 0:
        raise RepositoryError(f"could not read git blob: {revision_path}")
    return out.stdout


def _git_with_input(
    repo_root: Path,
    *args: str,
    payload: bytes,
    extra_env: Mapping[str, str] | None = None,
) -> str:
    environment = _git_environment(**dict(extra_env or {}))
    out = subprocess.run(
        _git_command(repo_root, *args),
        input=payload,
        capture_output=True,
        timeout=60,
        check=False,
        env=environment,
    )
    if out.returncode != 0:
        raise RepositoryError(f"git {' '.join(args)} failed")
    return out.stdout.decode("utf-8").strip()


def _create_exact_commit(
    *,
    prepared: "PreparedTransaction",
    starting_head: str,
    message: str,
) -> tuple[str, bytes]:
    """Create one commit object and its exact index bytes in private storage."""

    repo_root = prepared.repo_root
    index_root = repo_root / ".work/indexes"
    index_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="publication-", dir=index_root) as raw:
        index_path = Path(raw) / "index"
        index_env = {"GIT_INDEX_FILE": str(index_path)}
        _git_with_input(
            repo_root,
            "read-tree",
            starting_head,
            payload=b"",
            extra_env=index_env,
        )
        for relative, payload in (
            (CANONICAL_PATHS[0], prepared.data_bytes),
            (CANONICAL_PATHS[1], prepared.checkpoint_bytes),
        ):
            blob_oid = _git_with_input(
                repo_root,
                "hash-object",
                "-w",
                "--stdin",
                payload=payload,
                extra_env=index_env,
            )
            _git_with_input(
                repo_root,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob_oid,
                relative,
                payload=b"",
                extra_env=index_env,
            )
        tree_oid = _git_with_input(
            repo_root,
            "write-tree",
            payload=b"",
            extra_env=index_env,
        )
        identity_env = {
            "GIT_AUTHOR_NAME": "Knowledge Collector",
            "GIT_AUTHOR_EMAIL": "knowledge@localhost",
            "GIT_COMMITTER_NAME": "Knowledge Collector",
            "GIT_COMMITTER_EMAIL": "knowledge@localhost",
        }
        commit_oid = _git_with_input(
            repo_root,
            "commit-tree",
            tree_oid,
            "-p",
            starting_head,
            payload=(message + "\n").encode("utf-8"),
            extra_env=identity_env,
        )
        index_bytes = _read_regular_bytes(index_path)
    return commit_oid, index_bytes


def _git_index_paths(repo_root: Path, publication_id: str) -> tuple[Path, Path, Path]:
    git_dir = Path(_git(repo_root, "rev-parse", "--absolute-git-dir").strip())
    if not git_dir.is_absolute() or not git_dir.is_dir() or git_dir.is_symlink():
        raise RepositoryError("canonical Git directory is unsafe")
    claim_name = f"knowledge-index-{_validate_publication_id(publication_id)}.claim"
    return git_dir / "index", git_dir / "index.lock", git_dir / claim_name


def _read_linked_regular_bytes(path: Path) -> bytes:
    """Read a regular file whose link count may exceed one during promotion."""

    absolute = Path(os.path.abspath(path))
    directory_fd = _open_directory_without_symlinks(absolute.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute.name, flags, dir_fd=directory_fd)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise RepositoryError("unsafe Git index artifact")
            return handle.read()
    except RepositoryError:
        raise
    except OSError as error:
        raise RepositoryError("could not read Git index artifact") from error
    finally:
        os.close(directory_fd)


def _linked_digest(path: Path) -> str:
    return _sha256_bytes(_read_linked_regular_bytes(path))


def _same_inode(left: Path, right: Path) -> bool:
    try:
        left_stat = os.stat(left, follow_symlinks=False)
        right_stat = os.stat(right, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(left_stat.st_mode)
        and stat.S_ISREG(right_stat.st_mode)
        and left_stat.st_dev == right_stat.st_dev
        and left_stat.st_ino == right_stat.st_ino
    )


def _write_index_claim(path: Path, payload: bytes) -> None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_fd)
    except FileExistsError as error:
        raise RepositoryError("stale publication index claim requires recovery") from error
    finally:
        os.close(directory_fd)


def _unlink_regular(path: Path) -> None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    try:
        metadata = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise RepositoryError("unsafe Git index artifact")
        os.unlink(path.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(directory_fd)


def _cleanup_owned_index_artifacts(repo_root: Path, record: Mapping[str, object]) -> None:
    publication_id = str(record.get("publication_id", ""))
    _index, lock, claim = _git_index_paths(repo_root, publication_id)
    expected = record.get("publication_index_sha256")
    if claim.exists() or claim.is_symlink():
        if _linked_digest(claim) != expected:
            raise RepositoryError("publication index claim digest mismatch")
        if _same_inode(claim, lock):
            _unlink_regular(lock)
        _unlink_regular(claim)


def _acquire_owned_index_lock(
    repo_root: Path, record: Mapping[str, object], publication_index: bytes,
) -> None:
    publication_id = str(record.get("publication_id", ""))
    index, lock, claim = _git_index_paths(repo_root, publication_id)
    _write_index_claim(claim, publication_index)
    directory_fd = _open_directory_without_symlinks(lock.parent)
    try:
        try:
            os.link(
                claim.name, lock.name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        except FileExistsError as error:
            _unlink_regular(claim)
            raise RepositoryError("Git index is busy") from error
    finally:
        os.close(directory_fd)
    if not _same_inode(claim, lock):
        raise RepositoryError("publication did not acquire the exact Git index lock")
    if _linked_digest(index) != record.get("original_index_sha256"):
        _cleanup_owned_index_artifacts(repo_root, record)
        raise RepositoryError("Git index changed before publication lock acquisition")


def _promote_owned_index(repo_root: Path, record: Mapping[str, object]) -> None:
    publication_id = str(record.get("publication_id", ""))
    index, lock, claim = _git_index_paths(repo_root, publication_id)
    expected = record.get("publication_index_sha256")
    if not (claim.exists() or claim.is_symlink()):
        raise RepositoryError("publication index ownership claim is missing")
    if _linked_digest(claim) != expected:
        raise RepositoryError("publication index claim digest mismatch")
    if _same_inode(claim, lock):
        if _linked_digest(index) != record.get("original_index_sha256"):
            raise RepositoryError("Git index changed while publication lock was held")
        directory_fd = _open_directory_without_symlinks(index.parent)
        try:
            os.replace(
                lock.name, index.name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    elif _same_inode(claim, index):
        return
    else:
        # Once index.lock has been promoted, a subsequent ordinary git add may
        # atomically replace index. The surviving one-link claim still proves
        # the exact publication bytes. If the canonical index still has its
        # original digest, however, the lock may merely have been unlinked
        # before promotion. Re-acquire the standard lock and finish safely.
        metadata = os.stat(claim, follow_symlinks=False)
        if metadata.st_nlink != 1:
            raise RepositoryError("Git index promotion ownership is ambiguous")
        if _linked_digest(index) == record.get("original_index_sha256"):
            directory_fd = _open_directory_without_symlinks(index.parent)
            try:
                try:
                    os.link(
                        claim.name, lock.name,
                        src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    os.fsync(directory_fd)
                except FileExistsError as error:
                    raise RepositoryError("Git index became busy during recovery") from error
                if _linked_digest(index) != record.get("original_index_sha256"):
                    _unlink_regular(lock)
                    raise RepositoryError("Git index changed during publication recovery")
                os.replace(
                    lock.name, index.name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


def _open_lock_file(path: Path):
    absolute = Path(os.path.abspath(path))
    directory_fd = _open_directory_without_symlinks(absolute.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute.name, flags, 0o600, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise RepositoryError("unsafe finalize lock")
        return os.fdopen(descriptor, "a+b")
    except RepositoryError:
        raise
    except OSError as error:
        raise RepositoryError("could not open finalize lock") from error
    finally:
        os.close(directory_fd)


@contextmanager
def finalize_lock(repo_root: Path) -> Iterator[None]:
    work = repo_root / ".work"
    if work.is_symlink():
        raise RepositoryError("unsafe publication work directory")
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _open_lock_file(work / "finalize.lock") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _journal_path(repo_root: Path) -> Path:
    return repo_root / ".work" / "finalize-journal.json"


def _read_journal(repo_root: Path) -> dict[str, object] | None:
    path = _journal_path(repo_root)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RepositoryError("finalize journal is unreadable; manual recovery required") from error
    if not isinstance(value, dict):
        raise RepositoryError("finalize journal is invalid")
    return value


def _write_journal(repo_root: Path, record: Mapping[str, object]) -> None:
    _atomic_json(_journal_path(repo_root), record)


def _validate_publication_id(publication_id: str) -> str:
    if not isinstance(publication_id, str) or not _PUBLICATION_ID.fullmatch(publication_id):
        raise RepositoryError("invalid publication id")
    return publication_id


def _consume_bound_capability(capability: str, authority_binding: str) -> None:
    """Consume a generic Hermes-core capability under the finalize lock."""

    try:
        from agent.direct_user_authority import consume_bound_capability
    except (ImportError, AttributeError) as error:
        raise RepositoryError("core publication authority is unavailable") from error
    if not consume_bound_capability(capability, authority_binding):
        raise RepositoryError("publication capability is missing, stale, or mismatched")


def _publication_dir(repo_root: Path, publication_id: str) -> Path:
    publication_id = _validate_publication_id(publication_id)
    root = repo_root / ".work" / "publications"
    if root.is_symlink():
        raise RepositoryError("unsafe publication backup directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / publication_id
    if path.is_symlink():
        raise RepositoryError("unsafe publication backup directory")
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _backup_paths(repo_root: Path, publication_id: str) -> dict[str, Path]:
    root = _publication_dir(repo_root, publication_id)
    return {
        "data/entries.json": root / "entries.before.json",
        "data/checkpoint.json": root / "checkpoint.before.json",
    }


def _output_digest_map(prepared: "PreparedTransaction") -> dict[str, str]:
    return {
        "data/entries.json": _sha256_bytes(prepared.data_bytes),
        "data/checkpoint.json": _sha256_bytes(prepared.checkpoint_bytes),
    }


@contextmanager
def _isolated_remote_git(repo_root: Path) -> Iterator[Path]:
    """Create a config-free bare Git view over the canonical object store."""

    work = repo_root / ".work"
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="remote-git-", dir=work) as raw:
        git_dir = Path(raw) / "repo.git"
        initialized = subprocess.run(
            [_GIT, "init", "--bare", str(git_dir)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_git_environment(),
        )
        if initialized.returncode != 0:
            raise RepositoryError("could not initialize isolated publication Git dir")
        object_store = repo_root / ".git/objects"
        if object_store.is_symlink() or not object_store.is_dir():
            raise RepositoryError("canonical Git object store is unsafe")
        alternates = git_dir / "objects/info/alternates"
        _atomic_write_bytes(alternates, (str(object_store.resolve()) + "\n").encode("utf-8"))
        yield git_dir


def _remote_git(repo_root: Path, git_dir: Path, *args: str) -> str:
    credential_helper = f"!{_trusted_gh_path()} auth git-credential"
    out = subprocess.run(
        [
            _GIT,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={credential_helper}",
            f"--git-dir={git_dir}",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=_git_environment(),
    )
    if out.returncode != 0:
        raise RepositoryError(f"isolated git {args[0]} failed: {out.stderr.strip()}")
    return out.stdout


def _remote_oid(repo_root: Path, remote_url: str, branch: str) -> str:
    with _isolated_remote_git(repo_root) as git_dir:
        line = _remote_git(
            repo_root,
            git_dir,
            "ls-remote",
            "--heads",
            remote_url,
            f"refs/heads/{branch}",
        ).strip()
    return line.split()[0] if line else ""


def _push_exact(
    repo_root: Path, *, remote_url: str, branch: str, commit_oid: str,
) -> None:
    with _isolated_remote_git(repo_root) as git_dir:
        _remote_git(
            repo_root,
            git_dir,
            "push",
            "--no-verify",
            remote_url,
            f"{commit_oid}:refs/heads/{branch}",
        )


def _sync_remote_tracking_ref(
    repo_root: Path,
    *,
    remote: str,
    branch: str,
    expected_old_oid: str,
    verified_new_oid: str,
) -> bool:
    """CAS the local remote-tracking ref after the remote OID is verified.

    The isolated push deliberately does not inherit or mutate normal repository
    config.  Keep ordinary ``git status`` truthful without overwriting a
    concurrent fetch: only the exact previously observed tracking OID may move.
    A concurrent tracking update is harmless and is left untouched.
    """

    if re.fullmatch(r"[A-Za-z0-9._-]+", remote) is None or re.fullmatch(
        r"[A-Za-z0-9._/-]+", branch
    ) is None:
        raise RepositoryError("invalid remote-tracking ref identity")
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    status, current = _git_optional(repo_root, "rev-parse", "--verify", tracking_ref)
    if status != 0:
        return False
    if current == verified_new_oid:
        return True
    if current != expected_old_oid:
        return False
    status, _ = _git_optional(
        repo_root,
        "update-ref",
        tracking_ref,
        verified_new_oid,
        expected_old_oid,
    )
    if status == 0:
        return True
    verify_status, observed = _git_optional(
        repo_root, "rev-parse", "--verify", tracking_ref,
    )
    return verify_status == 0 and observed == verified_new_oid


def _assert_empty_index(repo_root: Path) -> None:
    status, _ = _git_optional(repo_root, "diff", "--cached", "--quiet", "--")
    if status != 0:
        raise RepositoryError("publication requires a normal empty index")


def _snapshot_empty_index(repo_root: Path) -> str:
    """Return the digest of one exact, independently verified empty index.

    Git replaces the index atomically. Reading its bytes first and validating
    that private snapshot closes the check-then-read window where a concurrent
    ``git add`` could otherwise become the publication baseline.
    """

    git_dir = Path(_git(repo_root, "rev-parse", "--absolute-git-dir").strip())
    if not git_dir.is_absolute() or not git_dir.is_dir() or git_dir.is_symlink():
        raise RepositoryError("canonical Git directory is unsafe")
    payload = _read_linked_regular_bytes(git_dir / "index")
    index_root = repo_root / ".work/indexes"
    index_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="baseline-", dir=index_root) as raw:
        snapshot = Path(raw) / "index"
        _atomic_write_bytes(snapshot, payload)
        out = subprocess.run(
            _git_command(repo_root, "diff", "--cached", "--quiet", "--"),
            capture_output=True,
            timeout=60,
            check=False,
            env=_git_environment(GIT_INDEX_FILE=str(snapshot)),
        )
        if out.returncode != 0:
            raise RepositoryError("publication requires a normal empty index")
    return _sha256_bytes(payload)


def _assert_publication_environment(
    *, repo_root: Path, starting_head: str, remote: str, branch: str,
    expected_remote_url: str, expected_upstream_oid: str,
) -> str:
    if _git(repo_root, "rev-parse", "HEAD").strip() != starting_head:
        raise RepositoryError("starting HEAD changed before publication")
    if _git(repo_root, "symbolic-ref", "--short", "HEAD").strip() != branch:
        raise RepositoryError("publication requires expected branch")
    if _git(repo_root, "remote", "get-url", remote).strip() != expected_remote_url:
        raise RepositoryError("publication remote URL changed")
    push_urls = {
        value
        for value in _git(repo_root, "remote", "get-url", "--push", "--all", remote).splitlines()
        if value
    }
    if push_urls != {expected_remote_url}:
        raise RepositoryError("publication push URL changed")
    if _remote_oid(repo_root, expected_remote_url, branch) != expected_upstream_oid:
        raise RepositoryError("publication upstream OID changed")
    if starting_head != expected_upstream_oid:
        raise RepositoryError("publication requires local HEAD to equal upstream OID")
    baseline_index_sha256 = _snapshot_empty_index(repo_root)
    status, _ = _git_optional(repo_root, "diff", "--quiet", "--", *CANONICAL_PATHS)
    if status != 0:
        raise RepositoryError("canonical files are not clean")
    return baseline_index_sha256


def _validate_index_for_rollback(repo_root: Path, output_digests: Mapping[str, object]) -> None:
    names = {
        line for line in _git(repo_root, "diff", "--cached", "--name-only", "--").splitlines()
        if line
    }
    if not names.issubset(set(CANONICAL_PATHS)):
        raise RepositoryError("index changed outside canonical publication paths")
    for name in names:
        if _sha256_bytes(_git_blob(repo_root, f":{name}")) != output_digests.get(name):
            raise RepositoryError("index does not match journaled publication output")


def _rollback_locked(repo_root: Path, record: Mapping[str, object]) -> dict[str, object]:
    publication_id = str(record.get("publication_id", ""))
    starting_head = str(record.get("starting_head", ""))
    if _git(repo_root, "rev-parse", "HEAD").strip() != starting_head:
        raise RepositoryError("cannot roll back publication after HEAD changed")
    output_digests = record.get("output_digests")
    backup_digests = record.get("backup_digests")
    if not isinstance(output_digests, dict) or not isinstance(backup_digests, dict):
        raise RepositoryError("journal backup contract is invalid")
    owned_index_protocol = record.get("index_protocol") == "owned-lock-v1"
    if owned_index_protocol:
        _cleanup_owned_index_artifacts(repo_root, record)
    else:
        _validate_index_for_rollback(repo_root, output_digests)
    backups = _backup_paths(repo_root, publication_id)
    for relative in CANONICAL_PATHS:
        backup = backups[relative]
        if _sha256(backup) != backup_digests.get(relative):
            raise RepositoryError("publication backup digest mismatch")
    current_digests = {
        relative: _sha256(repo_root / relative)
        for relative in CANONICAL_PATHS
    }
    for relative, current_digest in current_digests.items():
        if current_digest not in {
            output_digests.get(relative), backup_digests.get(relative),
        }:
            raise RepositoryError(
                "canonical file changed after publication crash; preserved for manual recovery"
            )
    if not owned_index_protocol:
        _git(repo_root, "reset", "--", *CANONICAL_PATHS)
    for relative in CANONICAL_PATHS:
        # A file already matching the backup is left untouched. Re-check files
        # immediately before replacement so recovery never knowingly overwrites
        # bytes that appeared after the durable journal was written.
        current_digest = _sha256(repo_root / relative)
        if current_digest == backup_digests.get(relative):
            continue
        if current_digest != output_digests.get(relative):
            raise RepositoryError(
                "canonical file changed during publication recovery; preserved"
            )
        _atomic_write_bytes(repo_root / relative, _read_regular_bytes(backups[relative]))
    if not owned_index_protocol:
        _assert_empty_index(repo_root)
        diff_args = ("diff", "--quiet", "--", *CANONICAL_PATHS)
    else:
        diff_args = ("diff", "--quiet", "HEAD", "--", *CANONICAL_PATHS)
    status, _ = _git_optional(repo_root, *diff_args)
    if status != 0:
        raise RepositoryError("publication rollback did not restore starting HEAD")
    rolled_back = {**record, "stage": "rolled_back"}
    _write_journal(repo_root, rolled_back)
    return rolled_back


def _validate_committed_record(
    repo_root: Path,
    record: Mapping[str, object],
    *,
    require_active_head: bool = True,
) -> str:
    publication_id = str(record.get("publication_id", ""))
    starting_head = str(record.get("starting_head", ""))
    commit_oid = str(record.get("commit_oid", ""))
    output_digests = record.get("output_digests")
    if not all((publication_id, starting_head, commit_oid)) or not isinstance(output_digests, dict):
        raise RepositoryError("committed journal identity is invalid")
    if _git(repo_root, "rev-parse", f"{commit_oid}^").strip() != starting_head:
        raise RepositoryError("journal commit parent does not match starting HEAD")
    message_lines = _git(repo_root, "log", "-1", "--format=%B", commit_oid).splitlines()
    if f"Knowledge-Job: {publication_id}" not in message_lines:
        raise RepositoryError("journal commit does not contain the exact job trailer")
    changed = {
        line for line in _git(
            repo_root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit_oid
        ).splitlines() if line
    }
    if not changed.issubset(set(CANONICAL_PATHS)):
        raise RepositoryError("journal commit changed files outside canonical outputs")
    for relative in CANONICAL_PATHS:
        if _sha256_bytes(_git_blob(repo_root, f"{commit_oid}:{relative}")) != output_digests.get(relative):
            raise RepositoryError("journal commit output digest mismatch")
    tree_oid = _git(repo_root, "rev-parse", f"{commit_oid}^{{tree}}").strip()
    recorded_tree = record.get("tree_oid")
    if recorded_tree is not None and tree_oid != recorded_tree:
        raise RepositoryError("journal commit tree mismatch")
    if require_active_head:
        head = _git(repo_root, "rev-parse", "HEAD").strip()
        if head not in {starting_head, commit_oid}:
            raise RepositoryError("repository HEAD diverged from journaled publication")
    return commit_oid


def _reconcile_finalize_journal_locked(
    *, repo_root: Path, remote: str, branch: str,
) -> dict[str, object] | None:
    record = _read_journal(repo_root)
    if record is None:
        return None
    stage = record.get("stage")
    if stage == "rolled_back":
        return record
    pre_promotion_stages = {
        "canonical_replacing", "canonical_replaced", "commit_started",
        "commit_ready", "index_lock_intent", "index_locked",
    }
    if stage in pre_promotion_stages:
        starting_head = str(record.get("starting_head", ""))
        commit_oid = str(record.get("commit_oid", ""))
        head = _git(repo_root, "rev-parse", "HEAD").strip()
        if head == starting_head:
            return _rollback_locked(repo_root, record)
        if (
            record.get("index_protocol") != "owned-lock-v1"
            or not commit_oid
            or head != commit_oid
        ):
            raise RepositoryError("repository HEAD diverged during commit promotion")
        _validate_committed_record(repo_root, record, require_active_head=False)
        _promote_owned_index(repo_root, record)
        promoted = {
            **record,
            "stage": "index_promoted",
        }
        _write_journal(repo_root, promoted)
        _cleanup_owned_index_artifacts(repo_root, promoted)
        promoted = {**promoted, "stage": "committed"}
        _validate_committed_record(repo_root, promoted)
        _write_journal(repo_root, promoted)
        record = promoted
        stage = "committed"
    if stage == "head_promoted":
        commit_oid = _validate_committed_record(
            repo_root, record, require_active_head=False,
        )
        if _git(repo_root, "rev-parse", "HEAD").strip() != commit_oid:
            raise RepositoryError("repository HEAD diverged during index promotion")
        _promote_owned_index(repo_root, record)
        record = {**record, "stage": "index_promoted"}
        _write_journal(repo_root, record)
        stage = "index_promoted"
    if stage == "index_promoted":
        commit_oid = _validate_committed_record(
            repo_root, record, require_active_head=False,
        )
        if _git(repo_root, "rev-parse", "HEAD").strip() != commit_oid:
            raise RepositoryError("repository HEAD diverged after index promotion")
        _cleanup_owned_index_artifacts(repo_root, record)
        record = {**record, "stage": "committed"}
        _validate_committed_record(repo_root, record)
        _write_journal(repo_root, record)
        stage = "committed"
    if stage not in {"committed", "push_verified", "closed"}:
        raise RepositoryError("unknown finalize journal stage")
    expected_url = record.get("expected_remote_url")
    expected_upstream = record.get("expected_upstream_oid")
    if _git(repo_root, "remote", "get-url", remote).strip() != expected_url:
        raise RepositoryError("journal publication remote URL changed")
    if stage == "closed":
        # A closed journal is immutable history. Later ordinary commits are
        # expected, so validate its recorded commit without constraining the
        # current HEAD or index to that old publication.
        _validate_committed_record(repo_root, record, require_active_head=False)
        return record
    commit_oid = _validate_committed_record(repo_root, record)
    remote_oid = _remote_oid(repo_root, str(expected_url), branch)
    if stage == "push_verified":
        if remote_oid != commit_oid:
            raise RepositoryError("verified publication remote OID changed")
        return record
    if remote_oid not in {expected_upstream, commit_oid}:
        raise RepositoryError("remote advanced; journal reconcile stopped safely")
    if remote_oid != commit_oid:
        _push_exact(
            repo_root,
            remote_url=str(expected_url),
            branch=branch,
            commit_oid=commit_oid,
        )
    if _remote_oid(repo_root, str(expected_url), branch) != commit_oid:
        raise RepositoryError("push verification did not match committed OID")
    tracking_ref_synced = _sync_remote_tracking_ref(
        repo_root,
        remote=remote,
        branch=branch,
        expected_old_oid=str(expected_upstream),
        verified_new_oid=commit_oid,
    )
    verified = {
        **record,
        "stage": "push_verified",
        "remote_oid": commit_oid,
        "tracking_ref_synced": tracking_ref_synced,
    }
    _write_journal(repo_root, verified)
    return verified


def merge_entries(existing: EntriesDocument, additions: Sequence[Entry]) -> EntriesDocument:
    known_ids = {e.id for e in existing.entries}
    known_ext = {(e.source_id, e.external_id) for e in existing.entries}
    known_url = {e.canonical_url for e in existing.entries}
    new: list[Entry] = []
    for entry in additions:
        if (
            entry.id in known_ids
            or (entry.source_id, entry.external_id) in known_ext
            or entry.canonical_url in known_url
        ):
            continue
        known_ids.add(entry.id)
        known_ext.add((entry.source_id, entry.external_id))
        known_url.add(entry.canonical_url)
        new.append(entry)
    all_entries = list(existing.entries) + new
    all_entries.sort(key=lambda value: (value.published_at, value.id), reverse=True)
    return EntriesDocument(existing.schema_version, tuple(all_entries))


@dataclass(frozen=True)
class PreparedTransaction:
    data_path: Path
    checkpoint_path: Path
    repo_root: Path
    data_bytes: bytes
    checkpoint_bytes: bytes


def _write_canonical(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(path, payload)


def prepare_transaction(
    *, repo_root: Path, merged: EntriesDocument,
    checkpoint: Checkpoint, transaction_dir: Path,
) -> PreparedTransaction:
    if transaction_dir.is_symlink():
        raise RepositoryError("unsafe transaction directory")
    transaction_dir.mkdir(parents=True, exist_ok=True)
    data_path = transaction_dir / "entries.json"
    checkpoint_path = transaction_dir / "checkpoint.json"
    _write_canonical(data_path, merged.to_json())
    _write_canonical(checkpoint_path, checkpoint.to_json())

    data_bytes = _read_regular_bytes(data_path)
    checkpoint_bytes = _read_regular_bytes(checkpoint_path)
    raw = json.loads(data_bytes.decode("utf-8"))
    validate.validate_entries_document(raw).raise_if_bad()
    checkpoint_raw = json.loads(checkpoint_bytes.decode("utf-8"))
    validate.validate_checkpoint(checkpoint_raw).raise_if_bad()
    expected_data = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    expected_checkpoint = (
        json.dumps(checkpoint_raw, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if data_bytes != expected_data or checkpoint_bytes != expected_checkpoint:
        raise RepositoryError("non-deterministic serialize")
    return PreparedTransaction(
        data_path=data_path,
        checkpoint_path=checkpoint_path,
        repo_root=repo_root,
        data_bytes=data_bytes,
        checkpoint_bytes=checkpoint_bytes,
    )


def commit_transaction(
    prepared: PreparedTransaction, *, message: str | None = None,
    job_id: str, starting_head: str,
    expected_output_digests: dict[str, str],
    capability: str,
    authority_binding: str,
    publication_binding: str,
    remote: str = "origin", branch: str = "main",
    expected_remote_url: str,
    expected_upstream_oid: str,
    on_push_verified: Callable[[str], None] | None = None,
) -> str:
    """Replace, commit, push and verify while holding the finalize lock."""
    repo_root = prepared.repo_root
    output_digests = _output_digest_map(prepared)
    with finalize_lock(repo_root):
        publication_id = _validate_publication_id(job_id)
        if not all((
            starting_head,
            expected_output_digests,
            expected_remote_url,
            expected_upstream_oid,
            capability,
            authority_binding,
            publication_binding,
        )):
            raise RepositoryError("job publication requires exact repository bindings")
        publication_binding_sha256 = _sha256_bytes(publication_binding.encode("utf-8"))
        expected_record_outputs = {
            "data/entries.json": expected_output_digests.get("entries_sha256"),
            "data/checkpoint.json": expected_output_digests.get("checkpoint_sha256"),
        }
        existing_journal = _read_journal(repo_root)
        if existing_journal and (
            existing_journal.get("stage") not in {"rolled_back", "closed"}
            or existing_journal.get("publication_id") == publication_id
        ):
            # Validate the journal before consuming authority or reconciling any
            # side effect. A capability for job B must never push job A.
            if (
                existing_journal.get("publication_id") != publication_id
                or existing_journal.get("publication_binding_sha256")
                != publication_binding_sha256
                or existing_journal.get("starting_head") != starting_head
                or existing_journal.get("expected_remote_url") != expected_remote_url
                or existing_journal.get("expected_upstream_oid") != expected_upstream_oid
                or existing_journal.get("output_digests") != expected_record_outputs
            ):
                raise RepositoryError(
                    "unfinished publication journal does not match this exact capability"
                )
        _consume_bound_capability(capability, authority_binding)
        reconciled = _reconcile_finalize_journal_locked(
            repo_root=repo_root, remote=remote, branch=branch,
        )
        if (
            reconciled
            and reconciled.get("publication_id") == publication_id
            and reconciled.get("stage") in {"push_verified", "closed"}
        ):
            commit_oid = str(reconciled["commit_oid"])
            if reconciled.get("stage") == "push_verified":
                if on_push_verified is not None:
                    on_push_verified(commit_oid)
                reconciled = {**reconciled, "stage": "closed"}
                _write_journal(repo_root, reconciled)
            return commit_oid
        if reconciled and reconciled.get("stage") == "push_verified":
            raise RepositoryError(
                "another pushed publication requires job completion before replacement"
            )

        record: dict[str, object] | None = None
        committed = False
        try:
            baseline_index_sha256 = _assert_publication_environment(
                repo_root=repo_root, starting_head=str(starting_head),
                remote=remote, branch=branch,
                expected_remote_url=str(expected_remote_url),
                expected_upstream_oid=str(expected_upstream_oid),
            )
            manifest_expected = {
                "entries_sha256": output_digests["data/entries.json"],
                "checkpoint_sha256": output_digests["data/checkpoint.json"],
            }
            if expected_output_digests != manifest_expected:
                raise RepositoryError("prepared output digests do not match publication manifest")
            backups = _backup_paths(repo_root, publication_id)
            backup_digests: dict[str, str] = {}
            for relative in CANONICAL_PATHS:
                payload = _read_regular_bytes(repo_root / relative)
                _atomic_write_bytes(backups[relative], payload)
                backup_digests[relative] = _sha256_bytes(payload)
            record = {
                "version": 1,
                "publication_id": publication_id,
                "starting_head": starting_head,
                "stage": "canonical_replacing",
                "index_protocol": "owned-lock-v1",
                "original_index_sha256": baseline_index_sha256,
                "output_digests": output_digests,
                "backup_digests": backup_digests,
                "remote": remote,
                "branch": branch,
                "expected_remote_url": expected_remote_url,
                "expected_upstream_oid": expected_upstream_oid,
                "publication_binding_sha256": publication_binding_sha256,
            }
            _write_journal(repo_root, record)

            _atomic_write_bytes(repo_root / CANONICAL_PATHS[0], prepared.data_bytes)
            _atomic_write_bytes(repo_root / CANONICAL_PATHS[1], prepared.checkpoint_bytes)
            record = {**record, "stage": "canonical_replaced"}
            _write_journal(repo_root, record)
            commit_message = message or "knowledge: 収集結果とcheckpointを更新"
            if f"Knowledge-Job: {publication_id}" not in commit_message.splitlines():
                commit_message += f"\n\nKnowledge-Job: {publication_id}"
            record = {**record, "stage": "commit_started"}
            _write_journal(repo_root, record)
            commit_oid, _publication_index = _create_exact_commit(
                prepared=prepared,
                starting_head=starting_head,
                message=commit_message,
            )
            index, _index_lock, claim = _git_index_paths(repo_root, publication_id)
            record = {
                **record,
                "stage": "commit_ready",
                "commit_oid": commit_oid,
                "tree_oid": _git(repo_root, "rev-parse", f"{commit_oid}^{{tree}}").strip(),
                "original_index_sha256": baseline_index_sha256,
                "publication_index_sha256": _sha256_bytes(_publication_index),
                "index_claim_name": claim.name,
            }
            _write_journal(repo_root, record)
            record = {**record, "stage": "index_lock_intent"}
            _write_journal(repo_root, record)
            _acquire_owned_index_lock(repo_root, record, _publication_index)
            record = {**record, "stage": "index_locked"}
            _write_journal(repo_root, record)
            _git(repo_root, "update-ref", f"refs/heads/{branch}", commit_oid, starting_head)
            committed = True
            record = {**record, "stage": "head_promoted"}
            _write_journal(repo_root, record)
            _promote_owned_index(repo_root, record)
            record = {**record, "stage": "index_promoted"}
            _write_journal(repo_root, record)
            _cleanup_owned_index_artifacts(repo_root, record)
            record = {**record, "stage": "committed"}
            _validate_committed_record(repo_root, record)
            _write_journal(repo_root, record)
        except Exception as error:
            if not committed:
                current = _read_journal(repo_root)
                if current and current.get("stage") in {
                    "canonical_replacing", "canonical_replaced", "commit_started",
                    "commit_ready", "index_lock_intent", "index_locked",
                }:
                    head = _git(repo_root, "rev-parse", "HEAD").strip()
                    if head == starting_head:
                        _rollback_locked(repo_root, current)
            if record is None and isinstance(error, RepositoryError):
                raise
            raise RepositoryError("transaction failed before commit; rolled back when safe") from error

        _push_exact(
            repo_root,
            remote_url=expected_remote_url,
            branch=branch,
            commit_oid=commit_oid,
        )
        if _remote_oid(repo_root, expected_remote_url, branch) != commit_oid:
            raise RepositoryError("push verification did not match committed OID")
        tracking_ref_synced = _sync_remote_tracking_ref(
            repo_root,
            remote=remote,
            branch=branch,
            expected_old_oid=expected_upstream_oid,
            verified_new_oid=commit_oid,
        )
        record = {
            **record,
            "stage": "push_verified",
            "remote_oid": commit_oid,
            "tracking_ref_synced": tracking_ref_synced,
        }
        _write_journal(repo_root, record)
        if on_push_verified is not None:
            on_push_verified(commit_oid)
        record = {**record, "stage": "closed"}
        _write_journal(repo_root, record)
        return commit_oid
