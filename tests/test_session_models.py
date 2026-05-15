from datetime import datetime, timezone
from spec_to_pr.models import (
    OrchestratorSession,
    RepoState,
    Phase,
    WorkItem,
    SourceType,
)


def _make_work_item():
    return WorkItem.from_jira("ROSAENG-42")


def test_session_initial_phase():
    session = OrchestratorSession.new(_make_work_item())
    assert session.current_phase == Phase.SPEC_INGESTION
    assert session.attempt_number == 0
    assert session.dry_run is False
    assert session.repos == []


def test_session_is_not_terminal_initially():
    session = OrchestratorSession.new(_make_work_item())
    assert not session.is_terminal


def test_session_is_terminal_complete():
    session = OrchestratorSession.new(_make_work_item())
    session.current_phase = Phase.COMPLETE
    assert session.is_terminal


def test_session_is_terminal_escalated():
    session = OrchestratorSession.new(_make_work_item())
    session.current_phase = Phase.HUMAN_ESCALATION
    assert session.is_terminal


def test_session_is_terminal_aborted():
    session = OrchestratorSession.new(_make_work_item())
    session.current_phase = Phase.ABORTED
    assert session.is_terminal


def test_session_dry_run():
    session = OrchestratorSession.new(_make_work_item(), dry_run=True)
    assert session.dry_run is True


def test_repo_state_defaults():
    repo = RepoState(repo_name="platform-api", repo_url="https://github.com/x/y", workspace_path="/ws/y")
    assert repo.branch == "main"
    assert repo.changes == []
    assert repo.pr_url is None
    assert repo.status == "clean"


def test_session_has_uuid():
    s1 = OrchestratorSession.new(_make_work_item())
    s2 = OrchestratorSession.new(_make_work_item())
    assert s1.session_id != s2.session_id
    assert len(s1.session_id) == 36  # UUID format
