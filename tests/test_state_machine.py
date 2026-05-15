import pytest
from spec_to_pr.models import OrchestratorSession, WorkItem, Phase
from spec_to_pr.state_machine import StateMachine, InvalidTransitionError


def _session(dry_run=False):
    return OrchestratorSession.new(WorkItem.from_jira("ROSAENG-1"), dry_run=dry_run)


def test_spec_ingestion_to_implementation():
    sm = StateMachine()
    session = _session()
    sm.transition(session, spec_valid=True, dry_run=False)
    assert session.current_phase == Phase.IMPLEMENTATION


def test_spec_ingestion_to_dry_run_review():
    sm = StateMachine()
    session = _session(dry_run=True)
    sm.transition(session, spec_valid=True, dry_run=True)
    assert session.current_phase == Phase.DRY_RUN_REVIEW


def test_spec_ingestion_invalid():
    sm = StateMachine()
    session = _session()
    with pytest.raises(InvalidTransitionError):
        sm.transition(session, spec_valid=False)


def test_dry_run_review_approved():
    sm = StateMachine()
    session = _session(dry_run=True)
    session.current_phase = Phase.DRY_RUN_REVIEW
    sm.transition(session, human_approved=True)
    assert session.current_phase == Phase.IMPLEMENTATION


def test_dry_run_review_rejected():
    sm = StateMachine()
    session = _session(dry_run=True)
    session.current_phase = Phase.DRY_RUN_REVIEW
    sm.transition(session, human_approved=False)
    assert session.current_phase == Phase.ABORTED


def test_implementation_to_deployment():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.IMPLEMENTATION
    sm.transition(session, implementation_complete=True)
    assert session.current_phase == Phase.DEPLOYMENT


def test_implementation_failure_to_circuit_breaker():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.IMPLEMENTATION
    sm.transition(session, implementation_complete=False)
    assert session.current_phase == Phase.CIRCUIT_BREAKER_CHECK


def test_deployment_success_to_e2e():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.DEPLOYMENT
    sm.transition(session, deployment_successful=True)
    assert session.current_phase == Phase.E2E_EXECUTION


def test_deployment_failure_to_debug():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.DEPLOYMENT
    sm.transition(session, deployment_successful=False)
    assert session.current_phase == Phase.DEBUG


def test_e2e_pass_to_pr_submission():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.E2E_EXECUTION
    sm.transition(session, tests_passed=True)
    assert session.current_phase == Phase.PR_SUBMISSION


def test_e2e_fail_to_debug():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.E2E_EXECUTION
    sm.transition(session, tests_passed=False)
    assert session.current_phase == Phase.DEBUG


def test_debug_to_circuit_breaker():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.DEBUG
    sm.transition(session)
    assert session.current_phase == Phase.CIRCUIT_BREAKER_CHECK


def test_circuit_breaker_retry():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.CIRCUIT_BREAKER_CHECK
    sm.transition(session, breaker_tripped=False)
    assert session.current_phase == Phase.IMPLEMENTATION
    assert session.attempt_number == 1


def test_circuit_breaker_tripped():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.CIRCUIT_BREAKER_CHECK
    sm.transition(session, breaker_tripped=True)
    assert session.current_phase == Phase.HUMAN_ESCALATION


def test_pr_submission_to_complete():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.PR_SUBMISSION
    sm.transition(session, prs_created=True)
    assert session.current_phase == Phase.COMPLETE


def test_transition_from_terminal_raises():
    sm = StateMachine()
    session = _session()
    session.current_phase = Phase.COMPLETE
    with pytest.raises(InvalidTransitionError):
        sm.transition(session)
