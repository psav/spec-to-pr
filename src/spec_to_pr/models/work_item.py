from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class SourceType(str, Enum):
    JIRA = "jira"
    FILE = "file"
    INLINE = "inline"
    REVIEW = "review"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) from a markdown string with optional YAML frontmatter."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    import yaml
    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip()
    return yaml.safe_load(fm_text) or {}, body


def _generate_spec_id() -> str:
    short = uuid.uuid4().hex[:6].upper()
    return f"SPEC-{short}"


def _extract_title_from_body(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    return first_line[:80]


# Headings (lowercase) that belong exclusively to specific pipeline agents.
_COMMITTER_HEADINGS: frozenset[str] = frozenset({"committer", "committer notes"})
_PR_SUBMITTER_HEADINGS: frozenset[str] = frozenset({"pr submission", "pr submitter", "pr submitter notes"})
_DEVELOPER_EXCLUDED: frozenset[str] = _COMMITTER_HEADINGS | _PR_SUBMITTER_HEADINGS


def _parse_spec_sections(spec_content: str) -> dict[str, str]:
    """Return {lowercase_h2_heading: body_text} for every H2 section in the spec body."""
    _, body = _parse_frontmatter(spec_content)
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^## (.+)$", line.rstrip())
        if m:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = m.group(1).strip().lower()
            lines = []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def _spec_for_developer(spec_content: str) -> str:
    """Return spec_content with committer and PR-submitter sections removed.

    Falls back to the full spec if the spec has no recognised agent-specific H2
    sections (backward compat with old-format specs).
    """
    sections = _parse_spec_sections(spec_content)
    if not sections or not (sections.keys() & _DEVELOPER_EXCLUDED):
        return spec_content

    _, body = _parse_frontmatter(spec_content)
    fm_end = spec_content.find("\n---", 3) + 4 if spec_content.startswith("---") else 0
    frontmatter = spec_content[:fm_end] if fm_end else ""

    out_lines: list[str] = []
    skip = False
    for line in body.splitlines():
        m = re.match(r"^## (.+)$", line.rstrip())
        if m:
            skip = m.group(1).strip().lower() in _DEVELOPER_EXCLUDED
        if not skip:
            out_lines.append(line)

    body_out = "\n".join(out_lines).rstrip()
    return (frontmatter + "\n" + body_out).strip() if frontmatter else body_out


def _spec_section(spec_content: str, headings: frozenset[str]) -> str:
    """Return the content of the first matching H2 section, or empty string."""
    sections = _parse_spec_sections(spec_content)
    for heading in headings:
        if heading in sections:
            return sections[heading]
    return ""


@dataclass
class WorkItem:
    work_id: str
    source_type: SourceType
    source_ref: str
    spec_content: str = ""
    title: str = ""

    @classmethod
    def from_jira(cls, jira_id: str) -> WorkItem:
        jira_id = jira_id.strip()
        return cls(work_id=jira_id, source_type=SourceType.JIRA, source_ref=jira_id)

    @classmethod
    def from_file(cls, path: str) -> WorkItem:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Spec file not found: {path}")
        text = p.read_text()
        fm, body = _parse_frontmatter(text)
        work_id = fm.get("work_id") or _generate_spec_id()
        title = fm.get("title") or _extract_title_from_body(body)
        return cls(
            work_id=work_id,
            source_type=SourceType.FILE,
            source_ref=path,
            spec_content=text,
            title=title,
        )

    @classmethod
    def from_inline(cls, text: str) -> WorkItem:
        fm, body = _parse_frontmatter(text)
        title = fm.get("title") or _extract_title_from_body(body)
        return cls(
            work_id=_generate_spec_id(),
            source_type=SourceType.INLINE,
            source_ref="inline",
            spec_content=text,
            title=title,
        )
