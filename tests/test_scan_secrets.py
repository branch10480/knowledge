from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


SCANNER = Path(__file__).resolve().parents[1] / "scripts/scan-secrets.sh"


def _environment(tmp_path: Path) -> dict[str, str]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(exist_ok=True)
    gitleaks = binary_dir / "gitleaks"
    gitleaks.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gitleaks.chmod(0o700)
    return {**os.environ, "PATH": f"{binary_dir}:{os.environ['PATH']}"}


def _run(*arguments: str, cwd: Path, environment: dict[str, str]):
    return subprocess.run(
        [str(SCANNER), *arguments], cwd=cwd, env=environment,
        capture_output=True, text=True, check=False,
    )


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def test_path_scan_returns_digest_of_exact_snapshot(tmp_path: Path):
    environment = _environment(tmp_path)
    source = tmp_path / "safe.txt"
    payload = b"ordinary public text\n"
    source.write_bytes(payload)

    result = _run("--json", "--paths", str(source), cwd=tmp_path, environment=environment)

    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest == {
        "files": [{
            "label": str(source),
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }],
        "ok": True,
        "schema_version": 1,
    }


def test_staged_scan_uses_index_blobs_not_worktree_bytes(tmp_path: Path):
    environment = _environment(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    source = repo / "note.txt"
    safe = b"safe staged bytes\n"
    secret = b"sk-" + b"A" * 32 + b"\n"

    source.write_bytes(safe)
    _git(repo, "add", "note.txt")
    source.write_bytes(secret)
    staged_safe = _run("--json", "--staged", cwd=repo, environment=environment)
    assert staged_safe.returncode == 0, staged_safe.stderr
    assert json.loads(staged_safe.stdout)["files"][0]["sha256"] == (
        "sha256:" + hashlib.sha256(safe).hexdigest()
    )

    _git(repo, "add", "note.txt")
    source.write_bytes(safe)
    staged_secret = _run("--staged", cwd=repo, environment=environment)
    assert staged_secret.returncode == 1
    assert "secret scan FAILED" in staged_secret.stderr


def test_explicit_scanner_paths_override_path_lookup(tmp_path: Path):
    environment = _environment(tmp_path)
    source = tmp_path / "safe.txt"
    source.write_text("ordinary public text\n", encoding="utf-8")

    missing_gitleaks = {
        **environment,
        "GITLEAKS_BIN": str(tmp_path / "missing-gitleaks"),
    }
    gitleaks_result = _run(
        "--paths", str(source), cwd=tmp_path, environment=missing_gitleaks,
    )
    assert gitleaks_result.returncode == 1
    assert "required gitleaks scanner is unavailable" in gitleaks_result.stderr

    missing_grep = {
        **environment,
        "GREP_BIN": str(tmp_path / "missing-grep"),
    }
    grep_result = _run(
        "--paths", str(source), cwd=tmp_path, environment=missing_grep,
    )
    assert grep_result.returncode == 2
    assert "required grep scanner is unavailable" in grep_result.stderr
