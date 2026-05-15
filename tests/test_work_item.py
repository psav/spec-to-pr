import textwrap
import pytest
from pathlib import Path
from spec_to_pr.models import WorkItem, SourceType


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
