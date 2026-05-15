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


@dataclass
class WorkItem:
    work_id: str
    source_type: SourceType
    source_ref: str
    spec_content: str = ""

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
        return cls(
            work_id=work_id,
            source_type=SourceType.FILE,
            source_ref=path,
            spec_content=text,
        )

    @classmethod
    def from_inline(cls, text: str) -> WorkItem:
        return cls(
            work_id=_generate_spec_id(),
            source_type=SourceType.INLINE,
            source_ref="inline",
            spec_content=text,
        )
