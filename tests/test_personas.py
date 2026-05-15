import textwrap
from pathlib import Path
import pytest
from spec_to_pr.personas import PersonaLoader, Persona


DEVELOPER_AGENT = textwrap.dedent("""\
    ---
    name: developer
    description: Implements features and writes tests across repositories
    model: claude-sonnet-4-6
    tools: Read, Edit, Write, Bash, Grep, Glob
    responsibilities:
      - Implement feature code following existing patterns
      - Write and refine E2E tests
    approach: Read the spec thoroughly before writing code.
    output_format: Code changes with passing tests
    constraints:
      - Do not merge PRs
      - Do not teardown environments
    sdk_config:
      model: claude-sonnet-4-6
      max_turns: 100
      permission_mode: acceptEdits
      thinking:
        type: enabled
        budget_tokens: 10000
    ---
    Developer persona body text.
""")


def _write_agent(tmp_path, filename, content):
    (tmp_path / filename).write_text(content)


def test_load_persona(tmp_path):
    _write_agent(tmp_path, "developer.md", DEVELOPER_AGENT)
    loader = PersonaLoader(agents_path=tmp_path)
    persona = loader.load("developer")
    assert persona.name == "developer"
    assert persona.description == "Implements features and writes tests across repositories"
    assert persona.sdk_config["model"] == "claude-sonnet-4-6"
    assert persona.sdk_config["max_turns"] == 100
    assert "Do not merge PRs" in persona.constraints


def test_persona_not_found(tmp_path):
    loader = PersonaLoader(agents_path=tmp_path)
    with pytest.raises(FileNotFoundError):
        loader.load("nonexistent")


def test_persona_system_prompt(tmp_path):
    _write_agent(tmp_path, "developer.md", DEVELOPER_AGENT)
    loader = PersonaLoader(agents_path=tmp_path)
    persona = loader.load("developer")
    prompt = persona.build_system_prompt()
    assert "developer" in prompt
    assert "Implements features" in prompt
    assert "Do not merge PRs" in prompt


def test_persona_to_sdk_options(tmp_path):
    _write_agent(tmp_path, "developer.md", DEVELOPER_AGENT)
    loader = PersonaLoader(agents_path=tmp_path)
    persona = loader.load("developer")
    options = persona.to_sdk_options()
    assert options["model"] == "claude-sonnet-4-6"
    assert options["max_turns"] == 100
    assert "system" in options
    assert "developer" in options["system"]
