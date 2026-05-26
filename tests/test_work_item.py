import textwrap
import pytest
from pathlib import Path
from spec_to_pr.models import WorkItem, SourceType
from spec_to_pr.models.work_item import (
    _parse_spec_sections,
    _spec_for_developer,
    _spec_section,
    _COMMITTER_HEADINGS,
    _PR_SUBMITTER_HEADINGS,
)


def test_work_item_from_jira():
    item = WorkItem.from_jira("ROSAENG-1234")
    assert item.work_id == "ROSAENG-1234"
    assert item.source_type == SourceType.JIRA
    assert item.source_ref == "ROSAENG-1234"


def test_work_item_from_jira_strips_whitespace():
    item = WorkItem.from_jira("  ROSAENG-999  ")
    assert item.work_id == "ROSAENG-999"


def test_work_item_from_file_with_frontmatter(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(textwrap.dedent("""\
        ---
        work_id: SPEC-0001
        ---
        # My feature
    """))
    item = WorkItem.from_file(str(spec))
    assert item.work_id == "SPEC-0001"
    assert item.source_type == SourceType.FILE
    assert item.source_ref == str(spec)
    assert "# My feature" in item.spec_content


def test_work_item_from_file_without_frontmatter(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Add health check\n")
    item = WorkItem.from_file(str(spec))
    assert item.work_id.startswith("SPEC-")
    assert item.source_type == SourceType.FILE


def test_work_item_from_file_not_found():
    with pytest.raises(FileNotFoundError):
        WorkItem.from_file("/nonexistent/path/spec.md")


def test_work_item_from_inline():
    item = WorkItem.from_inline("Add health check endpoint")
    assert item.work_id.startswith("SPEC-")
    assert item.source_type == SourceType.INLINE
    assert item.source_ref == "inline"
    assert item.spec_content == "Add health check endpoint"


def test_work_item_generated_ids_are_unique():
    a = WorkItem.from_inline("feature A")
    b = WorkItem.from_inline("feature B")
    assert a.work_id != b.work_id


# ---------------------------------------------------------------------------
# Section parsing helpers
# ---------------------------------------------------------------------------

SECTIONED_SPEC = textwrap.dedent("""\
    ---
    work_id: TEST-1
    ---
    # Title

    ## Context
    Some background.

    ## Implementation
    Do the work.

    ## Committer
    Expect uncommitted files.

    ## PR Submission
    Target main, title: Fix the thing.
""")


def test_parse_spec_sections_returns_all_headings():
    sections = _parse_spec_sections(SECTIONED_SPEC)
    assert set(sections) == {"context", "implementation", "committer", "pr submission"}


def test_parse_spec_sections_captures_content():
    sections = _parse_spec_sections(SECTIONED_SPEC)
    assert "Do the work." in sections["implementation"]
    assert "Expect uncommitted files." in sections["committer"]


def test_spec_for_developer_strips_committer_and_pr_sections():
    result = _spec_for_developer(SECTIONED_SPEC)
    assert "## Committer" not in result
    assert "## PR Submission" not in result
    assert "Expect uncommitted files" not in result
    assert "Target main" not in result


def test_spec_for_developer_keeps_implementation_and_context():
    result = _spec_for_developer(SECTIONED_SPEC)
    assert "## Implementation" in result
    assert "## Context" in result
    assert "Do the work." in result


def test_spec_for_developer_preserves_frontmatter():
    result = _spec_for_developer(SECTIONED_SPEC)
    assert "work_id: TEST-1" in result


def test_spec_for_developer_no_sections_returns_full_spec():
    plain = "# Title\n\nJust some text, no H2 sections."
    assert _spec_for_developer(plain) == plain


def test_spec_for_developer_no_agent_sections_returns_full_spec():
    spec = textwrap.dedent("""\
        ---
        work_id: X
        ---
        ## Context
        foo

        ## Implementation
        bar
    """)
    result = _spec_for_developer(spec)
    assert "## Context" in result
    assert "## Implementation" in result
    assert "foo" in result
    assert "bar" in result


def test_spec_section_finds_committer_alias():
    spec = textwrap.dedent("""\
        ---
        work_id: X
        ---
        ## Committer notes
        Expect files.
    """)
    result = _spec_section(spec, _COMMITTER_HEADINGS)
    assert "Expect files." in result


def test_spec_section_finds_committer_canonical():
    spec = textwrap.dedent("""\
        ---
        work_id: X
        ---
        ## Committer
        Stage everything.
    """)
    result = _spec_section(spec, _COMMITTER_HEADINGS)
    assert "Stage everything." in result


def test_spec_section_finds_pr_submitter_notes_alias():
    spec = textwrap.dedent("""\
        ---
        work_id: X
        ---
        ## PR Submitter notes
        Target main.
    """)
    result = _spec_section(spec, _PR_SUBMITTER_HEADINGS)
    assert "Target main." in result


def test_spec_section_finds_pr_submission_canonical():
    spec = textwrap.dedent("""\
        ---
        work_id: X
        ---
        ## PR Submission
        Force push to bot fork.
    """)
    result = _spec_section(spec, _PR_SUBMITTER_HEADINGS)
    assert "Force push to bot fork." in result


def test_spec_section_returns_empty_when_not_found():
    assert _spec_section(SECTIONED_SPEC, frozenset({"nonexistent heading"})) == ""


def test_spec_for_developer_on_real_review_spec():
    """Regression: pr-506-review.md committer/PR sections must be stripped."""
    import pathlib
    spec_path = pathlib.Path("/workspace/pr-506-review.md")
    if not spec_path.exists():
        pytest.skip("pr-506-review.md not present in /workspace")
    spec = spec_path.read_text()
    result = _spec_for_developer(spec)
    assert "Committer notes" not in result
    assert "PR Submitter notes" not in result
    assert "Implementation guidance" in result
