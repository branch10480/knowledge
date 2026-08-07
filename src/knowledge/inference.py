"""Local inference admission checks for the unattended collection job."""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence


INFERENCE_PORTS = (18080, 18082)
LSOF = "/usr/sbin/lsof"


class InferenceCheckError(RuntimeError):
    """Raised when local inference activity cannot be checked safely."""


def busy_inference_ports(
    *,
    ports: Sequence[int] = INFERENCE_PORTS,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[int, ...]:
    """Return ports with established inference connections.

    lsof returns 1 when no matching socket exists. Any other failure is treated
    as an admission-check error so the cron job fails closed.
    """
    busy: list[int] = []
    for port in ports:
        try:
            result = runner(
                [LSOF, "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED", "-t"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise InferenceCheckError("lsof execution failed") from exc
        if result.returncode == 0:
            if result.stdout.strip():
                busy.append(port)
            continue
        if result.returncode != 1:
            raise InferenceCheckError(f"lsof failed for port {port}")
    return tuple(busy)
