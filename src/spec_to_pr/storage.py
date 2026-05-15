from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from spec_to_pr.models.work_item import WorkItem, SourceType
from spec_to_pr.models.session import OrchestratorSession, RepoState, Phase
from spec_to_pr.models.phase_context import (
    DebugMemoryEntry,
    FailurePhase,
    DebugOutcome,
    E2EResults,
)


def _dt(val) -> datetime:
    if isinstance(val, datetime):
        return val
    return datetime.fromisoformat(str(val))


class FileStorage:
    """File-backed storage adapter. Layout: base_path/{work_id}/session.yaml, attempts/*.yaml"""

    def __init__(self, base_path: Path) -> None:
        self.base_path = Path(base_path)

    def _work_dir(self, work_id: str) -> Path:
        d = self.base_path / work_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def save_session(self, session: OrchestratorSession) -> None:
        data = {
            "session_id": session.session_id,
            "current_phase": session.current_phase.value,
            "attempt_number": session.attempt_number,
            "max_attempts": session.max_attempts,
            "dry_run": session.dry_run,
            "created_at": session.created_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "work_item": {
                "work_id": session.work_item.work_id,
                "source_type": session.work_item.source_type.value,
                "source_ref": session.work_item.source_ref,
                "spec_content": session.work_item.spec_content,
            },
            "repos": [dataclasses.asdict(r) for r in session.repos],
        }
        path = self._work_dir(session.work_item.work_id) / "session.yaml"
        path.write_text(yaml.dump(data, default_flow_style=False))

    def load_session(self, work_id: str) -> Optional[OrchestratorSession]:
        path = self.base_path / work_id / "session.yaml"
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text())
        wi_data = data["work_item"]
        work_item = WorkItem(
            work_id=wi_data["work_id"],
            source_type=SourceType(wi_data["source_type"]),
            source_ref=wi_data["source_ref"],
            spec_content=wi_data.get("spec_content", ""),
        )
        repos = [
            RepoState(
                repo_name=r["repo_name"],
                repo_url=r["repo_url"],
                workspace_path=r["workspace_path"],
                branch=r.get("branch", "main"),
                changes=r.get("changes", []),
                pr_url=r.get("pr_url"),
                status=r.get("status", "clean"),
            )
            for r in data.get("repos", [])
        ]
        return OrchestratorSession(
            session_id=data["session_id"],
            work_item=work_item,
            current_phase=Phase(data["current_phase"]),
            attempt_number=data["attempt_number"],
            max_attempts=data["max_attempts"],
            dry_run=data["dry_run"],
            repos=repos,
            created_at=_dt(data["created_at"]),
            updated_at=_dt(data["updated_at"]),
        )

    # ------------------------------------------------------------------
    # Debug entries
    # ------------------------------------------------------------------

    def save_debug_entry(self, work_id: str, entry: DebugMemoryEntry) -> None:
        attempts_dir = self._work_dir(work_id) / "attempts"
        attempts_dir.mkdir(exist_ok=True)
        path = attempts_dir / f"{entry.attempt_number}.yaml"
        data = {
            "attempt_number": entry.attempt_number,
            "timestamp": entry.timestamp.isoformat(),
            "phase_at_failure": entry.phase_at_failure.value,
            "error_summary": entry.error_summary,
            "error_fingerprint": entry.error_fingerprint,
            "test_results": dataclasses.asdict(entry.test_results),
            "debug_findings": entry.debug_findings,
            "hypotheses": entry.hypotheses,
            "changes_attempted": entry.changes_attempted,
            "outcome": entry.outcome.value if entry.outcome else None,
        }
        path.write_text(yaml.dump(data, default_flow_style=False))

    def load_debug_entries(self, work_id: str) -> list[DebugMemoryEntry]:
        attempts_dir = self.base_path / work_id / "attempts"
        if not attempts_dir.exists():
            return []
        entries = []
        for p in sorted(attempts_dir.glob("*.yaml")):
            d = yaml.safe_load(p.read_text())
            tr = d.get("test_results", {})
            entries.append(DebugMemoryEntry(
                attempt_number=d["attempt_number"],
                timestamp=_dt(d["timestamp"]),
                phase_at_failure=FailurePhase(d["phase_at_failure"]),
                error_summary=d["error_summary"],
                error_fingerprint=d["error_fingerprint"],
                test_results=E2EResults(
                    total=tr.get("total", 0),
                    passed=tr.get("passed", 0),
                    failed=tr.get("failed", 0),
                    failed_tests=tr.get("failed_tests", []),
                ),
                debug_findings=d.get("debug_findings", []),
                hypotheses=d.get("hypotheses", []),
                changes_attempted=d.get("changes_attempted", []),
                outcome=DebugOutcome(d["outcome"]) if d.get("outcome") else None,
            ))
        return entries
