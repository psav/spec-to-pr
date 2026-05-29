"""Tests for the spec-to-pr --visualise TUI renderer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from io import StringIO
from pathlib import Path

import pytest

from spec_to_pr.visualiser import PHASE_ORDER, Visualiser

_CLI_BINARY = shutil.which("spec-to-pr") or ".venv/bin/spec-to-pr"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SESSION_YAML = """\
session_id: abc-123
current_phase: {phase}
attempt_number: {attempt}
max_attempts: 3
dry_run: false
work_item:
  work_id: {work_id}
repos: []
"""

_CONV_LINES = [
    {"type": "metadata", "work_id": "TEST-1", "model": "claude-sonnet-4-6", "system_prompt": ""},
    {"type": "tool_call", "turn": 1, "tool": "Read", "input": {"path": "src/api.py"}, "result": "# code here"},
    {"type": "assistant", "turn": 1, "content": [{"type": "text", "text": "I'll make the changes now."}], "stop_reason": "tool_use"},
    {"type": "tool_call", "turn": 2, "tool": "Write", "input": {"path": "src/api.py", "content": "new"}, "result": "Written src/api.py (3 chars)"},
    {"type": "assistant", "turn": 2, "content": [{"type": "text", "text": "Implementation complete."}], "stop_reason": "end_turn"},
    {"type": "result", "final_text": "Implementation complete."},
]


def _make_env(tmp_path: Path, phase: str = "implementation", attempt: int = 0) -> Visualiser:
    storage = tmp_path / "sessions"
    convs = tmp_path / "conversations"
    (storage / "TEST-1").mkdir(parents=True)
    convs.mkdir()
    (storage / "TEST-1" / "session.yaml").write_text(
        _SESSION_YAML.format(phase=phase, attempt=attempt, work_id="TEST-1")
    )
    (convs / "TEST-1_20250526_120000.jsonl").write_text(
        "\n".join(json.dumps(l) for l in _CONV_LINES) + "\n"
    )
    return Visualiser(storage_dir=storage, conversations_dir=convs, work_id="TEST-1")


def _render_to_str(v: Visualiser) -> str:
    from rich.console import Console
    buf = StringIO()
    c = Console(file=buf, width=140, no_color=True)
    c.print(v._render(c))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Phase trio logic
# ---------------------------------------------------------------------------

def test_phase_trio_mid_sequence(tmp_path: Path) -> None:
    v = _make_env(tmp_path, phase="implementation")
    v._poll_session()
    prev, current, nxt = v._phase_trio()
    assert current == "implementation"
    assert prev == PHASE_ORDER[PHASE_ORDER.index("implementation") - 1]
    assert nxt == PHASE_ORDER[PHASE_ORDER.index("implementation") + 1]


def test_phase_trio_first_phase(tmp_path: Path) -> None:
    v = _make_env(tmp_path, phase="spec_ingestion")
    v._poll_session()
    prev, current, nxt = v._phase_trio()
    assert current == "spec_ingestion"
    assert prev is None
    assert nxt == "dry_run_review"


def test_phase_trio_last_normal_phase(tmp_path: Path) -> None:
    v = _make_env(tmp_path, phase="pr_submission")
    v._poll_session()
    prev, current, nxt = v._phase_trio()
    assert current == "pr_submission"
    assert nxt is None


def test_phase_trio_terminal_complete(tmp_path: Path) -> None:
    v = _make_env(tmp_path, phase="complete")
    v._poll_session()
    prev, current, nxt = v._phase_trio()
    assert current == "complete"
    assert nxt is None


def test_phase_trio_no_session(tmp_path: Path) -> None:
    v = Visualiser(storage_dir=tmp_path / "empty", conversations_dir=tmp_path / "c")
    prev, current, nxt = v._phase_trio()
    assert current is None


# ---------------------------------------------------------------------------
# Session polling
# ---------------------------------------------------------------------------

def test_poll_session_reads_work_id(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    assert v._session.get("work_item", {}).get("work_id") == "TEST-1"
    assert v._session.get("current_phase") == "implementation"
    assert v._session.get("attempt_number") == 0


def test_poll_session_auto_discovers_latest(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    (storage / "AUTO-99").mkdir(parents=True)
    (storage / "AUTO-99" / "session.yaml").write_text(
        _SESSION_YAML.format(phase="deployment", attempt=1, work_id="AUTO-99")
    )
    v = Visualiser(storage_dir=storage, conversations_dir=tmp_path / "c", work_id=None)
    v._poll_session()
    assert v._session.get("current_phase") == "deployment"
    assert v.work_id == "AUTO-99"


def test_poll_session_not_reread_without_mtime_change(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    old = v._session.copy()
    # Inject a sentinel without touching the file
    v._session["_sentinel"] = True
    v._poll_session()  # mtime unchanged — should not re-read
    assert v._session.get("_sentinel") is True


# ---------------------------------------------------------------------------
# Conversation log polling and formatting
# ---------------------------------------------------------------------------

def test_poll_log_reads_entries(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    v._poll_log()
    texts = [t.plain for t in v._log_lines]
    combined = " ".join(texts)
    assert "Read" in combined
    assert "Write" in combined
    assert "Implementation complete" in combined


def test_poll_log_metadata_entry(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    v._poll_log()
    texts = [t.plain for t in v._log_lines]
    assert any("claude-sonnet-4-6" in t for t in texts)


def test_poll_log_tool_call_shows_tool_name_and_result(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    v._poll_log()
    texts = [t.plain for t in v._log_lines]
    read_line = next((t for t in texts if "Read" in t and "src/api.py" in t), None)
    assert read_line is not None
    assert "# code here" in read_line or "code" in read_line


def test_poll_log_assistant_end_turn_included(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    v._poll_log()
    texts = [t.plain for t in v._log_lines]
    assert any("Implementation complete" in t for t in texts)


def test_poll_log_incremental_reads(tmp_path: Path) -> None:
    storage = tmp_path / "sessions"
    convs = tmp_path / "conversations"
    (storage / "INC-1").mkdir(parents=True)
    convs.mkdir()
    (storage / "INC-1" / "session.yaml").write_text(
        _SESSION_YAML.format(phase="implementation", attempt=0, work_id="INC-1")
    )
    conv = convs / "INC-1_20250526_120000.jsonl"
    conv.write_text(json.dumps({"type": "metadata", "work_id": "INC-1", "model": "m", "system_prompt": ""}) + "\n")

    v = Visualiser(storage_dir=storage, conversations_dir=convs, work_id="INC-1")
    v._poll_session()
    v._poll_log()
    count_before = len(v._log_lines)

    # Append more entries to simulate a live agent writing new lines
    with conv.open("a") as f:
        f.write(json.dumps({"type": "tool_call", "turn": 1, "tool": "Bash", "input": {"command": "ls"}, "result": "file.py"}) + "\n")

    v._poll_log()
    assert len(v._log_lines) > count_before
    texts = [t.plain for t in v._log_lines]
    assert any("Bash" in t for t in texts)


def test_poll_log_switches_to_newer_file(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    v._poll_log()
    assert v._conv_file is not None
    first_file = v._conv_file

    # Write a newer conversation file with an explicitly future mtime so it
    # wins the max(mtime) comparison regardless of filesystem time resolution.
    newer = (tmp_path / "conversations") / "TEST-1_20250526_130000.jsonl"
    newer.write_text(json.dumps({"type": "metadata", "work_id": "TEST-1", "model": "m2", "system_prompt": ""}) + "\n")
    future = time.time() + 10
    os.utime(newer, (future, future))

    v._poll_log()
    assert v._conv_file == newer
    assert v._conv_file != first_file
    texts = [t.plain for t in v._log_lines]
    assert any("m2" in t for t in texts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_shows_current_phase(tmp_path: Path) -> None:
    v = _make_env(tmp_path, phase="implementation")
    v._poll_session()
    v._poll_log()
    out = _render_to_str(v)
    assert "IMPLEMENTATION" in out


def test_render_shows_prev_and_next_phases(tmp_path: Path) -> None:
    v = _make_env(tmp_path, phase="implementation")
    v._poll_session()
    v._poll_log()
    out = _render_to_str(v)
    prev_label = "Dry-Run Review"   # phase before implementation
    next_label = "Deployment"       # phase after implementation
    assert prev_label in out
    assert next_label in out


def test_render_shows_work_id_and_attempt(tmp_path: Path) -> None:
    v = _make_env(tmp_path, attempt=1)
    v._poll_session()
    out = _render_to_str(v)
    assert "TEST-1" in out
    assert "attempt 2/3" in out


def test_render_shows_tool_calls_in_log(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    v._poll_log()
    out = _render_to_str(v)
    assert "Read" in out
    assert "Write" in out


def test_render_shows_result_in_log(tmp_path: Path) -> None:
    v = _make_env(tmp_path)
    v._poll_session()
    v._poll_log()
    out = _render_to_str(v)
    assert "Implementation complete" in out


def test_render_waiting_state_no_session(tmp_path: Path) -> None:
    v = Visualiser(storage_dir=tmp_path / "empty", conversations_dir=tmp_path / "c")
    out = _render_to_str(v)
    assert "waiting" in out.lower() or "Waiting" in out


def test_render_terminal_complete_phase(tmp_path: Path) -> None:
    v = _make_env(tmp_path, phase="complete")
    v._poll_session()
    out = _render_to_str(v)
    assert "COMPLETE" in out


# ---------------------------------------------------------------------------
# CLI routing
# ---------------------------------------------------------------------------

def test_cli_visualise_help() -> None:
    result = subprocess.run(
        [_CLI_BINARY, "--visualise", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "--storage" in result.stdout
    assert "--conversations" in result.stdout
    assert "--work-id" in result.stdout


def test_cli_normal_subcommand_unaffected() -> None:
    result = subprocess.run(
        [_CLI_BINARY, "validate", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--file" in result.stdout


def test_cli_visualise_help_includes_workspace() -> None:
    result = subprocess.run(
        [_CLI_BINARY, "--visualise", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--workspace" in result.stdout


# ---------------------------------------------------------------------------
# Make-log streaming
# ---------------------------------------------------------------------------

def _make_log_env(tmp_path: Path, work_id: str = "TEST-1") -> tuple[Visualiser, Path]:
    """Return a Visualiser with workspace set and a make-log directory ready."""
    storage = tmp_path / "sessions"
    convs = tmp_path / "conversations"
    workspace = tmp_path / "workspace"
    repo = workspace / "some-repo"
    log_dir = repo / ".spec-to-pr" / "make-logs"
    log_dir.mkdir(parents=True)
    (storage / work_id).mkdir(parents=True)
    convs.mkdir()
    (storage / work_id / "session.yaml").write_text(
        _SESSION_YAML.format(phase="deployment", attempt=0, work_id=work_id)
    )
    v = Visualiser(
        storage_dir=storage,
        conversations_dir=convs,
        work_id=work_id,
        workspace=workspace,
    )
    return v, log_dir


def test_find_latest_make_log_returns_none_without_workspace(tmp_path: Path) -> None:
    v = Visualiser(storage_dir=tmp_path, conversations_dir=tmp_path, work_id="TEST-1")
    assert v._find_latest_make_log() is None


def test_find_latest_make_log_returns_none_when_no_logs(tmp_path: Path) -> None:
    v, log_dir = _make_log_env(tmp_path)
    assert v._find_latest_make_log() is None


def test_find_latest_make_log_finds_matching_log(tmp_path: Path) -> None:
    v, log_dir = _make_log_env(tmp_path)
    log_file = log_dir / "TEST-1-ephemeral-provision-20260529-120000.log"
    log_file.write_text("=== make ephemeral-provision started ===\nsome output\n")
    result = v._find_latest_make_log()
    assert result == log_file


def test_find_latest_make_log_ignores_other_work_id(tmp_path: Path) -> None:
    v, log_dir = _make_log_env(tmp_path)
    (log_dir / "OTHER-1-ephemeral-provision-20260529-120000.log").write_text("noise")
    assert v._find_latest_make_log() is None


def test_find_latest_make_log_returns_most_recent(tmp_path: Path) -> None:
    v, log_dir = _make_log_env(tmp_path)
    old = log_dir / "TEST-1-ephemeral-provision-20260529-110000.log"
    new = log_dir / "TEST-1-ephemeral-e2e-20260529-120000.log"
    old.write_text("old")
    time.sleep(0.01)
    new.write_text("new")
    assert v._find_latest_make_log() == new


def test_poll_log_streams_make_log_when_no_conv(tmp_path: Path) -> None:
    v, log_dir = _make_log_env(tmp_path)
    log_file = log_dir / "TEST-1-ephemeral-e2e-20260529-120000.log"
    log_file.write_text("=== make ephemeral-e2e started ===\nBuilding image...\n")
    v._poll_log()
    texts = [t.plain for t in v._log_lines]
    combined = " ".join(texts)
    assert "Building image" in combined


def test_poll_log_prefers_fresh_conv_over_make_log(tmp_path: Path) -> None:
    v, log_dir = _make_log_env(tmp_path)
    log_file = log_dir / "TEST-1-ephemeral-e2e-20260529-120000.log"
    log_file.write_text("make output\n")
    # Write a fresh JSONL (just modified)
    convs = tmp_path / "conversations"
    conv_file = convs / "TEST-1_fresh.jsonl"
    entry = {"type": "result", "final_text": "Agent done."}
    conv_file.write_text(json.dumps(entry) + "\n")
    v._poll_log()
    texts = [t.plain for t in v._log_lines]
    combined = " ".join(texts)
    assert "Agent done" in combined
    assert "make output" not in combined
