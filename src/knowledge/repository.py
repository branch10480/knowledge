"""トランザクショナルな entries/checkpoint のマージと commit。

- entries と checkpoint を同一一時 directory に書き、両方を検証してから os.replace
- git commit は必ず同一。commit 対象を固定し、git add -A は使わない
- push は git push origin HEAD:main と明示。競合時は force push せず失敗
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import validate
from .models import Checkpoint, EntriesDocument, Entry


class RepositoryError(Exception):
    pass


def merge_entries(existing: EntriesDocument, additions: Sequence[Entry]) -> EntriesDocument:
    known_ids = {e.id for e in existing.entries}
    known_ext = {(e.source_id, e.external_id) for e in existing.entries}
    known_url = {e.canonical_url for e in existing.entries}
    new: list[Entry] = []
    for e in additions:
        if e.id in known_ids or (e.source_id, e.external_id) in known_ext or e.canonical_url in known_url:
            continue
        known_ids.add(e.id)
        known_ext.add((e.source_id, e.external_id))
        known_url.add(e.canonical_url)
        new.append(e)
    all_entries = list(existing.entries) + new
    all_entries.sort(key=lambda x: (x.published_at, x.id), reverse=True)
    return EntriesDocument(existing.schema_version, tuple(all_entries))


@dataclass(frozen=True)
class PreparedTransaction:
    data_path: Path
    checkpoint_path: Path
    repo_root: Path


def _write_canonical(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def prepare_transaction(
    *, repo_root: Path, merged: EntriesDocument,
    checkpoint: Checkpoint, transaction_dir: Path,
) -> PreparedTransaction:
    transaction_dir.mkdir(parents=True, exist_ok=True)
    data_path = transaction_dir / "entries.json"
    cp_path = transaction_dir / "checkpoint.json"
    _write_canonical(data_path, merged.to_json())
    _write_canonical(cp_path, checkpoint.to_json())

    # schema 検証と再読込一致
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    rep = validate.validate_entries_document(raw)
    rep.raise_if_bad()
    cp_raw = json.loads(cp_path.read_text(encoding="utf-8"))
    validate.validate_checkpoint(cp_raw).raise_if_bad()

    # 再読込一致（決定性）
    again = json.loads(data_path.read_text(encoding="utf-8"))
    if raw != again:
        raise RepositoryError("non-deterministic serialize")

    return PreparedTransaction(data_path=data_path, checkpoint_path=cp_path, repo_root=repo_root)


def commit_transaction(
    prepared: PreparedTransaction, *, message: str | None = None
) -> str:
    """entries/checkpoint を data/ へ原子的に置換し、同一 commit で main へ push。"""
    data_final = prepared.repo_root / "data" / "entries.json"
    cp_final = prepared.repo_root / "data" / "checkpoint.json"

    # 既存を backup
    backup_data = data_final.with_suffix(".bak")
    backup_cp = cp_final.with_suffix(".bak")
    if data_final.exists():
        shutil.copyfile(data_final, backup_data)
    if cp_final.exists():
        shutil.copyfile(cp_final, backup_cp)

    committed = False
    try:
        shutil.copyfile(prepared.data_path, data_final)
        shutil.copyfile(prepared.checkpoint_path, cp_final)
        # git add（対象固定）
        _git(prepared.repo_root, "add", "--", "data/entries.json", "data/checkpoint.json")
        msg = message or "knowledge: 収集結果とcheckpointを更新"
        _git(prepared.repo_root, "commit", "-m", msg)
        committed = True
    except Exception as error:
        # commit 前の失敗は worktree と index を開始時へ戻す。commit 後の
        # push 失敗は下で扱い、同じ OID を再送できるよう local commit を残す。
        if not committed:
            if backup_data.exists():
                shutil.copyfile(backup_data, data_final)
            if backup_cp.exists():
                shutil.copyfile(backup_cp, cp_final)
            try:
                _git(
                    prepared.repo_root,
                    "reset",
                    "--",
                    "data/entries.json",
                    "data/checkpoint.json",
                )
            except RepositoryError:
                pass
        raise RepositoryError("transaction failed before commit; rolled back") from error

    # push origin HEAD:main。失敗時は local commit と backup を残し、durable
    # job reconciler が同じ OID を再送する。新しい commit は作らない。
    _git(prepared.repo_root, "push", "origin", "HEAD:main")
    commit = _git(prepared.repo_root, "rev-parse", "HEAD").strip()
    for backup in (backup_data, backup_cp):
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
    return commit


def _git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RepositoryError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout
