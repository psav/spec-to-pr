"""
Real-time console visualiser for a running spec-to-pr session.

Polls session.yaml for phase transitions and tails the most recently
modified conversation JSONL file to stream agent activity.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Canonical phase order for prev/next calculation
PHASE_ORDER = [
    "spec_ingestion",
    "dry_run_review",
    "implementation",
    "deployment",
    "e2e_execution",
    "debug",
    "circuit_breaker_check",
    "pr_submission",
]

TERMINAL_PHASES = {"complete", "human_escalation", "aborted"}

PHASE_LABELS = {
    "spec_ingestion": "Spec Ingestion",
    "dry_run_review": "Dry-Run Review",
    "implementation": "Implementation",
    "deployment": "Deployment",
    "e2e_execution": "E2E Tests",
    "debug": "Debug",
    "circuit_breaker_check": "Circuit Breaker",
    "pr_submission": "PR Submission",
    "complete": "Complete ✓",
    "human_escalation": "Escalation",
    "aborted": "Aborted",
}

MAX_LOG_BUFFER = 200
DISPLAY_LOG_LINES = 35
POLL_INTERVAL = 0.5
CONV_FRESHNESS_SECS = 30


class Visualiser:
    def __init__(
        self,
        storage_dir: Path,
        conversations_dir: Path,
        work_id: Optional[str] = None,
        workspace: Optional[Path] = None,
    ) -> None:
        self.storage_dir = storage_dir
        self.conversations_dir = conversations_dir
        self.work_id = work_id
        self.workspace = workspace

        self._session: dict = {}
        self._session_mtime: float = 0.0

        self._log_lines: list[Text] = []
        self._conv_file: Optional[Path] = None
        self._conv_pos: int = 0
        self._make_log_file: Optional[Path] = None
        self._make_log_pos: int = 0

    def run(self, stop_event: "threading.Event | None" = None) -> None:
        if not HAS_RICH:
            print(
                "Error: 'rich' library is required. "
                "Install with: pip install rich",
                file=sys.stderr,
            )
            sys.exit(1)

        import threading  # noqa: F401 — referenced in type hint above

        # When embedded inside `spec-to-pr run --visualise`, sys.stdout is
        # redirected to /dev/null to suppress orchestrator output. Use
        # sys.__stdout__ so the TUI always writes to the real terminal.
        tty = getattr(sys, "__stdout__", None) or sys.stdout
        console = Console(file=tty)
        with Live(
            self._render(console),
            console=console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            try:
                while not (stop_event and stop_event.is_set()):
                    self._poll_session()
                    self._poll_log()
                    live.update(self._render(console))
                    time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                pass

    # ──────────────────────────────────────────────────────────────────────
    # Polling
    # ──────────────────────────────────────────────────────────────────────

    def _find_session_path(self) -> Optional[Path]:
        if self.work_id:
            p = self.storage_dir / self.work_id / "session.yaml"
            return p if p.exists() else None
        candidates = list(self.storage_dir.glob("*/session.yaml"))
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _poll_session(self) -> None:
        path = self._find_session_path()
        if path is None:
            return
        try:
            mtime = path.stat().st_mtime
            if mtime <= self._session_mtime:
                return
            with path.open() as f:
                data = yaml.safe_load(f) or {}
            self._session = data
            self._session_mtime = mtime
            if not self.work_id:
                self.work_id = (data.get("work_item") or {}).get("work_id")
        except Exception:
            pass

    def _find_latest_conv(self) -> Optional[Path]:
        if not self.work_id or not self.conversations_dir.exists():
            return None
        candidates = list(self.conversations_dir.glob(f"{self.work_id}*.jsonl"))
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _find_latest_make_log(self) -> Optional[Path]:
        if not self.workspace or not self.work_id:
            return None
        candidates = list(self.workspace.glob(f"*/.spec-to-pr/make-logs/{self.work_id}-*.log"))
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _poll_log(self) -> None:
        latest_conv = self._find_latest_conv()
        latest_make = self._find_latest_make_log()
        now = time.time()

        conv_is_fresh = (
            latest_conv is not None
            and now - latest_conv.stat().st_mtime < CONV_FRESHNESS_SECS
        )
        use_make_log = not conv_is_fresh and latest_make is not None

        if use_make_log:
            self._poll_make_log_source(latest_make)
        else:
            self._poll_conv_source(latest_conv)

        if len(self._log_lines) > MAX_LOG_BUFFER:
            self._log_lines = self._log_lines[-MAX_LOG_BUFFER:]

    def _poll_conv_source(self, latest: Optional[Path]) -> None:
        if self._make_log_file is not None:
            self._make_log_file = None
            sep = Text()
            sep.append("── agent resumed ──", style="dim italic cyan")
            self._log_lines.append(sep)

        if latest != self._conv_file:
            self._conv_file = latest
            self._conv_pos = 0
            if latest:
                sep = Text()
                sep.append(f"── {latest.name} ──", style="dim italic")
                self._log_lines.append(sep)

        if not self._conv_file:
            return

        try:
            with self._conv_file.open() as f:
                f.seek(self._conv_pos)
                new_data = f.read()
                self._conv_pos = f.tell()

            for raw in new_data.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    line = self._format_entry(entry)
                    if line is not None:
                        self._log_lines.append(line)
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

    def _poll_make_log_source(self, latest: Optional[Path]) -> None:
        if self._conv_file is not None and self._make_log_file is None:
            sep = Text()
            sep.append("── make ──", style="dim italic yellow")
            self._log_lines.append(sep)

        if latest != self._make_log_file:
            self._make_log_file = latest
            self._make_log_pos = 0
            if latest:
                sep = Text()
                sep.append(f"── {latest.name} ──", style="dim italic")
                self._log_lines.append(sep)

        if not self._make_log_file:
            return

        try:
            with self._make_log_file.open() as f:
                f.seek(self._make_log_pos)
                new_data = f.read()
                self._make_log_pos = f.tell()

            for line in new_data.splitlines():
                line = line.rstrip()
                if not line:
                    continue
                t = Text()
                t.append(escape(line), style="dim")
                self._log_lines.append(t)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # JSONL entry formatting
    # ──────────────────────────────────────────────────────────────────────

    def _format_entry(self, entry: dict) -> Optional[Text]:
        etype = entry.get("type")

        if etype == "metadata":
            model = str(entry.get("model") or "")
            t = Text()
            t.append("model: ", style="dim")
            t.append(escape(model), style="dim italic")
            return t

        if etype == "tool_call":
            turn = entry.get("turn", "?")
            tool = str(entry.get("tool") or "?")
            inp = entry.get("input") or {}
            result_raw = str(entry.get("result") or "")

            # Summarise input — show up to 2 key=value pairs
            if isinstance(inp, dict):
                parts = []
                for k, v in list(inp.items())[:2]:
                    s = str(v)
                    if "\n" in s:
                        s = s.split("\n")[0] + "…"
                    if len(s) > 55:
                        s = s[:55] + "…"
                    parts.append(f"{k}={s}")
                inp_str = ", ".join(parts)
            else:
                inp_str = str(inp)[:80]

            # Summarise result
            result_display = result_raw.replace("\n", " ").strip()
            if len(result_display) > 120:
                result_display = result_display[:120] + "…"

            t = Text()
            t.append(f"[T{turn}] ", style="bold cyan")
            t.append(tool, style="bold yellow")
            t.append(f"({escape(inp_str)})\n")
            t.append("     → ", style="dim")
            t.append(escape(result_display), style="dim")
            return t

        if etype == "assistant":
            turn = entry.get("turn", "?")
            stop = entry.get("stop_reason", "")
            content = entry.get("content") or []

            text_parts: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        tx = block.get("text", "").strip()
                        if tx:
                            text_parts.append(tx)

            if not text_parts:
                return None

            text = " ".join(text_parts)
            if len(text) > 220:
                text = text[:220] + "…"

            t = Text()
            if stop == "end_turn":
                t.append("✓ ", style="bold green")
            else:
                t.append("· ", style="bold blue")
            t.append(f"[T{turn}] ", style="bold")
            t.append(escape(text))
            return t

        if etype == "result":
            final = str(entry.get("final_text") or "").strip()
            if not final:
                return None
            if len(final) > 220:
                final = final[:220] + "…"
            t = Text()
            t.append("✓ Result: ", style="bold green")
            t.append(escape(final))
            return t

        return None

    # ──────────────────────────────────────────────────────────────────────
    # Phase helpers
    # ──────────────────────────────────────────────────────────────────────

    def _phase_trio(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (previous, current, next) phase strings."""
        current = str(self._session.get("current_phase") or "")
        if not current:
            return None, None, None

        if current in TERMINAL_PHASES:
            prev = PHASE_ORDER[-1] if PHASE_ORDER else None
            return prev, current, None

        try:
            idx = PHASE_ORDER.index(current)
        except ValueError:
            return None, current, None

        prev = PHASE_ORDER[idx - 1] if idx > 0 else None
        nxt = PHASE_ORDER[idx + 1] if idx < len(PHASE_ORDER) - 1 else None
        return prev, current, nxt

    # ──────────────────────────────────────────────────────────────────────
    # Rendering
    # ──────────────────────────────────────────────────────────────────────

    def _render(self, console: "Console") -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="phases", size=7),
            Layout(name="log"),
        )

        layout["header"].update(self._render_header())
        layout["phases"].update(self._render_phases())
        layout["log"].update(self._render_log(console))
        return layout

    def _render_header(self) -> Panel:
        wi = self._session.get("work_item") or {}
        work_id = str(wi.get("work_id") or "waiting for session…")
        attempt = int(self._session.get("attempt_number") or 0)
        max_att = int(self._session.get("max_attempts") or 3)
        ts = datetime.now().strftime("%H:%M:%S")

        t = Text(justify="center")
        t.append("spec-to-pr", style="bold white")
        t.append("  │  ", style="dim")
        t.append(work_id, style="bold cyan")
        t.append("  │  ", style="dim")
        t.append(f"attempt {attempt + 1}/{max_att}", style="yellow")
        t.append("  │  ", style="dim")
        t.append(ts, style="dim")

        return Panel(t, style="blue", padding=(0, 1))

    def _render_phases(self) -> Panel:
        prev, current, nxt = self._phase_trio()

        grid = Table.grid(padding=(0, 3), expand=True)
        grid.add_column(justify="right", ratio=1)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="left", ratio=1)

        grid.add_row(
            self._phase_cell(prev, "prev"),
            self._phase_cell(current, "current"),
            self._phase_cell(nxt, "next"),
        )
        grid.add_row(
            Text("← previous", style="dim") if prev else Text(""),
            Text("▼ current", style="dim yellow"),
            Text("next →", style="dim") if nxt else Text(""),
        )

        return Panel(
            grid,
            title="[bold]Phase Progress[/bold]",
            border_style="yellow",
            padding=(1, 2),
        )

    def _phase_cell(self, phase: Optional[str], role: str) -> Text:
        if phase is None:
            if role == "prev":
                return Text("(start)", style="dim")
            if role == "next":
                return Text("(done)", style="dim")
            return Text("Waiting…", style="dim italic")

        label = PHASE_LABELS.get(phase, phase)

        t = Text()
        if role == "current":
            if phase == "complete":
                t.append("✓ ", style="bold green")
                t.append(label.upper(), style="bold green")
            elif phase in TERMINAL_PHASES:
                t.append("✗ ", style="bold red")
                t.append(label.upper(), style="bold red")
            else:
                t.append("▶ ", style="bold yellow")
                t.append(label.upper(), style="bold yellow")
        elif role == "prev":
            t.append("✓ ", style="green")
            t.append(label, style="green")
        else:
            t.append("○ ", style="dim")
            t.append(label, style="dim")

        return t

    def _render_log(self, console: "Console") -> Panel:
        if self._make_log_file is not None:
            conv_name = self._make_log_file.name
        elif self._conv_file is not None:
            conv_name = self._conv_file.name
        else:
            conv_name = "none"

        if self._log_lines:
            visible = self._log_lines[-DISPLAY_LOG_LINES:]
            log_text = Text()
            for i, entry_text in enumerate(visible):
                log_text.append_text(entry_text)
                if i < len(visible) - 1:
                    log_text.append("\n")
        else:
            log_text = Text("Waiting for conversation log…", style="dim italic", justify="center")

        return Panel(
            log_text,
            title=f"[bold]Log[/bold]  [dim]{escape(conv_name)}[/dim]",
            border_style="blue",
        )
