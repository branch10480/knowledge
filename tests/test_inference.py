"""Unattended local-inference admission checks."""
from __future__ import annotations

import subprocess

import pytest

from knowledge import cli, inference


def _result(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_busy_inference_ports_returns_established_ports():
    results = iter((_result(1), _result(0, "123\n456\n")))

    def runner(*_args, **_kwargs):
        return next(results)

    assert inference.busy_inference_ports(runner=runner) == (18082,)


def test_busy_inference_ports_accepts_fully_idle_state():
    assert inference.busy_inference_ports(
        runner=lambda *_args, **_kwargs: _result(1)
    ) == ()


def test_busy_inference_ports_fails_closed_on_lsof_error():
    with pytest.raises(inference.InferenceCheckError):
        inference.busy_inference_ports(
            runner=lambda *_args, **_kwargs: _result(2)
        )


def test_busy_inference_ports_fails_closed_when_lsof_is_missing():
    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    with pytest.raises(inference.InferenceCheckError):
        inference.busy_inference_ports(runner=missing)


def test_check_inference_idle_command_reports_idle(monkeypatch, capsys):
    monkeypatch.setattr(inference, "busy_inference_ports", lambda: ())
    assert cli.check_inference_idle_command() == 0
    assert '"inference_idle": true' in capsys.readouterr().out


def test_check_inference_idle_command_uses_tempfail_for_busy_state(
    monkeypatch, capsys
):
    monkeypatch.setattr(inference, "busy_inference_ports", lambda: (18080, 18082))
    assert cli.check_inference_idle_command() == 75
    output = capsys.readouterr().out
    assert '"busy_ports": [18080, 18082]' in output


def test_check_inference_idle_command_fails_closed(monkeypatch, capsys):
    def fail():
        raise inference.InferenceCheckError

    monkeypatch.setattr(inference, "busy_inference_ports", fail)
    assert cli.check_inference_idle_command() == 1
    assert '"inference idle check failed"' in capsys.readouterr().out
