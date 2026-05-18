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
        self._context_log_init(session)
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
        cwd = Path(session.repos[0].workspace_path) if session.repos else self.config.workspace
        log.info("Running make ephemeral-provision in %s", cwd)
        ok = self._run_make("ephemeral-provision", cwd=cwd)
        if not ok:
            self._context_log_append(
                session,
                f"Deployment FAILED — attempt {session.attempt_number}",
                f"- `make ephemeral-provision` failed in `{cwd}`\n"
                f"- Entering debug phase",
            )
        self.state_machine.transition(session, deployment_successful=ok)

    def _run_e2e(self, session: OrchestratorSession) -> None:
        log.info("Running e2e tests")
        cwd = Path(session.repos[0].workspace_path) if session.repos else self.config.workspace
        ok = self._run_make("ephemeral-e2e", cwd=cwd)
        if not ok:
            self._context_log_append(
                session,
                f"E2E FAILED — attempt {session.attempt_number}",
                f"- `make ephemeral-e2e` failed in `{cwd}`\n"
                f"- Entering debug phase",
            )
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

        # Build list of repos that need PR creation
        repos_to_process = [
            repo for repo in session.repos
            if repo.status in ("committed",) and repo.pr_url is None
        ]

        if not repos_to_process:
            log.info("No repos need PR creation")
            self.state_machine.transition(session, prs_created=True)
            return

        # Use pr-submitter agent to push and create PRs
        try:
            self._run_pr_submission_agent(repos_to_process, session)
            # Mark all as pr_created (agent should have handled them)
            for repo in repos_to_process:
                if repo.pr_url:
                    repo.status = "pr_created"
            self.state_machine.transition(session, prs_created=True)
        except Exception as exc:
            log.error("PR submission failed: %s", exc)
            self.state_machine.transition(session, prs_created=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _commit_and_track_changes(self, session: OrchestratorSession) -> None:
        """Discover repos with local work under the workspace, commit, and track them.

        Scans all direct subdirectories of the workspace for git repos that have
        either uncommitted changes or local commits not yet pushed to any remote.
        Runs the committer agent once per repo found. Works naturally for multi-repo
        specs where the developer clones several repos side-by-side.
        """
        from spec_to_pr.models.session import RepoState
        import re

        branch_name = f"spec-to-pr/{session.work_item.work_id}"

        repos_with_work = self._find_repos_with_local_work()
        if not repos_with_work:
            log.info("No repos with local work found under %s", self.config.workspace)
            return

        log.info("Found %d repo(s) with local work: %s",
                 len(repos_with_work), [p.name for p in repos_with_work])

        for repo_path in repos_with_work:
            url_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, cwd=repo_path,
            )
            if url_result.returncode != 0:
                log.warning("No origin remote in %s, skipping", repo_path)
                continue

            repo_url = url_result.stdout.strip()
            repo_name = (
                repo_url
                .replace("https://github.com/", "")
                .replace("git@github.com:", "")
                .removesuffix(".git")
            )

            runner, system_prompt = self._make_runner("committer", session)
            task = (
                f"Work ID: {session.work_item.work_id}\n"
                f"Branch: {branch_name}\n"
                f"Repository path: {repo_path}\n\n"
                f"Commit any outstanding changes in the repository at `{repo_path}`.\n\n"
                f"1. `cd {repo_path}`\n"
                f"2. Run `git status --porcelain` to check for uncommitted changes.\n"
                f"   - If there ARE uncommitted changes: proceed to step 3.\n"
                f"   - If there are NO uncommitted changes, the developer already committed. "
                f"Run `git log --oneline --not --remotes` to confirm there are unpushed commits, "
                f"then skip to step 6 and list those files.\n"
                f"3. Filter out spec-to-pr metadata — never stage:\n"
                f"   - .spec-to-pr/ directory\n"
                f"   - conversations/ directory\n"
                f"   - Spec *.md files in the workspace root\n"
                f"4. Create or switch to branch `{branch_name}`: "
                f"`git checkout -b {branch_name}` or `git checkout {branch_name}`.\n"
                f"5. Stage only implementation files and commit:\n"
                f"   [{session.work_item.work_id}] <brief description of what changed>\n\n"
                f"   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>\n"
                f"6. Report committed files as:\n"
                f"   Committed files: <file1>, <file2>, ...\n\n"
                f"Respond with 'Commit complete.' when done."
            )

            result = runner.run(
                system_prompt=system_prompt,
                task=task,
                work_id=f"{session.work_item.work_id}-commit-{repo_path.name}",
            )
            log.info("Committer for %s: %s", repo_path.name, result[:300])

            committed_files_match = re.search(r'Committed files:\s*(.+?)(?:\n|$)', result)
            committed_files = []
            if committed_files_match:
                files_str = committed_files_match.group(1)
                committed_files = [f.strip().strip("`") for f in files_str.split(",") if f.strip()]

            if not committed_files:
                diff_result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                    capture_output=True, text=True, cwd=repo_path,
                )
                if diff_result.returncode == 0:
                    committed_files = [f.strip() for f in diff_result.stdout.strip().split("\n") if f.strip()]

            if not committed_files:
                log.warning("No committed files detected for %s, skipping", repo_path.name)
                continue

            repo_state = RepoState(
                repo_name=repo_name,
                repo_url=repo_url,
                workspace_path=str(repo_path),
                branch=branch_name,
                changes=committed_files,
                status="committed",
            )
            session.repos.append(repo_state)
            log.info("Tracked %s on %s with %d files", repo_name, branch_name, len(committed_files))

    def _find_repos_with_local_work(self) -> list[Path]:
        """Return git repos under workspace that have uncommitted changes or unpushed commits."""
        found = []
        for path in sorted(self.config.workspace.iterdir()):
            if not path.is_dir() or path.name == "spec-to-pr":
                continue
            if not (path / ".git").exists():
                continue
            # Uncommitted changes
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=path,
            )
            if status.returncode == 0 and status.stdout.strip():
                found.append(path)
                continue
            # Local commits not on any remote
            ahead = subprocess.run(
                ["git", "log", "--oneline", "--not", "--remotes"],
                capture_output=True, text=True, cwd=path,
            )
            if ahead.returncode == 0 and ahead.stdout.strip():
                found.append(path)
        return found

    # ------------------------------------------------------------------
    # Shared context log — one markdown file per work item, readable by all agents
    # ------------------------------------------------------------------

    def _context_log_path(self, session: OrchestratorSession) -> Path:
        path = self.config.storage_path / session.work_item.work_id / "context.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _context_log_init(self, session: OrchestratorSession) -> None:
        path = self._context_log_path(session)
        if path.exists():
            return  # preserve across resume
        now = datetime.now(timezone.utc).isoformat()
        path.write_text(
            f"# Context log: {session.work_item.work_id}\n\n"
            f"Session: {session.session_id}\n"
            f"Started: {now}\n\n"
            f"## Spec\n\n"
            f"{session.work_item.spec_content}\n\n"
            f"---\n\n"
            f"*Agents: read this file before starting. Append a brief summary when done.*\n\n"
        )

    def _context_log_append(self, session: OrchestratorSession, heading: str, body: str) -> None:
        path = self._context_log_path(session)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(path, "a") as f:
            f.write(f"## {heading} — {now}\n\n{body.strip()}\n\n")

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
            # Prefer committed file list from session state (post-commit, git diff HEAD is empty)
            if session.repos and session.repos[0].changes:
                changed_files = "\n".join(f"M\t{f}" for f in session.repos[0].changes)
                log.info("Using %d committed files from session state", len(session.repos[0].changes))
            else:
                # Fall back to git diff for uncommitted changes
                primary_cwd = (
                    Path(session.repos[0].workspace_path) if session.repos
                    else self.config.workspace
                )
                result = subprocess.run(
                    ["git", "diff", "--name-status", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=primary_cwd,
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

    def _run_make(self, target: str, cwd: Path | None = None) -> bool:
        run_cwd = cwd or self.config.workspace
        result = subprocess.run(
            ["make", target],
            capture_output=True,
            text=True,
            cwd=run_cwd,
        )
        if result.returncode != 0:
            log.error("make %s failed (in %s):\n%s", target, run_cwd, result.stderr[-2000:])
        return result.returncode == 0

    def _make_runner(self, persona_name: str, session: OrchestratorSession | None = None) -> tuple[AgentRunner, str]:
        """Return an AgentRunner configured with the given persona's SDK settings."""
        run_id = session.session_id[:8] if session else None
        try:
            persona = self.persona_loader.load(persona_name)
            sdk_cfg = persona.sdk_config
            runner = AgentRunner(
                workspace=self.config.workspace,
                model=sdk_cfg.get("model", "claude-sonnet-4-6"),
                max_turns=sdk_cfg.get("max_turns", 50),
                conversations_dir=self.config.conversations_path,
                run_id=run_id,
            )
            system_prompt = persona.build_system_prompt()
        except FileNotFoundError:
            log.warning("Persona %r not found — using defaults", persona_name)
            runner = AgentRunner(
                workspace=self.config.workspace,
                conversations_dir=self.config.conversations_path,
                run_id=run_id,
            )
            system_prompt = "You are a software developer. Implement the requested changes."

        # Append project documentation if available
        if self._project_docs:
            system_prompt += f"\n\n# Project Documentation\n\n{self._project_docs}"

        return runner, system_prompt

    def _run_claude_agent(self, persona_name: str, session: OrchestratorSession) -> None:
        """Run a Claude SDK agent session for implementation work."""
        runner, system_prompt = self._make_runner(persona_name, session)
        ctx_path = self._context_log_path(session)

        task = (
            f"Work ID: {session.work_item.work_id}\n"
            f"Attempt: {session.attempt_number}\n\n"
            f"## Context log\n"
            f"Read `{ctx_path}` first — it contains the spec, everything learned in "
            f"previous attempts, and pointers to prior conversation logs you can Read "
            f"for details.\n\n"
            f"## Your task\n"
            f"{session.work_item.spec_content}\n\n"
            f"## When done\n"
            f"Stop calling tools once you have reached a conclusion. Append a brief "
            f"markdown section to `{ctx_path}` summarising: what you found, what you "
            f"changed (with file paths), and what still needs work. Include the path to "
            f"your conversation log if available. Then respond with a final summary "
            f"followed by exactly: 'Implementation complete.'"
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
        runner, system_prompt = self._make_runner(persona_name, session)
        ctx_path = self._context_log_path(session)

        task = (
            f"Debug failure for work item {session.work_item.work_id} "
            f"(attempt {session.attempt_number}).\n\n"
            f"## Context log\n"
            f"Read `{ctx_path}` first — it has the full history of what has been tried.\n\n"
            f"## Your task\n"
            f"Investigate logs, deployment state, and recent changes. Identify what failed "
            f"and why. Append your findings to `{ctx_path}` as a markdown section, "
            f"including the path to your conversation log. Then return a bullet-point list "
            f"of findings and hypotheses followed by exactly: 'Debug complete.'"
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

    def _run_pr_submission_agent(self, repos: list, session: OrchestratorSession) -> None:
        """Run PR submission agent to push branches and create PRs."""
        runner, system_prompt = self._make_runner("pr-submitter", session)

        # Build task description with repo info
        repos_info = []
        for repo in repos:
            files_summary = ", ".join(repo.changes[:10])
            if len(repo.changes) > 10:
                files_summary += f" ... ({len(repo.changes)} total)"
            repos_info.append(
                f"- Repository: {repo.repo_name}\n"
                f"  Branch with changes: {repo.branch}\n"
                f"  Workspace: {repo.workspace_path}\n"
                f"  Changed files: {files_summary}"
            )

        ctx_path = self._context_log_path(session)

        task = (
            f"Work ID: {session.work_item.work_id}\n"
            f"Attempt: {session.attempt_number}\n\n"
            f"## Context log\n"
            f"Read `{ctx_path}` first for full context on what was implemented and why.\n\n"
            f"## Repositories to publish\n\n"
            f"{chr(10).join(repos_info)}\n\n"
            f"For each repository listed above:\n"
            f"1. cd to the Workspace path\n"
            f"2. Run `git remote -v` to identify fork and upstream remotes\n"
            f"3. Run `git log --oneline -10` and `git branch -r` to understand the branch history\n"
            f"4. Determine the correct base branch for the PR:\n"
            f"   - If the spec is fixing a problem IN a specific PR or branch, target that PR's branch\n"
            f"   - If the spec is a standalone feature or fix, target the upstream default branch\n"
            f"   - Use `git log --not --remotes --format=%P | tail -1 | xargs git branch -r --contains 2>/dev/null` "
            f"to find where the local commits diverged from remote history\n"
            f"5. Use `gh api` to push the branch to the fork remote (do NOT use `git push`)\n"
            f"6. Generate a meaningful PR title and body from the spec and changed files:\n"
            f"   - Title: concise summary of what was actually fixed or changed\n"
            f"   - Body: what problem was solved, key files changed, work ID ({session.work_item.work_id}), "
            f"attempt {session.attempt_number}, 'Generated by spec-to-pr'\n"
            f"7. Create the PR: `gh pr create --repo <upstream> --head <fork>:<branch> "
            f"--base <detected-base> --title '...' --body '...'`\n\n"
            f"After creating each PR, report the PR URL in the format:\n"
            f"PR created for <repo-name>: <url>\n\n"
            f"When all PRs are created, respond with 'PR submission complete.'"
        )

        result = runner.run(
            system_prompt=system_prompt,
            task=task,
            work_id=f"{session.work_item.work_id}-pr"
        )
        log.info("PR submission agent finished. Summary: %s", result[:300])

        # Parse PR URLs from agent output
        import re
        pr_url_pattern = r'PR created for ([^:]+):\s*(https://[^\s]+)'
        for match in re.finditer(pr_url_pattern, result):
            repo_match, pr_url = match.groups()
            # Find the repo and update its pr_url
            for repo in repos:
                if repo_match.strip() in repo.repo_name:
                    repo.pr_url = pr_url.strip()
                    log.info("Recorded PR URL for %s: %s", repo.repo_name, repo.pr_url)
                    break

    def _estimate_progress(self, session: OrchestratorSession) -> float:
        return max(0.0, 1.0 - (session.attempt_number / session.max_attempts))

    @staticmethod
    def _progress_score(entry: DebugMemoryEntry) -> float:
        tr = entry.test_results
        if tr.total == 0:
            return 0.0
        return tr.passed / tr.total
