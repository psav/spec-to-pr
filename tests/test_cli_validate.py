"""End-to-end tests for `spec-to-pr validate --file PATH`."""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

# Resolve the CLI binary at import time so the tests work regardless of whether
# the package is installed inside a project-local `.venv/` or system-wide (e.g.
# via `pip install -e .` in a container/CI environment).
_CLI_BINARY = shutil.which("spec-to-pr") or ".venv/bin/spec-to-pr"


def _run(file_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_CLI_BINARY, "validate", "--file", file_path],
        capture_output=True,
        text=True,
    )


def test_validate_file_with_frontmatter(tmp_path: Path) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text(
        textwrap.dedent("""\
            ---
            work_id: SPEC-VALIDATE
            ---
            # Add validate subcommand

            Some spec content here.
        """)
    )

    result = _run(str(spec))

    assert result.returncode == 0, f"Expected exit 0; stderr={result.stderr!r}"
    assert "[OK] file readable" in result.stdout
    assert "[OK] work_id: SPEC-VALIDATE" in result.stdout
    assert "[OK] spec_content:" in result.stdout
    # No WARN line when work_id is present
    assert "[WARN]" not in result.stdout


def test_validate_file_without_frontmatter(tmp_path: Path) -> None:
    spec = tmp_path / "no_fm.md"
    spec.write_text("# A spec without any YAML frontmatter\n\nJust plain content.\n")

    result = _run(str(spec))

    assert result.returncode == 0, f"Expected exit 0; stderr={result.stderr!r}"
    assert "[OK] file readable" in result.stdout
    assert "[WARN] no work_id" in result.stdout
    assert "auto-generate" in result.stdout
    assert "[OK] spec_content:" in result.stdout
    # No [OK] work_id line when there is no frontmatter work_id
    assert "[OK] work_id:" not in result.stdout


def test_validate_missing_file(tmp_path: Path) -> None:
    missing = str(tmp_path / "does_not_exist.md")

    result = _run(missing)

    assert result.returncode == 1, f"Expected exit 1; stdout={result.stdout!r}"
    # Error message must mention the file (written to stderr)
    assert "file not found" in result.stderr or "not found" in result.stderr
