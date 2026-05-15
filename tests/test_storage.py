from datetime import datetime, timezone
from pathlib import Path
from spec_to_pr.models import WorkItem, OrchestratorSession, Phase
from spec_to_pr.models.phase_context import DebugMemoryEntry, FailurePhase, DebugOutcome, E2EResults
from spec_to_pr.storage import FileStorage


def _session():
    return OrchestratorSession.new(WorkItem.from_jira("ROSAENG-1234"))


def test_save_and_load_session(tmp_path):
    storage = FileStorage(base_path=tmp_path)
    session = _session()
    storage.save_session(session)
    loaded = storage.load_session("ROSAENG-1234")
    assert loaded.work_item.work_id == "ROSAENG-1234"
    assert loaded.current_phase == Phase.SPEC_INGESTION
    assert loaded.session_id == session.session_id


def test_load_session_not_found(tmp_path):
    storage = FileStorage(base_path=tmp_path)
    assert storage.load_session("ROSAENG-9999") is None


def test_save_and_load_debug_entry(tmp_path):
    storage = FileStorage(base_path=tmp_path)
    entry = DebugMemoryEntry(
        attempt_number=1,
        timestamp=datetime.now(timezone.utc),
        phase_at_failure=FailurePhase.E2E_EXECUTION,
        error_summary="Pod OOMKilled",
        error_fingerprint="abc123",
        test_results=E2EResults(total=10, passed=7, failed=3, failed_tests=["test_foo"]),
        debug_findings=["pod restarted 3 times"],
        hypotheses=["memory limit too low"],
        changes_attempted=["deploy/values.yaml"],
        outcome=DebugOutcome.RETRY,
    )
    storage.save_debug_entry("ROSAENG-1234", entry)
    entries = storage.load_debug_entries("ROSAENG-1234")
    assert len(entries) == 1
    assert entries[0].error_summary == "Pod OOMKilled"
    assert entries[0].error_fingerprint == "abc123"
    assert entries[0].test_results.failed == 3


def test_save_multiple_debug_entries(tmp_path):
    storage = FileStorage(base_path=tmp_path)
    for i in range(3):
        entry = DebugMemoryEntry(
            attempt_number=i,
            timestamp=datetime.now(timezone.utc),
            phase_at_failure=FailurePhase.E2E_EXECUTION,
            error_summary=f"error {i}",
            error_fingerprint=f"fp{i}",
        )
        storage.save_debug_entry("ROSAENG-1234", entry)
    entries = storage.load_debug_entries("ROSAENG-1234")
    assert len(entries) == 3


def test_debug_entries_empty_for_new_work_id(tmp_path):
    storage = FileStorage(base_path=tmp_path)
    entries = storage.load_debug_entries("ROSAENG-0000")
    assert entries == []


def test_session_updated_phase_persists(tmp_path):
    storage = FileStorage(base_path=tmp_path)
    session = _session()
    storage.save_session(session)
    session.current_phase = Phase.IMPLEMENTATION
    storage.save_session(session)
    loaded = storage.load_session("ROSAENG-1234")
    assert loaded.current_phase == Phase.IMPLEMENTATION
