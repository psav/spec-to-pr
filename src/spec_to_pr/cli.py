from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from spec_to_pr.models import WorkItem
from spec_to_pr.models.work_item import _parse_frontmatter
from spec_to_pr.orchestrator import Config, Orchestrator
from spec_to_pr.storage import FileStorage


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spec-to-pr",
        description="Autonomous spec-to-pull-request orchestrator",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- run ----
    run_p = sub.add_parser("run", help="Execute the spec-to-PR workflow")
    src = run_p.add_mutually_exclusive_group(required=True)
    src.add_argument("--work-id", metavar="ID", help="JIRA ID (e.g. ROSAENG-1234)")
    src.add_argument("--file", metavar="PATH", help="Path to a spec markdown file")
    src.add_argument("--inline", metavar="TEXT", help="Inline spec text")
    run_p.add_argument("--dry-run", action="store_true", help="Plan only — do not deploy or test")
    run_p.add_argument("--skip-deploy", action="store_true", help="Skip ephemeral deploy/e2e — go straight to PR after implementation")
    run_p.add_argument("--max-attempts", type=int, default=3, metavar="N")
    run_p.add_argument("--storage", default=".spec-to-pr/sessions", metavar="PATH")
    run_p.add_argument("--agents", default=".claude/agents", metavar="PATH")
    run_p.add_argument("--conversations", default="/conversations", metavar="PATH")
    run_p.add_argument("--project-docs", metavar="PATH", help="Path to CLAUDE.md or project docs (auto-discovers if not specified)")

    # ---- status ----
    status_p = sub.add_parser("status", help="Show session status for a work ID")
    status_p.add_argument("--work-id", required=True, metavar="ID")
    status_p.add_argument("--storage", default=".spec-to-pr/sessions", metavar="PATH")

    # ---- resume ----
    resume_p = sub.add_parser("resume", help="Resume an interrupted session")
    resume_p.add_argument("--work-id", required=True, metavar="ID")
    resume_p.add_argument("--storage", default=".spec-to-pr/sessions", metavar="PATH")
    resume_p.add_argument("--agents", default=".claude/agents", metavar="PATH")
    resume_p.add_argument("--conversations", default="/conversations", metavar="PATH")
    resume_p.add_argument("--project-docs", metavar="PATH", help="Path to CLAUDE.md or project docs (auto-discovers if not specified)")
    resume_p.add_argument("--max-attempts", type=int, default=3, metavar="N")

    # ---- validate ----
    validate_p = sub.add_parser("validate", help="Validate a spec file without running the orchestrator")
    validate_p.add_argument("--file", required=True, metavar="PATH", help="Path to a spec markdown file")

    return parser


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    if args.work_id:
        work_item = WorkItem.from_jira(args.work_id)
        work_item.spec_content = f"JIRA ticket: {args.work_id}"
    elif args.file:
        work_item = WorkItem.from_file(args.file)
    else:
        work_item = WorkItem.from_inline(args.inline)

    config = Config(
        storage_path=Path(args.storage),
        agents_path=Path(args.agents),
        conversations_path=Path(args.conversations),
        project_docs_path=Path(args.project_docs) if args.project_docs else None,
        max_attempts=args.max_attempts,
        skip_deploy=args.skip_deploy,
    )
    orch = Orchestrator(config)
    session = orch.run(work_item, dry_run=args.dry_run)
    print(f"\nFinal phase: {session.current_phase.value}")
    return 0 if session.current_phase.value == "complete" else 1


def _cmd_status(args: argparse.Namespace) -> int:
    storage = FileStorage(Path(args.storage))
    session = storage.load_session(args.work_id)
    if session is None:
        print(f"No session found for {args.work_id}", file=sys.stderr)
        return 1
    print(f"Work ID : {session.work_item.work_id}")
    print(f"Phase   : {session.current_phase.value}")
    print(f"Attempt : {session.attempt_number} / {session.max_attempts}")
    print(f"Dry run : {session.dry_run}")
    entries = storage.load_debug_entries(args.work_id)
    if entries:
        print(f"\nDebug attempts: {len(entries)}")
        for e in entries:
            print(f"  [{e.attempt_number}] {e.error_summary} (fp={e.error_fingerprint})")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.file)
    print(f"Validating: {args.file}")

    if not path.exists():
        print(f"  [ERROR] file not found: {args.file}", file=sys.stderr)
        return 1

    try:
        text = path.read_text()
    except OSError as exc:
        print(f"  [ERROR] cannot read file: {exc}", file=sys.stderr)
        return 1

    print("  [OK] file readable")

    fm, body = _parse_frontmatter(text)
    work_id = fm.get("work_id")
    if work_id:
        print(f"  [OK] work_id: {work_id}")
    else:
        print("  [WARN] no work_id — will auto-generate")

    print(f"  [OK] spec_content: {len(text)} chars")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    config = Config(
        storage_path=Path(args.storage),
        agents_path=Path(args.agents),
        conversations_path=Path(args.conversations),
        project_docs_path=Path(args.project_docs) if args.project_docs else None,
        max_attempts=args.max_attempts,
    )
    orch = Orchestrator(config)
    session = orch.resume(args.work_id)
    print(f"\nFinal phase: {session.current_phase.value}")
    return 0 if session.current_phase.value == "complete" else 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _configure_logging(args.verbose)

    dispatch = {
        "run": _cmd_run,
        "status": _cmd_status,
        "resume": _cmd_resume,
        "validate": _cmd_validate,
    }
    sys.exit(dispatch[args.command](args))
