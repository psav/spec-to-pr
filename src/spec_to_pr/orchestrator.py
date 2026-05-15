from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from spec_to_pr.models import (
    CircuitBreaker,
    DebugMemoryEntry,
    E2EResults,
    OrchestratorSession,
    Phase,
    WorkItem,
)
from spec_to_pr.models.phase_context import DebugOutcome, EphemeralEnv, FailurePhase, PhaseContext
from spec_to_pr.agent_runner import AgentRunner
from spec_to_pr.personas import PersonaLoader
from spec_to_pr.state_machine import StateMachine
from spec_to_pr.storage import FileStorage

log = logging.getLogger(__name__)


@dataclass
class Config:
    storage_path: Path = field(default_factory=lambda: Path(".spec-to-pr/sessions"))
    agents_path: Path = field(default_factory=lambda: Path(".claude/agents"))
    conversations_path: Path = field(default_factory=lambda: Path("conversations"))
    project_docs_path: Path | None = None
    max_attempts: int = 3
    workspace: Path = field(default_factory=Path.cwd)
    skip_deploy: bool = False


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state_machine = StateMachine()
        self.storage = FileStorage(config.storage_path)
        self.persona_loader = PersonaLoader(config.agents_path)
        self._circuit_breaker: Optional[CircuitBreaker] = None
        self._project_docs: Optional[str] = None
        self._load_project_docs()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, work_item: WorkItem, dry_run: bool = False) -> OrchestratorSession:
        session = OrchestratorSession.new(work_item, dry_run=dry_run, max_attempts=self.config.max_attempts)
        self._circuit_breaker = CircuitBreaker(max_attempts=self.config.max_attempts)
        self.storage.save_session(session)
        return self._run_loop(session)

    def resume(self, work_id: str) -> OrchestratorSession:
        session = self.storage.load_session(work_id)
        if session is None:
            raise ValueError(f"No session found for work_id {work_id!r}")
        if session.is_terminal:
            log.info("Session %s is already in terminal phase %s", work_id, session.current_phase)
            return session
        entries = self.storage.load_debug_entries(work_id)
        self._circuit_breaker = CircuitBreaker(max_attempts=session.max_attempts)
        for e in entries:
            self._circuit_breaker.record_attempt(e.error_fingerprint, self._progress_score(e))
        return self._run_loop(session)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run_loop(self, session: OrchestratorSession) -> OrchestratorSession:
        while not session.is_terminal:
            log.info("[%s] phase=%s attempt=%d", session.work_item.work_id, session.current_phase.value, session.attempt_number)
            match session.current_phase:
                case Phase.SPEC_INGESTION:
                    self._ingest_spec(session)
                case Phase.DRY_RUN_REVIEW:
                    self._dry_run_review(session)
                case Phase.IMPLEMENTATION:
                    self._run_implementation_team(session)
                case Phase.DEPLOYMENT:
                    self._deploy(session)
                case Phase.E2E_EXECUTION:
                    self._run_e2e(session)
                case Phase.DEBUG:
                    self._debug(session)
                case Phase.CIRCUIT_BREAKER_CHECK:
                    self._check_circuit_breaker(session)
                case Phase.PR_SUBMISSION:
                    self._submit_prs(session)
            self.storage.save_session(session)
        return session

    # ------------------------------------------------------------------
    # Phase handlers
    # ------------------------------------------------------------------

    def _ingest_spec(self, session: OrchestratorSession) -> None:
        work_item = session.work_item
        if not work_item.spec_content:
            log.error("No spec content for %s", work_item.work_id)
            raise ValueError(f"work_id {work_item.work_id!r} has no spec content")
        log.info("Spec ingested (%d chars)", len(work_item.spec_content))
        self.state_machine.transition(session, spec_valid=True, dry_run=session.dry_run)

    def _dry_run_review(self, session: OrchestratorSession) -> None:
        print("\n=== DRY RUN REVIEW ===")
        print(f"Work ID : {session.work_item.work_id}")
        print(f"Spec    : {len(session.work_item.spec_content)} chars")
        print("\nImplementation plan would be generated and deployed if you proceed.")
        answer = input("Approve and continue? [y/N] ").strip().lower()
        self.state_machine.transition(session, human_approved=(answer == "y"))

    def _run_implementation_team(self, session: OrchestratorSession) -> None:
        """Spawn Claude SDK agent sessions for implementation work."""
        log.info("Running implementation team (attempt %d)", session.attempt_number)
        try:
            self._run_claude_agent("developer", session)

            # Commit changes and track them for PR creation
            self._commit_and_track_changes(session)

            if self.config.skip_deploy:
                log.info("skip_deploy=True — jumping straight to PR submission")
                session.current_phase = Phase.PR_SUBMISSION
                return

            # Infer if testing is needed based on changes
            if not self._should_run_tests(session):
                log.info("Claude inference: testing not needed — jumping to PR submission")
                session.current_phase = Phase.PR_SUBMISSION
                return

            self.state_machine.transition(session, implementation_complete=True)
        except Exception as exc:
            log.error("Implementation failed: %s", exc)
            # Record a circuit breaker attempt so the breaker can trip on repeated failures
            assert self._circuit_breaker is not None
            fingerprint = hashlib.sha256(str(exc).encode()).hexdigest()[:12]
            self._circuit_breaker.record_attempt(fingerprint, 0.0)
            self.state_machine.transition(session, implementation_complete=False)

    def _deploy(self, session: OrchestratorSession) -> None:
        log.info("Deploying to ephemeral environment")
        ok = self._run_make("ephemeral-provision")
        self.state_machine.transition(session, deployment_successful=ok)

    def _run_e2e(self, session: OrchestratorSession) -> None:
        log.info("Running e2e tests")
        ok = self._run_make("ephemeral-e2e")
        self.state_machine.transition(session, tests_passed=ok)

    def _debug(self, session: OrchestratorSession) -> None:
        log.info("Entering debug phase for attempt %d", session.attempt_number)
        previous = self.storage.load_debug_entries(session.work_item.work_id)
        try:
            findings = self._run_claude_agent_debug("developer", session, previous)
        except Exception as exc:
            log.error("Debug agent failed: %s", exc)
            findings = [f"Debug agent error: {exc}"]

        fingerprint = hashlib.sha256(("\n".join(findings)).encode()).hexdigest()[:12]
        progress = self._estimate_progress(session)
        entry = DebugMemoryEntry(
            attempt_number=session.attempt_number,
            timestamp=datetime.now(timezone.utc),
            phase_at_failure=FailurePhase.E2E_EXECUTION,
            error_summary=findings[0] if findings else "unknown error",
            error_fingerprint=fingerprint,
            debug_findings=findings,
        )
        self.storage.save_debug_entry(session.work_item.work_id, entry)
        assert self._circuit_breaker is not None
        self._circuit_breaker.record_attempt(fingerprint, progress)
        self.state_machine.transition(session)

    def _check_circuit_breaker(self, session: OrchestratorSession) -> None:
        assert self._circuit_breaker is not None
        tripped = self._circuit_breaker.tripped
        if tripped:
            log.warning("Circuit breaker tripped: %s", self._circuit_breaker.trip_reason)
        self.state_machine.transition(session, breaker_tripped=tripped)

    def _submit_prs(self, session: OrchestratorSession) -> None:
        log.info("Submitting PRs for %d repos", len(session.repos))
        for repo in session.repos:
            if repo.status in ("committed",) and repo.pr_url is None:
                pr_url = self._create_pr(repo.repo_name, repo.branch, session)
                if pr_url:
                    repo.pr_url = pr_url
                    repo.status = "pr_created"
        self.state_machine.transition(session, prs_created=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _commit_and_track_changes(self, session: OrchestratorSession) -> None:
        """Detect git changes, create branch, commit, and track in session.repos."""
        from spec_to_pr.models.session import RepoState

        # Check for changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=self.config.workspace,
        )

        if result.returncode != 0 or not result.stdout.strip():
            log.info("No git changes detected, skipping commit")
            return

        changed_files = [line.split()[-1] for line in result.stdout.strip().split('\n')]
        log.info("Detected %d changed files: %s", len(changed_files), changed_files[:5])

        # Get current repo info
        repo_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=self.config.workspace,
        ).stdout.strip()

        # Extract repo name from URL (e.g., org/repo)
        repo_name = repo_url.replace("https://github.com/", "").replace(".git", "")

        # Create branch
        branch_name = f"spec-to-pr/{session.work_item.work_id}"
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True,
            cwd=self.config.workspace,
        )
        log.info("Created branch: %s", branch_name)

        # Stage and commit changes
        subprocess.run(
            ["git", "add"] + changed_files,
            cwd=self.config.workspace,
        )

        commit_msg = f"[{session.work_item.work_id}] Automated implementation\n\nSpec-to-pr automated changes for work item {session.work_item.work_id}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True,
            cwd=self.config.workspace,
        )
        log.info("Committed changes: %s", commit_msg.split('\n')[0])

        # Track in session
        repo_state = RepoState(
            repo_name=repo_name,
            repo_url=repo_url,
            workspace_path=str(self.config.workspace),
            branch=branch_name,
            changes=changed_files,
            status="committed",
        )
        session.repos.append(repo_state)
        log.info("Tracked repo state: %s on branch %s", repo_name, branch_name)

    def _load_project_docs(self) -> None:
        """Load project documentation (CLAUDE.md) to provide environment context."""
        try:
            # Try explicit path first
            if self.config.project_docs_path and self.config.project_docs_path.exists():
                doc_path = self.config.project_docs_path
            else:
                # Auto-discover CLAUDE.md in workspace
                doc_path = self.config.workspace / "CLAUDE.md"
                if not doc_path.exists():
                    log.debug("No CLAUDE.md found at %s", doc_path)
                    return

            self._project_docs = doc_path.read_text()
            log.info("Loaded project documentation from %s (%d chars)", doc_path, len(self._project_docs))
        except Exception as exc:
            log.warning("Failed to load project documentation: %s", exc)

    def _should_run_tests(self, session: OrchestratorSession) -> bool:
        """Use Claude to infer whether testing is needed based on the changes."""
        try:
            # Get git diff of changes
            result = subprocess.run(
                ["git", "diff", "--name-status", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self.config.workspace,
                timeout=10,
            )
            if result.returncode != 0:
                log.warning("Failed to get git diff, assuming tests needed")
                return True

            changed_files = result.stdout.strip()
            if not changed_files:
                log.info("No file changes detected, skipping tests")
                return False

            # Ask Claude to infer if tests are needed
            runner = AgentRunner(
                workspace=self.config.workspace,
                model="claude-sonnet-4-6",
                max_turns=1,
            )

            task = f"""Analyze these file changes and determine if ephemeral environment testing is needed.

Changed files:
{changed_files}

Context from the spec:
{session.work_item.spec_content}

Guidelines:
- Documentation-only changes (.md files) typically don't need testing
- Comments-only changes don't need testing
- Log message changes typically don't need testing
- Infrastructure changes (terraform/, argocd/) need testing
- Code changes that affect runtime behavior need testing
- Test file changes need testing

Respond with EXACTLY one of:
- "SKIP_TESTS: <reason>" if testing is not needed
- "RUN_TESTS: <reason>" if testing is needed

Keep the reason brief (one sentence)."""

            response = runner.run(
                system_prompt="You are a software engineer deciding if changes need integration testing.",
                task=task,
            )

            # Parse response
            response = response.strip()
            if response.startswith("SKIP_TESTS:"):
                reason = response.replace("SKIP_TESTS:", "").strip()
                log.info("Claude inference: skip tests - %s", reason)
                return False
            elif response.startswith("RUN_TESTS:"):
                reason = response.replace("RUN_TESTS:", "").strip()
                log.info("Claude inference: run tests - %s", reason)
                return True
            else:
                log.warning("Unexpected response from Claude: %s, defaulting to run tests", response[:100])
                return True

        except Exception as exc:
            log.warning("Failed to infer test requirement: %s, defaulting to run tests", exc)
            return True

    def _run_make(self, target: str, **kwargs) -> bool:
        result = subprocess.run(
            ["make", target],
            capture_output=True,
            text=True,
            cwd=self.config.workspace,
            **kwargs,
        )
        if result.returncode != 0:
            log.error("make %s failed:\n%s", target, result.stderr[-2000:])
        return result.returncode == 0

    def _make_runner(self, persona_name: str) -> tuple[AgentRunner, str]:
        """Return an AgentRunner configured with the given persona's SDK settings."""
        try:
            persona = self.persona_loader.load(persona_name)
            sdk_cfg = persona.sdk_config
            runner = AgentRunner(
                workspace=self.config.workspace,
                model=sdk_cfg.get("model", "claude-sonnet-4-6"),
                max_turns=sdk_cfg.get("max_turns", 50),
                conversations_dir=self.config.conversations_path,
            )
            system_prompt = persona.build_system_prompt()
        except FileNotFoundError:
            log.warning("Persona %r not found — using defaults", persona_name)
            runner = AgentRunner(
                workspace=self.config.workspace,
                conversations_dir=self.config.conversations_path,
            )
            system_prompt = "You are a software developer. Implement the requested changes."

        # Append project documentation if available
        if self._project_docs:
            system_prompt += f"\n\n# Project Documentation\n\n{self._project_docs}"

        return runner, system_prompt

    def _run_claude_agent(self, persona_name: str, session: OrchestratorSession) -> None:
        """Run a Claude SDK agent session for implementation work."""
        runner, system_prompt = self._make_runner(persona_name)
        task = (
            f"Work ID: {session.work_item.work_id}\n"
            f"Attempt: {session.attempt_number}\n\n"
            f"{session.work_item.spec_content}\n\n"
            "Implement all changes described above. "
            "When done, respond with 'Implementation complete.'"
        )
        result = runner.run(
            system_prompt=system_prompt,
            task=task,
            work_id=session.work_item.work_id
        )
        log.info("Implementation agent finished. Summary: %s", result[:200])

    def _run_claude_agent_debug(
        self, persona_name: str, session: OrchestratorSession, previous: list
    ) -> list[str]:
        """Run a debug agent session and return a list of findings."""
        runner, system_prompt = self._make_runner(persona_name)
        prev_ctx = "\n".join(
            f"Attempt {e.attempt_number}: {e.error_summary}" for e in previous
        )
        task = (
            f"Debug failure for work item {session.work_item.work_id}.\n\n"
            f"Previous attempts:\n{prev_ctx}\n\n"
            "Investigate logs, pod state, and recent changes. "
            "Return a bullet-point list of findings and hypotheses."
        )
        response = runner.run(
            system_prompt=system_prompt,
            task=task,
            work_id=f"{session.work_item.work_id}-debug"
        )
        return [
            line.lstrip("-• ").strip()
            for line in response.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _create_pr(self, repo_name: str, branch: str, session: OrchestratorSession) -> Optional[str]:
        # Detect the fork remote to push to (not upstream origin)
        remote_result = subprocess.run(
            ["git", "remote", "-v"],
            capture_output=True,
            text=True,
            cwd=self.config.workspace,
        )

        # Parse remotes and look for a fork
        remotes = {}
        for line in remote_result.stdout.split('\n'):
            parts = line.split()
            if len(parts) >= 2:
                remote_name = parts[0]
                remote_url = parts[1]
                if '(push)' in line:  # Only care about push URLs
                    remotes[remote_name] = remote_url

        # Strategy: prefer any remote that isn't 'origin', or has 'bot'/'fork' in name
        remote_to_use = None
        for remote_name, remote_url in remotes.items():
            if remote_name != 'origin':
                remote_to_use = remote_name
                break
            if 'bot' in remote_name.lower() or 'fork' in remote_name.lower():
                remote_to_use = remote_name
                break

        if not remote_to_use:
            remote_to_use = 'origin'  # Fallback

        # Push branch to fork
        log.info("Pushing branch %s to %s (%s)", branch, remote_to_use, remotes.get(remote_to_use, 'unknown'))
        push_result = subprocess.run(
            ["git", "push", "-u", remote_to_use, branch],
            capture_output=True,
            text=True,
            cwd=self.config.workspace,
        )
        if push_result.returncode != 0:
            log.error("git push to %s failed: %s", remote_to_use, push_result.stderr)
            return None

        # Create PR
        log.info("Creating PR for %s on branch %s", repo_name, branch)
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--title", f"[{session.work_item.work_id}] Automated implementation",
                "--body", (
                    f"Automated PR for {session.work_item.work_id}\n\n"
                    f"Attempt: {session.attempt_number}\n\n"
                    f"Generated by spec-to-pr"
                ),
                "--head", branch,
            ],
            capture_output=True,
            text=True,
            cwd=self.config.workspace,
        )
        if result.returncode == 0:
            pr_url = result.stdout.strip()
            log.info("Created PR: %s", pr_url)
            return pr_url
        log.error("gh pr create failed for %s: %s", repo_name, result.stderr)
        return None

    def _estimate_progress(self, session: OrchestratorSession) -> float:
        return max(0.0, 1.0 - (session.attempt_number / session.max_attempts))

    @staticmethod
    def _progress_score(entry: DebugMemoryEntry) -> float:
        tr = entry.test_results
        if tr.total == 0:
            return 0.0
        return tr.passed / tr.total
