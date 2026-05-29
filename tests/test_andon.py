"""Tests for the andon cord — agent-initiated escalation."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_to_pr.models import AndonSignal, OrchestratorSession, Phase, WorkItem
from spec_to_pr.models.session import ANDON_TARGETS
from spec_to_pr.orchestrator import Config, Orchestrator
from spec_to_pr.state_machine import StateMachine


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _session():
    return OrchestratorSession.new(WorkItem.from_inline("Fix the thing"))


def _config(tmp_path: Path) -> Config:
    return Config(
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
        max_attempts=3,
        workspace=tmp_path,
    )


def _popen_ok():
    m = MagicMock()
    m.stdout = iter([])
    m.stderr = iter([])
    m.returncode = 0
    m.wait.return_value = 0
    return m


def _run_ok():
    m = MagicMock()
    m.returncode = 0
    m.stdout = "ok"
    m.stderr = ""
    return m


# ---------------------------------------------------------------------------
# AndonSignal model
# ---------------------------------------------------------------------------

def test_andon_signal_fields():
    sig = AndonSignal(target="HUMAN_ESCALATION", reason="spec is ambiguous", agent="developer")
    assert sig.target == "HUMAN_ESCALATION"
    assert sig.reason == "spec is ambiguous"
    assert sig.agent == "developer"


def test_andon_targets_set():
    assert ANDON_TARGETS == {"HUMAN_ESCALATION", "REPROVISION", "IMPLEMENTED"}


# ---------------------------------------------------------------------------
# _extract_andon_signal
# ---------------------------------------------------------------------------

def _orchestrator(tmp_path: Path) -> Orchestrator:
    return Orchestrator(_config(tmp_path))


def test_extract_andon_human_escalation(tmp_path):
    orch = _orchestrator(tmp_path)
    text = "I investigated and cannot proceed.\n\nANDON: HUMAN_ESCALATION: The spec contradicts itself."
    sig = orch._extract_andon_signal(text, agent="developer")
    assert sig is not None
    assert sig.target == "HUMAN_ESCALATION"
    assert sig.reason == "The spec contradicts itself."
    assert sig.agent == "developer"


def test_extract_andon_reprovision(tmp_path):
    orch = _orchestrator(tmp_path)
    text = "Debug complete.\nANDON: REPROVISION: env 69bc44e6 is 8 hours old and has 4 degraded clusters."
    sig = orch._extract_andon_signal(text, agent="developer-debug")
    assert sig is not None
    assert sig.target == "REPROVISION"
    assert "69bc44e6" in sig.reason


def test_extract_andon_implemented(tmp_path):
    orch = _orchestrator(tmp_path)
    text = "ANDON: IMPLEMENTED: fix was applied in attempt 2, no code changes needed."
    sig = orch._extract_andon_signal(text, agent="developer-debug")
    assert sig is not None
    assert sig.target == "IMPLEMENTED"


def test_extract_andon_case_insensitive(tmp_path):
    orch = _orchestrator(tmp_path)
    sig = orch._extract_andon_signal("andon: reprovision: env is dead", agent="x")
    assert sig is not None
    assert sig.target == "REPROVISION"


def test_extract_andon_no_signal(tmp_path):
    orch = _orchestrator(tmp_path)
    assert orch._extract_andon_signal("Debug complete.", agent="developer") is None


def test_extract_andon_unknown_target_not_matched(tmp_path):
    orch = _orchestrator(tmp_path)
    assert orch._extract_andon_signal("ANDON: SOMETHING_ELSE: reason", agent="x") is None


# ---------------------------------------------------------------------------
# State machine: skip_implementation flag
# ---------------------------------------------------------------------------

def test_circuit_breaker_skip_implementation_goes_to_deployment():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.CIRCUIT_BREAKER_CHECK
    session.skip_implementation = True
    sm.transition(session, breaker_tripped=False)
    assert session.current_phase == Phase.DEPLOYMENT
    assert session.attempt_number == 1
    assert session.skip_implementation is False  # flag cleared


def test_circuit_breaker_skip_implementation_tripped_still_escalates():
    """Circuit breaker trip takes priority over skip_implementation."""
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.CIRCUIT_BREAKER_CHECK
    session.skip_implementation = True
    sm.transition(session, breaker_tripped=True)
    assert session.current_phase == Phase.HUMAN_ESCALATION


def test_circuit_breaker_no_skip_implementation_goes_to_implementation():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.CIRCUIT_BREAKER_CHECK
    session.skip_implementation = False
    sm.transition(session, breaker_tripped=False)
    assert session.current_phase == Phase.IMPLEMENTATION


# ---------------------------------------------------------------------------
# Orchestrator: developer agent pulls HUMAN_ESCALATION andon
# ---------------------------------------------------------------------------

@patch("spec_to_pr.agent_runner.AgentRunner.run",
       return_value="Cannot implement this.\nANDON: HUMAN_ESCALATION: Spec is ambiguous.")
@patch("spec_to_pr.orchestrator.subprocess.run", return_value=_run_ok())
def test_developer_andon_human_escalation(mock_run, mock_agent, tmp_path):
    session = Orchestrator(_config(tmp_path)).run(WorkItem.from_inline("fix it"))
    assert session.current_phase == Phase.HUMAN_ESCALATION


# ---------------------------------------------------------------------------
# Orchestrator._debug: REPROVISION sets skip_implementation on the session
# ---------------------------------------------------------------------------

def test_debug_andon_reprovision_sets_skip_implementation(tmp_path):
    from spec_to_pr.models.circuit_breaker import CircuitBreaker

    orch = Orchestrator(_config(tmp_path))
    orch._circuit_breaker = CircuitBreaker(max_attempts=3)

    session = _session()
    session.current_phase = Phase.DEBUG
    # Storage needs the session directory to exist
    (tmp_path / "sessions" / session.work_item.work_id).mkdir(parents=True)
    (tmp_path / "sessions" / session.work_item.work_id / "context.md").write_text("# Context\n\n---\n\n*Agents: read this file before starting. Most recent updates appear first, below this line.*\n\n")

    with patch("spec_to_pr.agent_runner.AgentRunner.run",
               return_value="Environment is dead.\nANDON: REPROVISION: env has 4 degraded clusters."):
        orch._debug(session)

    assert session.skip_implementation is True
    # Normal DEBUG→CIRCUIT_BREAKER_CHECK transition still fires (REPROVISION doesn't bypass it)
    assert session.current_phase == Phase.CIRCUIT_BREAKER_CHECK


def test_debug_andon_implemented_sets_skip_implementation(tmp_path):
    from spec_to_pr.models.circuit_breaker import CircuitBreaker

    orch = Orchestrator(_config(tmp_path))
    orch._circuit_breaker = CircuitBreaker(max_attempts=3)

    session = _session()
    session.current_phase = Phase.DEBUG
    (tmp_path / "sessions" / session.work_item.work_id).mkdir(parents=True)
    (tmp_path / "sessions" / session.work_item.work_id / "context.md").write_text("# Context\n\n---\n\n*Agents: read this file before starting. Most recent updates appear first, below this line.*\n\n")

    with patch("spec_to_pr.agent_runner.AgentRunner.run",
               return_value="Fix was applied in attempt 2.\nANDON: IMPLEMENTED: no code changes needed."):
        orch._debug(session)

    assert session.skip_implementation is True
    assert session.current_phase == Phase.CIRCUIT_BREAKER_CHECK


# ---------------------------------------------------------------------------
# Orchestrator._debug: HUMAN_ESCALATION bypasses circuit breaker
# ---------------------------------------------------------------------------

def test_debug_andon_human_escalation_bypasses_circuit_breaker(tmp_path):
    from spec_to_pr.models.circuit_breaker import CircuitBreaker

    orch = Orchestrator(_config(tmp_path))
    # High limit — andon should stop us long before the breaker would trip
    orch._circuit_breaker = CircuitBreaker(max_attempts=5)

    session = _session()
    session.current_phase = Phase.DEBUG
    (tmp_path / "sessions" / session.work_item.work_id).mkdir(parents=True)
    (tmp_path / "sessions" / session.work_item.work_id / "context.md").write_text("# Context\n\n---\n\n*Agents: read this file before starting. Most recent updates appear first, below this line.*\n\n")

    with patch("spec_to_pr.agent_runner.AgentRunner.run",
               return_value="Cannot fix.\nANDON: HUMAN_ESCALATION: External dependency will never exist."):
        orch._debug(session)

    # HUMAN_ESCALATION is set immediately — state_machine.transition(session) is not called
    assert session.current_phase == Phase.HUMAN_ESCALATION
    assert session.skip_implementation is False  # not set for human escalation
