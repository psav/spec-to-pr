from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .session import OrchestratorSession, Phase


class FailurePhase(str, Enum):
    IMPLEMENTATION = "implementation"
    DEPLOYMENT = "deployment"
    E2E_EXECUTION = "e2e_execution"


class DebugOutcome(str, Enum):
    RETRY = "retry"
    ESCALATED = "escalated"


@dataclass
class E2EResults:
    total: int = 0
    passed: int = 0
    failed: int = 0
    failed_tests: list[str] = field(default_factory=list)


@dataclass
class EphemeralEnv:
    env_id: str
    region: str
    api_url: str
    state: str = "provisioning"


@dataclass
class DebugMemoryEntry:
    attempt_number: int
    timestamp: datetime
    phase_at_failure: FailurePhase
    error_summary: str
    error_fingerprint: str
    test_results: E2EResults = field(default_factory=E2EResults)
    debug_findings: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    changes_attempted: list[str] = field(default_factory=list)
    outcome: Optional[DebugOutcome] = None


@dataclass
class PhaseContext:
    from_phase: Phase
    to_phase: Phase
    session_state: OrchestratorSession
    previous_attempts: list[DebugMemoryEntry] = field(default_factory=list)
    implementation_plan: Optional[str] = None
    ephemeral_env: Optional[EphemeralEnv] = None
