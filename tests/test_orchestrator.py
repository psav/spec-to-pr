"""Integration tests for the Orchestrator — AgentRunner.run and subprocess are mocked."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_to_pr.models import OrchestratorSession, Phase, WorkItem
from spec_to_pr.orchestrator import Config, Orchestrator


def _config(tmp_path: Path) -> Config:
    return Config(
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
        max_attempts=3,
        workspace=tmp_path,
    )


def _work_item(text="Add health check endpoint") -> WorkItem:
    return WorkItem.from_inline(text)


def _make_ok():
    m = MagicMock()
    m.returncode = 0
    m.stdout = "ok"
    m.stderr = ""
    return m


def _make_fail():
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = "make failed"
    return m


@patch("spec_to_pr.agent_runner.AgentRunner.run", return_value="Implementation complete.")
@patch("spec_to_pr.orchestrator.subprocess.run", return_value=_make_ok())
def test_happy_path_complete(mock_make, mock_agent, tmp_path):
    """Spec → Implementation → Deploy → E2E pass → PR → Complete."""
    config = _config(tmp_path)
    session = Orchestrator(config).run(_work_item())
    assert session.current_phase == Phase.COMPLETE


@patch("spec_to_pr.agent_runner.AgentRunner.run", return_value="Implementation complete.")
@patch("spec_to_pr.orchestrator.subprocess.run", return_value=_make_ok())
def test_dry_run_aborts(mock_make, mock_agent, tmp_path):
    with patch("builtins.input", return_value="n"):
        session = Orchestrator(_config(tmp_path)).run(_work_item(), dry_run=True)
    assert session.current_phase == Phase.ABORTED


@patch("spec_to_pr.agent_runner.AgentRunner.run", return_value="Implementation complete.")
@patch("spec_to_pr.orchestrator.subprocess.run", return_value=_make_ok())
def test_dry_run_continues_on_approval(mock_make, mock_agent, tmp_path):
    with patch("builtins.input", return_value="y"):
        session = Orchestrator(_config(tmp_path)).run(_work_item(), dry_run=True)
    assert session.current_phase == Phase.COMPLETE


@patch("spec_to_pr.agent_runner.AgentRunner.run", return_value="Implementation complete.")
@patch("spec_to_pr.orchestrator.subprocess.run", return_value=_make_ok())
def test_skip_deploy_goes_straight_to_complete(mock_make, mock_agent, tmp_path):
    config = _config(tmp_path)
    config.skip_deploy = True
    session = Orchestrator(config).run(_work_item())
    assert session.current_phase == Phase.COMPLETE
    # make should never have been called for ephemeral targets
    make_targets = [c.args[0][1] for c in mock_make.call_args_list if c.args]
    assert "ephemeral-dev" not in make_targets


@patch("spec_to_pr.agent_runner.AgentRunner.run", return_value="- pod OOMKilled\n- memory limit too low")
@patch("spec_to_pr.orchestrator.subprocess.run")
def test_e2e_failure_trips_circuit_breaker(mock_make, mock_agent, tmp_path):
    """E2E failures cycle through debug→circuit_breaker until escalation."""
    def make_side_effect(*args, **kwargs):
        cmd = args[0] if args else []
        if "e2e" in " ".join(cmd):
            return _make_fail()
        return _make_ok()

    mock_make.side_effect = make_side_effect
    config = _config(tmp_path)
    config.max_attempts = 2
    session = Orchestrator(config).run(_work_item())
    assert session.current_phase == Phase.HUMAN_ESCALATION


@patch("spec_to_pr.agent_runner.AgentRunner.run", return_value="Implementation complete.")
@patch("spec_to_pr.orchestrator.subprocess.run", return_value=_make_ok())
def test_resume_loads_and_continues(mock_make, mock_agent, tmp_path):
    config = _config(tmp_path)
    orch = Orchestrator(config)
    wi = _work_item()
    session = OrchestratorSession.new(wi)
    session.current_phase = Phase.IMPLEMENTATION
    orch.storage.save_session(session)

    resumed = Orchestrator(config).resume(wi.work_id)
    assert resumed.current_phase == Phase.COMPLETE


@patch("spec_to_pr.agent_runner.AgentRunner.run", side_effect=RuntimeError("API error"))
def test_implementation_failure_trips_circuit_breaker(mock_agent, tmp_path):
    config = _config(tmp_path)
    config.max_attempts = 1
    session = Orchestrator(config).run(_work_item())
    assert session.current_phase == Phase.HUMAN_ESCALATION
