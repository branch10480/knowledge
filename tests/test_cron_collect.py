"""Contract tests for retired shell entrypoints."""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_legacy_cron_entrypoint_fails_closed() -> None:
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/cron-collect.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 64
    assert "Hermes cron job" in result.stderr


def test_legacy_collect_entrypoint_fails_closed() -> None:
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/collect.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 64
    assert "knowledge_start" in result.stderr
