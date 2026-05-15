# Context: Implementation Plan — Spec-to-PR Agent

## Codebase Analysis Findings

### Python Patterns

- Repo uses `uv` with PEP 723 inline script dependencies (no pyproject.toml)
- Python >=3.13
- Existing Python orchestrator in `ci/ephemeral-provider/` (main.py, orchestrator.py, aws.py, pipeline.py, git.py, yaml_utils.py)
- Scripts run via `uv run --no-cache`

### Agent Format (.claude/agents/)

- YAML frontmatter: name, description, tools, model, color (optional)
- Body: detailed markdown with workflows, examples, context
- Models: sonnet for complex analysis, haiku for lightweight
- Permissions managed globally in settings.json, not per-agent

### Skill/Command Format (.claude/commands/)

- YAML header with description only
- Accept `$ARGUMENTS` placeholder
- Reference `.spec/config.json` for configuration
- No tools declaration — inherit from harness

### Makefile Ephemeral Targets

- All wrap `scripts/dev/ephemeral-env.sh` with env vars (ID, REPO, BRANCH, E2E_REF, E2E_REPO)
- All fetch AWS creds from Vault before running
- State tracked in `.ephemeral-envs` file

### Existing Conventions

- No CONTRIBUTING.md; guidance in Makefile comments and docs/
- Permissions: global allow list in .claude/settings.json, local overrides in settings.local.json
- No agent-specific permissions

## Implementation Plan Q&A

### Q1: Should the orchestrator be a standalone Python package or uv inline scripts?

**Answer:** Standalone package with pyproject.toml. The orchestrator is complex enough to warrant a proper package.

### Q2: Should the ephemeral skill be a Claude Code command or Python module?

**Answer:** Claude Code command (.claude/commands/ephemeral.md) wrapping Make targets. The orchestrator calls Make targets directly via subprocess.

### Q3: Where should persona YAML files live?

**Answer:** In `.claude/agents/` with extended YAML frontmatter. Single source of truth — Claude Code ignores unknown fields, orchestrator reads the extra fields (sdk_config, constraints, memory_directives).

### Q4: Should the state machine use a library or be hand-rolled?

**Answer:** Hand-rolled with match/case. ~10 states, straightforward transitions. SDK subagents handle complex AI work within each phase.

### Q5: Should we use Claude's agents feature?

**Answer:** Hybrid — deterministic Python state machine for the outer loop (phase transitions, circuit breaker), but Claude SDK agent/subagent features within the implementation phase to coordinate the persona team (developer, qa-engineer, etc.).

## Testing Q&A

### Q6: TDD or test-after for orchestrator core?

**Answer:** TDD for core (state machine, circuit breaker). Integration tests (SDK sessions, ephemeral env calls) written after implementation.

### Q7: Mock or real infrastructure for integration tests?

**Answer:** Mock for CI (fast, reliable), real ephemeral environments for validation (separate manual step).
