"""Tests for project documentation loading."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_to_pr.orchestrator import Config, Orchestrator


def test_load_project_docs_auto_discover(tmp_path):
    """Verify CLAUDE.md is auto-discovered from workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create CLAUDE.md
    claude_md = workspace / "CLAUDE.md"
    claude_md.write_text("# Project Instructions\n\nUse `make test` for testing.")

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)

    # Verify docs were loaded
    assert orch._project_docs is not None
    assert "Project Instructions" in orch._project_docs
    assert "make test" in orch._project_docs


def test_load_project_docs_explicit_path(tmp_path):
    """Verify explicit project docs path is used."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create docs in a different location
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    custom_docs = docs_dir / "CUSTOM.md"
    custom_docs.write_text("# Custom Docs\n\nUse custom workflow.")

    config = Config(
        workspace=workspace,
        project_docs_path=custom_docs,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)

    # Verify custom docs were loaded
    assert orch._project_docs is not None
    assert "Custom Docs" in orch._project_docs
    assert "custom workflow" in orch._project_docs


def test_load_project_docs_not_found(tmp_path):
    """Verify graceful handling when docs are not found."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # No CLAUDE.md created

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)

    # Verify no crash, docs are None
    assert orch._project_docs is None


def test_project_docs_included_in_system_prompt(tmp_path):
    """Verify project docs are appended to system prompt."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create CLAUDE.md
    claude_md = workspace / "CLAUDE.md"
    claude_md.write_text("# Available Make Targets\n\n- make ephemeral-provision\n- make ephemeral-e2e")

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)

    # Get a runner and system prompt
    runner, system_prompt = orch._make_runner("developer")

    # Verify docs are in the system prompt
    assert "Available Make Targets" in system_prompt
    assert "make ephemeral-provision" in system_prompt
    assert "make ephemeral-e2e" in system_prompt
    assert "# Project Documentation" in system_prompt


def test_system_prompt_without_project_docs(tmp_path):
    """Verify system prompt works when no project docs exist."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = Config(
        workspace=workspace,
        storage_path=tmp_path / "sessions",
        agents_path=tmp_path / "agents",
    )

    orch = Orchestrator(config)

    # Get a runner and system prompt
    runner, system_prompt = orch._make_runner("developer")

    # Should have base prompt but no project docs section
    assert "software developer" in system_prompt.lower()
    assert "# Project Documentation" not in system_prompt
