"""Tests for commit and PR creation functionality."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from spec_to_pr.models import OrchestratorSession, WorkItem
from spec_to_pr.orchestrator import Config, Orchestrator


def test_commit_and_track_changes(tmp_path):
    """Verify changes are detected, committed, and tracked."""
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

    # Add a remote
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=workspace,
        capture_output=True,
    )

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)
    session = OrchestratorSession.new(WorkItem.from_inline("Test"))

    # Make a change
    (workspace / "test.txt").write_text("New file")

    # Commit and track
    orch._commit_and_track_changes(session)

    # Verify branch was created
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    assert "spec-to-pr/" in result.stdout

    # Verify commit was made
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    assert "Automated implementation" in result.stdout

    # Verify session.repos was populated
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


@patch("spec_to_pr.orchestrator.subprocess.run")
def test_create_pr_pushes_and_creates(mock_subprocess, tmp_path):
    """Verify _create_pr pushes branch and creates PR."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)
    session = OrchestratorSession.new(WorkItem.from_inline("Test"))

    # Mock git push success
    push_result = MagicMock()
    push_result.returncode = 0

    # Mock gh pr create success
    pr_result = MagicMock()
    pr_result.returncode = 0
    pr_result.stdout = "https://github.com/test/repo/pull/123\n"

    mock_subprocess.side_effect = [push_result, pr_result]

    # Create PR
    pr_url = orch._create_pr("test/repo", "spec-to-pr/TEST-123", session)

    # Verify git push was called
    assert mock_subprocess.call_args_list[0] == call(
        ["git", "push", "-u", "origin", "spec-to-pr/TEST-123"],
        capture_output=True,
        text=True,
        cwd=workspace,
    )

    # Verify gh pr create was called
    assert "gh" in mock_subprocess.call_args_list[1][0][0]
    assert "pr" in mock_subprocess.call_args_list[1][0][0]
    assert "create" in mock_subprocess.call_args_list[1][0][0]

    # Verify PR URL was returned
    assert pr_url == "https://github.com/test/repo/pull/123"


@patch("spec_to_pr.orchestrator.subprocess.run")
def test_create_pr_handles_push_failure(mock_subprocess, tmp_path):
    """Verify _create_pr returns None when push fails."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)
    session = OrchestratorSession.new(WorkItem.from_inline("Test"))

    # Mock git push failure
    push_result = MagicMock()
    push_result.returncode = 1
    push_result.stderr = "Permission denied"

    mock_subprocess.return_value = push_result

    # Create PR should fail
    pr_url = orch._create_pr("test/repo", "spec-to-pr/TEST-123", session)

    assert pr_url is None
