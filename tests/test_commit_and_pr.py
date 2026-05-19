"""Tests for commit and PR creation functionality."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_to_pr.models import OrchestratorSession, WorkItem
from spec_to_pr.orchestrator import Config, Orchestrator


def test_commit_and_track_changes(tmp_path):
    """Verify that a repo subdirectory with uncommitted changes triggers the committer agent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # _find_repos_with_local_work scans subdirectories of workspace (not workspace itself)
    repo_dir = workspace / "my-repo"
    repo_dir.mkdir()

    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True)

    # Create initial commit so there's a HEAD
    (repo_dir / "README.md").write_text("# Initial")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo_dir, capture_output=True)

    # Add a remote so _commit_and_track_changes can derive repo_name
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=repo_dir,
        capture_output=True,
    )

    # Add an uncommitted change so _find_repos_with_local_work picks up the repo
    (repo_dir / "test.txt").write_text("New file")

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)
    session = OrchestratorSession.new(WorkItem.from_inline("Test"))

    # Mock the agent runner so we don't need an LLM connection.
    # The committer agent response must include "Committed files:" for session.repos to be populated.
    mock_runner = MagicMock()
    mock_runner.run.return_value = "Committed files: test.txt\nCommit complete."

    with patch.object(orch, "_make_runner", return_value=(mock_runner, "system prompt")):
        orch._commit_and_track_changes(session)

    # Committer agent was invoked once
    mock_runner.run.assert_called_once()

    # session.repos should be populated from the agent response
    assert len(session.repos) == 1
    assert session.repos[0].repo_name == "test/repo"
    assert session.repos[0].status == "committed"
    assert "test.txt" in session.repos[0].changes


def test_commit_and_track_no_changes(tmp_path):
    """Verify graceful handling when there are no changes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Initialize a git repo
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace)

    # Create initial commit
    (workspace / "README.md").write_text("# Initial")
    subprocess.run(["git", "add", "README.md"], cwd=workspace)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=workspace, capture_output=True)

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)
    session = OrchestratorSession.new(WorkItem.from_inline("Test"))

    # No changes made - call method
    orch._commit_and_track_changes(session)

    # Verify no repos were tracked
    assert len(session.repos) == 0


def test_submit_prs_skips_when_no_repos(tmp_path):
    """Verify _submit_prs transitions to COMPLETE when there are no repos to process."""
    from spec_to_pr.models.session import Phase

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)
    session = OrchestratorSession.new(WorkItem.from_inline("Test"))
    session.current_phase = Phase.PR_SUBMISSION
    # No repos → repos_to_process will be empty
    session.repos = []

    orch._submit_prs(session)

    assert session.current_phase == Phase.COMPLETE
