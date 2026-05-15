from __future__ import annotations

from spec_to_pr.models.session import OrchestratorSession, Phase


class InvalidTransitionError(Exception):
    pass


class StateMachine:
    """Deterministic phase transition logic. All conditions are passed by the caller."""

    def transition(self, session: OrchestratorSession, **conditions) -> None:
        """Mutate session.current_phase based on the current phase and supplied conditions."""
        if session.is_terminal:
            raise InvalidTransitionError(
                f"Session is in terminal phase {session.current_phase!r} — no further transitions allowed"
            )

        match session.current_phase:
            case Phase.SPEC_INGESTION:
                self._from_spec_ingestion(session, **conditions)

            case Phase.DRY_RUN_REVIEW:
                self._from_dry_run_review(session, **conditions)

            case Phase.IMPLEMENTATION:
                self._from_implementation(session, **conditions)

            case Phase.DEPLOYMENT:
                self._from_deployment(session, **conditions)

            case Phase.E2E_EXECUTION:
                self._from_e2e(session, **conditions)

            case Phase.DEBUG:
                session.current_phase = Phase.CIRCUIT_BREAKER_CHECK

            case Phase.CIRCUIT_BREAKER_CHECK:
                self._from_circuit_breaker(session, **conditions)

            case Phase.PR_SUBMISSION:
                session.current_phase = Phase.COMPLETE

            case _:
                raise InvalidTransitionError(f"No transitions defined for {session.current_phase!r}")

    # ------------------------------------------------------------------
    # Per-phase helpers
    # ------------------------------------------------------------------

    def _from_spec_ingestion(self, session: OrchestratorSession, spec_valid: bool = False, dry_run: bool = False, **_) -> None:
        if not spec_valid:
            raise InvalidTransitionError("Spec validation failed — cannot transition from SPEC_INGESTION")
        session.current_phase = Phase.DRY_RUN_REVIEW if dry_run else Phase.IMPLEMENTATION

    def _from_dry_run_review(self, session: OrchestratorSession, human_approved: bool = False, **_) -> None:
        session.current_phase = Phase.IMPLEMENTATION if human_approved else Phase.ABORTED

    def _from_implementation(self, session: OrchestratorSession, implementation_complete: bool = False, **_) -> None:
        session.current_phase = Phase.DEPLOYMENT if implementation_complete else Phase.CIRCUIT_BREAKER_CHECK

    def _from_deployment(self, session: OrchestratorSession, deployment_successful: bool = False, **_) -> None:
        session.current_phase = Phase.E2E_EXECUTION if deployment_successful else Phase.DEBUG

    def _from_e2e(self, session: OrchestratorSession, tests_passed: bool = False, **_) -> None:
        session.current_phase = Phase.PR_SUBMISSION if tests_passed else Phase.DEBUG

    def _from_circuit_breaker(self, session: OrchestratorSession, breaker_tripped: bool = False, **_) -> None:
        if breaker_tripped:
            session.current_phase = Phase.HUMAN_ESCALATION
        else:
            session.attempt_number += 1
            session.current_phase = Phase.IMPLEMENTATION
