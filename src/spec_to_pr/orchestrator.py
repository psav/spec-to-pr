from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import yaml
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
from spec_to_pr.models.work_item import (
    _COMMITTER_HEADINGS,
    _PR_SUBMITTER_HEADINGS,
    _spec_for_developer,
    _spec_section,
)
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

    def review(
        self,
        pr_ref: str,
        since: str | None = None,
        escalation_user: str = "",
    ) -> OrchestratorSession | None:
        """Fetch open review comments on a PR and address them via the implementation pipeline."""
        import json
        import re

        url_match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_ref)
        hash_match = re.match(r"([^/]+/[^/]+)#(\d+)", pr_ref)
        if url_match:
            repo, pr_number = url_match.group(1), int(url_match.group(2))
        elif hash_match:
            repo, pr_number = hash_match.group(1), int(hash_match.group(2))
        else:
            raise ValueError(f"Cannot parse PR reference {pr_ref!r} — use URL or owner/repo#number")

        pr_result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo,
             "--json", "number,title,headRefName,baseRefName,url,updatedAt"],
            capture_output=True, text=True,
        )
        if pr_result.returncode != 0:
            raise RuntimeError(f"gh pr view failed: {pr_result.stderr}")
        pr = json.loads(pr_result.stdout)

        if since:
            from datetime import datetime, timezone
            updated_at = datetime.fromisoformat(pr["updatedAt"].replace("Z", "+00:00"))
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            if updated_at <= since_dt:
                log.info("PR #%d not updated since %s — skipping", pr_number, since)
                return None

        inline_result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}/comments",
             "--jq", "[.[] | {id:.id, path:.path, line:.line, body:.body, user:.user.login}]"],
            capture_output=True, text=True,
        )
        inline = json.loads(inline_result.stdout) if inline_result.returncode == 0 and inline_result.stdout.strip() else []

        issue_result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments",
             "--jq", "[.[] | {id:.id, body:.body, user:.user.login}]"],
            capture_output=True, text=True,
        )
        issue = json.loads(issue_result.stdout) if issue_result.returncode == 0 and issue_result.stdout.strip() else []

        if not inline and not issue:
            log.info("No review comments on PR #%d — skipping", pr_number)
            return None

        comments_md = ""
        reviewers: set[str] = set()
        if inline:
            comments_md += "### Inline review comments\n\n"
            for c in inline:
                comments_md += f"**@{c['user']}** on `{c['path']}` line {c.get('line', '?')}:\n> {c['body']}\n\n"
                reviewers.add(c["user"])
        if issue:
            comments_md += "### General PR comments\n\n"
            for c in issue:
                comments_md += f"**@{c['user']}**:\n> {c['body']}\n\n"
                reviewers.add(c["user"])

        # Reviewers to re-request after pushing (exclude the escalation user —
        # they need to respond to a question, not re-review finished work)
        rerequest_users = sorted(reviewers - {escalation_user} if escalation_user else reviewers)
        rerequest_md = ""
        if rerequest_users:
            rerequest_md = (
                f"\n## After pushing — re-request review\n\n"
                f"Once the PR Submitter has pushed the fix, re-request review from the "
                f"reviewers whose comments you addressed. Check each user's type first — "
                f"skip users whose `gh api users/<login> --jq '.type'` returns `\"Bot\"` "
                f"if re-triggering a bot review is undesirable; otherwise include them.\n\n"
                f"```bash\n"
                + "".join(
                    f"gh pr edit {pr_number} --repo {repo} --request-review {u}\n"
                    for u in rerequest_users
                )
                + f"```\n"
                f"This notifies reviewers that their comments have been addressed.\n"
            )

        escalation_md = ""
        if escalation_user:
            escalation_md = (
                f"\n## Escalation\n\n"
                f"If you are unsure how to address a comment or it requires a design decision, "
                f"do NOT guess — post a reply tagging @{escalation_user}:\n\n"
                f"```bash\n"
                f"gh api repos/{repo}/issues/{pr_number}/comments \\\n"
                f"  -X POST \\\n"
                f'  -f body="@{escalation_user} Need your input: <brief summary>"\n'
                f"```\n"
            )

        repo_name = repo.split("/")[-1]
        work_id = f"REVIEW-PR{pr_number}"
        spec = (
            f"---\nwork_id: {work_id}\n"
            f"title: \"Address review comments on PR #{pr_number}: {pr['title']}\"\n"
            f"skip_deploy: true\n---\n\n"
            f"# Address review comments on PR #{pr_number}\n\n"
            f"**PR**: [{pr['title']}]({pr['url']})\n"
            f"**Repository**: `{repo}`\n"
            f"**Branch**: `{pr['headRefName']}` → `{pr['baseRefName']}`\n\n"
            f"## Your task\n\n"
            f"Review and address the open comments on this PR. For each comment:\n"
            f"- If you can confidently address it with a targeted code change: do so\n"
            f"- If you are unsure or it requires a design decision: escalate (see below)\n\n"
            f"## Steps\n\n"
            f"1. Clone the repository if not already present at `/workspace/{repo_name}` "
            f"(or `git fetch` if already cloned)\n"
            f"2. Add the fork remote and check out the PR branch: `git checkout {pr['headRefName']}`\n"
            f"3. Read `CLAUDE.md` for repo-specific conventions and required checks\n"
            f"4. Address each comment below\n"
            f"5. Run `make pre-push` to validate all checks pass\n"
            f"6. Leave changes uncommitted — Committer and PR Submitter handle that\n\n"
            f"## Review comments\n\n{comments_md}"
            f"{escalation_md}"
            f"{rerequest_md}\n"
            f"## Done when\n\n"
            f"All addressable comments have code changes, comments needing human input "
            f"have escalation replies, review has been re-requested from addressed reviewers, "
            f"and `make pre-push` passes.\n\n"
            f"Respond with 'Implementation complete.' when done.\n"
        )

        from spec_to_pr.models.work_item import SourceType
        work_item = WorkItem(
            work_id=work_id,
            source_type=SourceType.REVIEW,
            source_ref=pr_ref,
            spec_content=spec,
            title=f"Address review comments on PR #{pr_number}",
        )
        self.config.skip_deploy = True
        return self.run(work_item)

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

    def _load_deployment_params(self, session: OrchestratorSession) -> None:
        """Load deployment parameters written by developer agent."""
        cwd = Path(session.repos[0].workspace_path) if session.repos else self.config.workspace
        params_file = cwd / ".spec-to-pr" / "deployment-params.yaml"

        if not params_file.exists():
            log.info("No deployment-params.yaml found - will use default deployment")
            return

        try:
            with open(params_file) as f:
                params = yaml.safe_load(f)
            if params and isinstance(params, dict):
                session.deployment_params = {k: str(v) for k, v in params.items()}
                log.info("Loaded deployment params from developer agent: %s", session.deployment_params)
            else:
                log.warning("deployment-params.yaml exists but is empty or invalid")
        except Exception as exc:
            log.warning("Failed to load deployment-params.yaml: %s", exc)

    def _run_implementation_team(self, session: OrchestratorSession) -> None:
        """Spawn Claude SDK agent sessions for implementation work."""
        log.info("Running implementation team (attempt %d)", session.attempt_number)
        try:
            self._run_claude_agent("developer", session)

            # Load deployment parameters if developer agent provided them
            self._load_deployment_params(session)

            # Commit changes and track them for PR creation
            self._commit_and_track_changes(session)

            # Reload deployment params — committer may have updated them after pushing
            self._load_deployment_params(session)

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
        if self.config.skip_deploy:
            log.info("skip_deploy=True — skipping deployment, jumping to PR submission")
            session.current_phase = Phase.PR_SUBMISSION
            return
        log.info("Deploying to ephemeral environment")
        cwd = Path(session.repos[0].workspace_path) if session.repos else self.config.workspace

        # Use deployment parameters specified by developer agent
        make_vars = session.deployment_params
        if make_vars:
            log.info("Using deployment params from developer agent: %s", make_vars)

        envs_file = cwd / ".ephemeral-envs"
        if envs_file.exists():
            in_progress = [
                line for line in envs_file.read_text().splitlines()
                if "STATE=provisioning" in line
            ]
            if in_progress:
                log.warning(
                    "Skipping ephemeral-provision — %d env(s) already provisioning: %s",
                    len(in_progress), in_progress,
                )
                self.state_machine.transition(session, deployment_successful=True)
                return

        log.info("Running make ephemeral-provision in %s", cwd)
        ok, stderr = self._run_make("ephemeral-provision", cwd=cwd, make_vars=make_vars)

        if not ok:
            error_class = self._classify_deployment_error(stderr)

            if error_class == "infrastructure":
                log.error("Infrastructure error detected - escalating to human immediately")
                self._context_log_append(
                    session,
                    f"Deployment FAILED (infrastructure error) — attempt {session.attempt_number}",
                    f"- `make ephemeral-provision` failed with infrastructure error\n"
                    f"- Error: {stderr[-500:]}\n"
                    f"- This is not a code issue that debug can fix - escalating to human",
                )
                session.current_phase = Phase.HUMAN_ESCALATION
                return
            else:
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
        ok, _ = self._run_make("ephemeral-e2e", cwd=cwd)
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

        branch_name = (
            (session.deployment_params or {}).get("BRANCH")
            or f"spec-to-pr/{session.work_item.work_id}"
        )

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
            committer_notes = _spec_section(session.work_item.spec_content, _COMMITTER_HEADINGS)
            notes_block = (
                f"\n## Spec notes for this commit\n{committer_notes}\n"
                if committer_notes else ""
            )
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
                f"then skip straight to step 6 (push).\n"
                f"3. Filter out spec-to-pr metadata — never stage:\n"
                f"   - .spec-to-pr/ directory\n"
                f"   - conversations/ directory\n"
                f"   - Spec *.md files in the workspace root\n"
                f"4. Create or switch to branch `{branch_name}`: "
                f"`git checkout -b {branch_name}` or `git checkout {branch_name}`.\n"
                f"5. Stage only implementation files and commit:\n"
                f"   [{session.work_item.work_id}] <brief description of what changed>\n\n"
                f"   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>\n"
                f"6. Push the branch to the bot fork so the ephemeral environment can clone it:\n"
                f"   - Identify the bot remote: "
                f"`BOT_OWNER=$(gh api user --jq '.login')` then find the remote whose URL "
                f"contains `/${{BOT_OWNER}}/`.\n"
                f"   - `git push <bot-remote> HEAD:{branch_name} --force`\n"
                f"   - Report the push result (remote URL and branch).\n"
                f"   - After a successful push, write `{repo_path}/.spec-to-pr/deployment-params.yaml` "
                f"with exactly these two lines (substitute real values):\n"
                f"     REPO: <bot-owner>/<repo-name>\n"
                f"     BRANCH: {branch_name}\n"
                f"     (repo-name is just the last path component of the remote URL, no .git suffix)\n"
                f"7. Report committed files as:\n"
                f"   Committed files: <file1>, <file2>, ...\n"
                f"{notes_block}\n"
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
                ["git", "log", "HEAD", "--oneline", "--not", "--remotes"],
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
            f"*Agents: read this file before starting. Most recent updates appear first, below this line.*\n\n"
        )

    def _context_log_append(self, session: OrchestratorSession, heading: str, body: str) -> None:
        path = self._context_log_path(session)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"## {heading} — {now}\n\n{body.strip()}\n\n"
        existing = path.read_text() if path.exists() else ""
        # Insert after the header marker so most-recent entries appear before older ones.
        marker = "*Agents: read this file before starting. Most recent updates appear first, below this line.*\n\n"
        if marker in existing:
            idx = existing.index(marker) + len(marker)
            path.write_text(existing[:idx] + entry + existing[idx:])
        else:
            with open(path, "a") as f:
                f.write(entry)

    def _load_project_docs(self) -> None:
        """Load project documentation to provide environment and project context to agents."""
        parts = []

        # Always load workspace-root CLAUDE.md — it explains the container/proxy environment
        # (dummy GITHUB_TOKEN, proxy-injected auth, no git push, use gh api instead, etc.)
        workspace_claude = self.config.workspace / "CLAUDE.md"
        if workspace_claude.exists():
            try:
                parts.append(workspace_claude.read_text())
                log.info("Loaded workspace environment docs from %s", workspace_claude)
            except Exception as exc:
                log.warning("Failed to load workspace CLAUDE.md: %s", exc)

        # Also load any explicitly-specified project docs (e.g. spec-to-pr's own CLAUDE.md)
        if self.config.project_docs_path:
            if self.config.project_docs_path.exists():
                try:
                    text = self.config.project_docs_path.read_text()
                    parts.append(text)
                    log.info("Loaded project docs from %s (%d chars)", self.config.project_docs_path, len(text))
                except Exception as exc:
                    log.warning("Failed to load project docs from %s: %s", self.config.project_docs_path, exc)
            else:
                log.debug("Project docs path not found: %s", self.config.project_docs_path)

        self._project_docs = "\n\n---\n\n".join(parts) if parts else None
        if self._project_docs:
            log.info("Total project documentation: %d chars", len(self._project_docs))

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
                    log.warning("Failed to get git diff, skipping tests")
                    return False
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

    def _classify_deployment_error(self, stderr: str) -> str:
        """Classify deployment failure to determine if debug phase is useful.

        Returns:
            'infrastructure' - git/AWS/network errors that debug can't fix
            'code' - errors in terraform/code that debug might fix
            'unknown' - unclear, let debug investigate
        """
        infrastructure_patterns = [
            r"Remote branch .* not found",
            r"fatal: repository .* not found",
            r"fatal: could not read Username",
            r"The security token included in the request is invalid",
            r"InvalidClientTokenId",
            r"CredentialsError",
            r"NoCredentialsError",
            r"Unable to locate credentials",
            r"Connection refused",
            r"Name or service not known",
            r"invalid peer certificate",
        ]

        for pattern in infrastructure_patterns:
            if re.search(pattern, stderr, re.IGNORECASE):
                return "infrastructure"

        return "unknown"

    def _run_make(self, target: str, cwd: Path | None = None, make_vars: dict[str, str] | None = None) -> tuple[bool, str]:
        """Run make target with optional variables. Returns (success, stderr)."""
        run_cwd = cwd or self.config.workspace
        # Ensure SSL/cert environment variables are passed through for proxy environments
        env = os.environ.copy()
        _proxy_ca = "/etc/pki/ca-trust/source/anchors/proxy-ca.crt"
        _system_ca = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
        if "AWS_CA_BUNDLE" not in env and os.path.exists(_system_ca):
            env["AWS_CA_BUNDLE"] = _system_ca
        if "REQUESTS_CA_BUNDLE" not in env:
            env["REQUESTS_CA_BUNDLE"] = env.get("AWS_CA_BUNDLE", _system_ca)
        if "SSL_CERT_FILE" not in env:
            env["SSL_CERT_FILE"] = env.get("REQUESTS_CA_BUNDLE", _system_ca)
        if "UV_SYSTEM_CERTS" not in env:
            env["UV_SYSTEM_CERTS"] = "1"
        # Pass the egress proxy CA cert to container image builds (podman/docker build --build-arg).
        # ensure_image() in env-common.sh checks PROXY_CA_CERT and passes it as a build arg so
        # that dnf/apt inside the CI container can reach package registries through the proxy.
        if "PROXY_CA_CERT" not in env and os.path.exists(_proxy_ca):
            try:
                env["PROXY_CA_CERT"] = Path(_proxy_ca).read_text()
            except OSError:
                pass

        cmd = ["make", target]
        if make_vars:
            cmd.extend(f"{k}={v}" for k, v in make_vars.items())

        log_dir = run_cwd / ".spec-to-pr" / "make-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / f"{target}-{timestamp}.log"
        log.info("make %s → streaming output to %s", target, log_path)

        stderr_buf: list[str] = []
        with open(log_path, "w") as lf:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=run_cwd, env=env,
            )

            def _drain(stream, collect: list[str] | None, tty) -> None:
                for line in stream:
                    lf.write(line)
                    if collect is not None:
                        collect.append(line)
                    tty.write(line)
                    tty.flush()

            t_out = threading.Thread(target=_drain, args=(proc.stdout, None, sys.stdout), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf, sys.stderr), daemon=True)
            t_out.start()
            t_err.start()
            proc.wait()
            t_out.join()
            t_err.join()

        stderr = "".join(stderr_buf)
        if proc.returncode != 0:
            log.error("make %s failed (exit %d) — full log: %s", target, proc.returncode, log_path)
        return proc.returncode == 0, stderr

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
            f"## Your phase: IMPLEMENTATION\n"
            f"You are in phase 1 of a multi-phase pipeline. Make the required code "
            f"changes on disk, then signal completion.\n\n"
            f"## Context log\n"
            f"Read `{ctx_path}` first — it contains the spec, everything learned in "
            f"previous attempts, and pointers to prior conversation logs you can Read "
            f"for details.\n\n"
            f"## Your task\n"
            f"{_spec_for_developer(session.work_item.spec_content)}\n\n"
            f"## When done\n"
            f"Stop calling tools once you have reached a conclusion. Append a brief "
            f"markdown section to `{ctx_path}` summarising: what you found, what you "
            f"changed (with file paths), and what still needs work. Include the path to "
            f"your conversation log if available. Then respond with a final summary "
            f"followed by exactly: 'Implementation complete.'\n\n"
            f"## STOP — do not commit or push\n"
            f"Your job ends when the files are correct on disk. A Committer agent runs "
            f"immediately after you and handles `git add`, `git commit`, and `git push`. "
            f"If you commit or push, you will create duplicate commits and break the pipeline."
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
            f"of findings and hypotheses followed by exactly: 'Debug complete.'\n\n"
            f"## Constraints\n"
            f"- Do NOT run `make ephemeral-provision` — provisioning is the orchestrator's job on retry.\n"
            f"- You MAY run `make ephemeral-resync ID=<id>` if an environment already exists and "
            f"a code fix needs redeploying to verify it.\n"
            f"- Do NOT start long-running blocking operations. Background any shell commands that "
            f"take more than a few seconds.\n"
            f"- Write findings and exit. The orchestrator will handle retrying the failed phase."
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

        pr_notes = _spec_section(session.work_item.spec_content, _PR_SUBMITTER_HEADINGS)
        pr_notes_block = (
            f"\n## Spec notes for this PR\n{pr_notes}\n"
            if pr_notes else ""
        )

        task = (
            f"Work ID: {session.work_item.work_id}\n"
            f"Attempt: {session.attempt_number}\n\n"
            f"## Context log\n"
            f"Read `{ctx_path}` first for full context on what was implemented and why.\n\n"
            f"## Repositories to publish\n\n"
            f"{chr(10).join(repos_info)}\n\n"
            f"For each repository, follow this decision tree exactly.\n\n"
            f"### Step 1 — identify remotes and branch topology\n"
            f"- Establish your identity: `BOT_OWNER=$(gh api user --jq '.login')`\n"
            f"- `git remote -v` — list all remotes with their URLs.\n"
            f"- The **bot fork** is the remote whose URL contains `${{BOT_OWNER}}/` as the owner.\n"
            f"- The **upstream** is any other remote (the canonical org repo).\n"
            f"- `git log --not --remotes --oneline` — commits that exist locally but not on any remote.\n"
            f"- Find the divergence point: `git log --not --remotes --format='%P' | tail -1`\n"
            f"- Find the source branch: `git branch -r --contains <divergence-sha>`\n"
            f"  This is the branch these commits were built on top of.\n\n"
            f"### Step 2 — push and decide on a PR\n\n"
            f"**Never push directly to `main` or any default branch** — always push to a named branch.\n\n"
            f"**Case A — source branch is on our bot fork**\n"
            f"- Push back to that same branch on the bot fork: `git push <bot-remote> HEAD:<source-branch-name> --force`\n"
            f"- Check for an existing open PR: "
            f"`gh pr list --repo <upstream> --head <bot-owner>:<source-branch-name> --state open --json number,url`\n"
            f"- If PR exists: report its URL. **Do NOT open a new PR.**\n"
            f"- If no PR: open one against the upstream non-default base branch (or `main` for standalone work).\n\n"
            f"**Case B — source branch is on a third-party fork (not our bot, not upstream)**\n"
            f"- Push our fix as a new branch to the bot fork: `git push <bot-remote> HEAD:<branch-name> --force`\n"
            f"- Open a PR from `<bot-owner>:<branch-name>` → `<third-party-owner>:<source-branch-name>`\n"
            f"  on the upstream repo. Contribute to their PR, not to main.\n"
            f"  **Do NOT target main** — that would claim their work.\n\n"
            f"**Case C — source branch is upstream main or standalone new feature**\n"
            f"- Push to the bot fork: `git push <bot-remote> HEAD:<branch-name> --force`\n"
            f"- Open a PR from `<bot-owner>:<branch-name>` → `main` on the upstream.\n\n"
            f"### Step 3 — PR content (when opening a new PR)\n"
            f"`gh pr create --repo <upstream> --head <bot-owner>:<branch> --base <base> --title '...' --body '...'`\n"
            f"Body: what problem was solved, key files changed, "
            f"work ID ({session.work_item.work_id}), 'Generated by spec-to-pr'\n\n"
            f"After handling each repo, report the PR URL as:\n"
            f"PR created for <repo-name>: <url>\n\n"
            f"{pr_notes_block}"
            f"When all repos are handled, respond with 'PR submission complete.'"
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