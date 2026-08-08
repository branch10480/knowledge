"""Contract tests for the unattended cron entrypoint."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "scripts" / "cron-collect.sh"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    origin.mkdir()
    repo.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=origin)
    _git("init", "--initial-branch=main", cwd=repo)
    _git("config", "user.name", "Knowledge Test", cwd=repo)
    _git("config", "user.email", "knowledge-test@example.invalid", cwd=repo)

    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy2(ENTRYPOINT, scripts / "cron-collect.sh")
    (scripts / "collect.sh").write_text(
        """#!/usr/bin/env bash
set -u
attempt=0
if [ -f "$KNOWLEDGE_TEST_STATE" ]; then
  attempt=$(sed -n '1p' "$KNOWLEDGE_TEST_STATE")
fi
attempt=$((attempt + 1))
printf '%d\n' "$attempt" > "$KNOWLEDGE_TEST_STATE"
status=$(sed -n "${attempt}p" "$KNOWLEDGE_TEST_STATUSES")
printf 'fake collect attempt=%d status=%s\n' "$attempt" "${status:-1}"
exit "${status:-1}"
""",
        encoding="utf-8",
    )
    _git("add", "scripts", cwd=repo)
    _git("commit", "-m", "test fixture", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)
    return repo, origin, tmp_path / "attempt.txt"


def _run_entrypoint(
    repo: Path,
    origin: Path,
    state: Path,
    tmp_path: Path,
    statuses: tuple[int, ...],
    *,
    max_attempts: int = 4,
) -> subprocess.CompletedProcess[str]:
    status_file = tmp_path / "statuses.txt"
    status_file.write_text("".join(f"{status}\n" for status in statuses), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "KNOWLEDGE_CRON_EXPECTED_ORIGIN": str(origin),
            "KNOWLEDGE_CRON_MAX_ATTEMPTS": str(max_attempts),
            "KNOWLEDGE_CRON_RETRY_DELAY_SECONDS": "0",
            "KNOWLEDGE_TEST_STATE": str(state),
            "KNOWLEDGE_TEST_STATUSES": str(status_file),
        }
    )
    return subprocess.run(
        ["/bin/bash", str(repo / "scripts" / "cron-collect.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_retries_tempfail_until_success(tmp_path: Path):
    repo, origin, state = _make_repo(tmp_path)

    result = _run_entrypoint(repo, origin, state, tmp_path, (75, 75, 0))

    assert result.returncode == 0
    assert state.read_text(encoding="utf-8").strip() == "3"
    assert result.stdout.count('"retry_scheduled":true') == 2


def test_stops_after_bounded_tempfail_attempts(tmp_path: Path):
    repo, origin, state = _make_repo(tmp_path)

    result = _run_entrypoint(
        repo, origin, state, tmp_path, (75, 75, 75, 0), max_attempts=3
    )

    assert result.returncode == 75
    assert state.read_text(encoding="utf-8").strip() == "3"
    assert '"retry_scheduled":false' in result.stdout


def test_does_not_retry_non_temporary_failure(tmp_path: Path):
    repo, origin, state = _make_repo(tmp_path)

    result = _run_entrypoint(repo, origin, state, tmp_path, (1, 0))

    assert result.returncode == 1
    assert state.read_text(encoding="utf-8").strip() == "1"
    assert '"retry_scheduled"' not in result.stdout
