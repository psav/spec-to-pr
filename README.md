# spec-to-pr

Autonomous spec-to-pull-request orchestrator for the ROSA Regional Platform.

Given a feature specification (JIRA ticket, local markdown file, or inline text), the orchestrator:

1. Ingests the spec
2. Spawns Claude agent sessions to implement code and E2E tests
3. Deploys to an ephemeral environment
4. Runs E2E tests and enters a debug loop on failure
5. Trips a circuit breaker and escalates to a human after repeated failures
6. Creates pull requests across all affected repositories on success

## Usage

```bash
# Run from a JIRA ticket (spec content must be provided separately or fetched)
spec-to-pr run --work-id ROSAENG-1234

# Run from a local spec file
spec-to-pr run --file path/to/spec.md

# Run from inline text
spec-to-pr run --inline "Add health check endpoint to platform API"

# Dry run — plan only, no deployment
spec-to-pr run --file spec.md --dry-run

# Check session status
spec-to-pr status --work-id ROSAENG-1234

# Resume an interrupted session
spec-to-pr resume --work-id ROSAENG-1234
```

## Installation

```bash
pip install -e .
# or with uv
uv pip install -e .
```

Requires Python 3.11+.

## Running tests

```bash
pytest tests/ -v
```

## Container

Build the image with Podman:

```bash
podman build -t spec-to-pr -f spec-to-pr/Containerfile spec-to-pr/
```

Run with mounted workspace and injected credentials:

```bash
podman run --rm \
  -v "$(pwd)":/workspace \
  -e GITHUB_TOKEN="$GITHUB_TOKEN" \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-sa.json \
  -v /path/to/gcp-sa.json:/secrets/gcp-sa.json:ro \
  spec-to-pr run --file /workspace/my-spec.md
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | GitHub token for PR creation |
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes | Path to GCP service account JSON (for Claude via Vertex AI) |
| `AWS_PROFILE` | No | AWS profile for ephemeral env operations (default: `central`) |

## Architecture

```
spec-to-pr/
  src/spec_to_pr/
    cli.py          # argparse entry point (run / status / resume)
    orchestrator.py # deterministic run loop + phase handlers
    state_machine.py# phase transition logic
    storage.py      # file-backed session + debug entry storage
    personas.py     # load .claude/agents/ files → SDK options
    models/
      work_item.py  # WorkItem (jira / file / inline)
      session.py    # OrchestratorSession, RepoState, Phase
      circuit_breaker.py  # CircuitBreaker + TripReason
      phase_context.py    # PhaseContext, DebugMemoryEntry, EphemeralEnv
```

The orchestrator is **deterministic Python** — all phase transitions, circuit breaker evaluation, and persona dispatch are in code, not delegated to an LLM. Claude agent sessions are spawned via `claude --print` for implementation and debug work only.

## Persona configuration

Personas are defined as `.claude/agents/*.md` files with standard Claude Code frontmatter plus an extended `sdk_config` block:

```yaml
---
name: developer
model: claude-sonnet-4-6
sdk_config:
  model: claude-sonnet-4-6
  max_turns: 100
  permission_mode: acceptEdits
  thinking:
    type: enabled
    budget_tokens: 10000
---
```

The `PersonaLoader` reads these files and constructs the system prompt by combining `responsibilities`, `approach`, `output_format`, and `constraints` fields.

## Storage layout

Sessions are stored under `.spec-to-pr/sessions/{work_id}/`:

```
.spec-to-pr/sessions/ROSAENG-1234/
  session.yaml          # current session state
  attempts/
    0.yaml              # debug memory for attempt 0
    1.yaml              # debug memory for attempt 1
```

## Circuit breaker

The circuit breaker trips on:
- **Max attempts reached** — `attempt_count >= max_attempts` (default: 3)
- **Repeated error** — same error fingerprint in two consecutive attempts
- **No progress** — progress score delta < 5% between consecutive attempts

When tripped, the orchestrator transitions to `HUMAN_ESCALATION` and prints a summary of all attempts with their debug findings.
