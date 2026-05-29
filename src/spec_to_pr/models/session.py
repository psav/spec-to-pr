from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .work_item import WorkItem

_TERMINAL_PHASES = {"complete", "human_escalation", "aborted"}

ANDON_TARGETS = {"HUMAN_ESCALATION", "REPROVISION", "IMPLEMENTED"}


@dataclass
class AndonSignal:
    """Structured escalation signal any agent can emit to break the retry loop."""
    # target is one of ANDON_TARGETS:
    #   HUMAN_ESCALATION — stop; a human must decide
    #   REPROVISION      — skip implementation, tear down and re-provision the env
    #   IMPLEMENTED      — fix is already on disk; skip implementation, go straight to deployment
    target: str
    reason: str
    agent: str
    timestamp: str = ""


class Phase(str, Enum):
    SPEC_INGESTION = "spec_ingestion"
    DRY_RUN_REVIEW = "dry_run_review"
    IMPLEMENTATION = "implementation"
    DEPLOYMENT = "deployment"
    E2E_EXECUTION = "e2e_execution"
    DEBUG = "debug"
    CIRCUIT_BREAKER_CHECK = "circuit_breaker_check"
    PR_SUBMISSION = "pr_submission"
    HUMAN_ESCALATION = "human_escalation"
    COMPLETE = "complete"
    ABORTED = "aborted"


@dataclass
class RepoState:
    repo_name: str
    repo_url: str
    workspace_path: str
    branch: str = "main"
    base_branch: str = "main"
    changes: list[str] = field(default_factory=list)
    pr_url: Optional[str] = None
    status: str = "clean"


@dataclass
class OrchestratorSession:
    session_id: str
    work_item: WorkItem
    current_phase: Phase
    attempt_number: int
    max_attempts: int
    dry_run: bool
    repos: list[RepoState]
    created_at: datetime
    updated_at: datetime
    deployment_params: dict[str, str] = field(default_factory=dict)
    ephemeral_id: str = ""
    pending_andon: Optional[AndonSignal] = None
    skip_implementation: bool = False

    @classmethod
    def new(cls, work_item: WorkItem, dry_run: bool = False, max_attempts: int = 3) -> OrchestratorSession:
        now = datetime.now(timezone.utc)
        return cls(
            session_id=str(uuid.uuid4()),
            work_item=work_item,
            current_phase=Phase.SPEC_INGESTION,
            attempt_number=0,
            max_attempts=max_attempts,
            dry_run=dry_run,
            repos=[],
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        return self.current_phase.value in _TERMINAL_PHASES
