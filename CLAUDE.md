# spec-to-pr

Autonomous spec-to-pull-request orchestrator. Ingests a feature spec (JIRA ticket, markdown file, or inline text), spawns Claude Agent SDK sessions to implement code and E2E tests, deploys to an ephemeral environment, runs tests with a debug/retry loop, and creates PRs on success. A circuit breaker escalates to humans after repeated failures.

## Quick reference

```bash
make test          # Run pytest via uv
make build         # Build container image
./run-spec.sh <spec.md>  # Run orchestrator in container
```

## Project structure

- `src/spec_to_pr/` — Python package (Python 3.11+)
  - `cli.py` — argparse entry point: `run`, `status`, `resume`, `validate`
  - `orchestrator.py` — core phase-based orchestrator
  - `state_machine.py` — deterministic phase transitions
  - `agent_runner.py` — Claude SDK tool-use loop wrapper
  - `personas.py` — loads persona definitions from `.claude/agents/`
  - `storage.py` — YAML-backed session/debug persistence
  - `models/` — data models (WorkItem, Session, CircuitBreaker, PhaseContext)
- `tests/` — pytest suite, mostly subprocess-based CLI invocations
- `.spec/` — spec-driven development artifacts (requirements, implementation plans)
- `Containerfile` — UBI9 Python 3.11 image with git, gh, uv

## Architecture

**Phase state machine:**
`SPEC_INGESTION → IMPLEMENTATION → DEPLOYMENT → E2E_EXECUTION → PR_SUBMISSION → COMPLETE`

Failed phases route through `DEBUG → CIRCUIT_BREAKER_CHECK`, which either retries (back to IMPLEMENTATION) or escalates to `HUMAN_ESCALATION`. `DRY_RUN_REVIEW` and `ABORTED` are alternate terminal states.

**Circuit breaker** trips on: max attempts reached (default 3), repeated error fingerprint, or no progress (<5% delta).

## Dependencies

Core: `anthropic>=0.50.0`, `pyyaml>=6.0`
Dev: `pytest>=7.0.0`

## Environment variables

- `GITHUB_TOKEN` — for PR creation via `gh`
- `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION` — Vertex AI Claude access
- `AWS_PROFILE` — AWS credentials for ephemeral environments

## Conventions

- Tests use subprocess to invoke the installed CLI for E2E validation
- Session state persists as YAML in `.spec-to-pr/sessions/{work_id}/`
- Persona files live in `.claude/agents/` with YAML frontmatter + markdown body
- Spec files use markdown with optional YAML frontmatter (work_id, title, etc.)
