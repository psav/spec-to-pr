from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


def _parse_agent_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a .claude/agents/ markdown file."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip()
    return yaml.safe_load(fm_text) or {}, body


@dataclass
class Persona:
    name: str
    description: str
    responsibilities: list[str] = field(default_factory=list)
    approach: str = ""
    output_format: str = ""
    constraints: list[str] = field(default_factory=list)
    sdk_config: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    def build_system_prompt(self, phase_context: Optional[str] = None) -> str:
        lines = [
            f"You are {self.name} — {self.description}.",
            "",
            "## Responsibilities",
        ]
        for r in self.responsibilities:
            lines.append(f"- {r}")
        if self.approach:
            lines += ["", "## Approach", self.approach]
        if self.output_format:
            lines += ["", "## Output Format", self.output_format]
        if self.constraints:
            lines += ["", "## Constraints"]
            for c in self.constraints:
                lines.append(f"- {c}")
        if phase_context:
            lines += ["", "## Current Context", phase_context]
        return "\n".join(lines)

    def to_sdk_options(self, phase_context: Optional[str] = None) -> dict[str, Any]:
        opts: dict[str, Any] = {**self.sdk_config}
        opts["system"] = self.build_system_prompt(phase_context)
        return opts


class PersonaLoader:
    def __init__(self, agents_path: Path) -> None:
        self.agents_path = Path(agents_path)

    def load(self, name: str) -> Persona:
        path = self.agents_path / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Persona agent file not found: {path}")
        text = path.read_text()
        fm, body = _parse_agent_frontmatter(text)

        responsibilities = fm.get("responsibilities", [])
        if isinstance(responsibilities, str):
            responsibilities = [r.strip() for r in responsibilities.splitlines() if r.strip()]

        constraints = fm.get("constraints", [])
        if isinstance(constraints, str):
            constraints = [c.strip() for c in constraints.splitlines() if c.strip()]

        sdk_config = fm.get("sdk_config", {})
        if not sdk_config:
            sdk_config = {
                "model": fm.get("model", "claude-sonnet-4-6"),
                "max_turns": fm.get("max_turns", 50),
            }

        return Persona(
            name=fm.get("name", name),
            description=fm.get("description", ""),
            responsibilities=responsibilities,
            approach=fm.get("approach", ""),
            output_format=fm.get("output_format", ""),
            constraints=constraints,
            sdk_config=sdk_config,
            body=body,
        )
