from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .work_item import WorkItem

_TERMINAL_PHASES = {"complete", "human_escalation", "aborted"}


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
