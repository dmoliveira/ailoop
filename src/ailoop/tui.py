from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Literal

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, DataTable, Header, Static

from .service import LoopService
from .stats import STATUS_ICONS
from .tasks import parse_task_file

FilterMode = Literal["running", "active", "all"]
LogKind = Literal["stdout", "stderr", "prompt", "events"]

RUNNING_STATUSES = {"running", "pause_requested", "stop_requested"}
ACTIVE_STATUSES = RUNNING_STATUSES | {"paused", "idle"}


def launch_in_tmux(config_path: Path, loop_id: str | None = None) -> None:
    command_text = (
        f"cd {shlex.quote(str(Path.cwd()))} && {shlex.quote(sys.executable)} -m ailoop.cli "
        f"--config {shlex.quote(str(config_path))} tui --tmux-session"
    ) + (f" --loop-id {shlex.quote(loop_id)}" if loop_id else "")
    command = [
        "tmux",
        "new-session",
        "-A",
        "-s",
        "ailoop-tui",
        command_text,
    ]
    subprocess.run(command, check=True)


def tail_text(path: Path, lines: int = 400) -> str:
    if not path.exists():
        return "<missing>"
    chunks = path.read_text().splitlines()
    return "\n".join(chunks[-lines:])


def read_events(path: Path, limit: int = 80) -> str:
    if not path.exists():
        return "<missing>"
    rows = path.read_text().splitlines()[-limit:]
    return "\n".join(rows)


def short_status(status: str) -> str:
    return {
        "pause_requested": "pausing",
        "stop_requested": "stopping",
    }.get(status, status)


def short_loop_id(loop_id: str) -> str:
    return loop_id if len(loop_id) <= 12 else loop_id[:12]


class LoopDashboard(App[None]):
    CSS = """
    Screen {
        background: #08111f;
        color: #f8fafc;
    }

    #main {
        height: 1fr;
    }

    #sidebar {
        width: 38;
        min-width: 30;
        border: round #243244;
        padding: 1;
        background: #0b1220;
    }

    #content {
        width: 1fr;
        padding: 0 1;
    }

    #details {
        width: 42;
        min-width: 32;
        border: round #243244;
        padding: 1;
        background: #0b1220;
    }

    #loops {
        height: 1fr;
        margin-top: 1;
    }

    .panel-title {
        text-style: bold;
        color: #38bdf8;
        margin-bottom: 1;
    }

    .toolbar {
        height: auto;
        margin-bottom: 1;
    }

    .toolbar Button {
        margin-right: 1;
        margin-bottom: 1;
    }

    .toolbar Button.active {
        background: #1d4ed8;
        color: #f8fafc;
        text-style: bold;
        border: round #38bdf8;
    }

    #log_view {
        border: round #243244;
        height: 1fr;
        padding: 1;
        background: #0b1220;
    }

    #detail_view {
        height: 1fr;
    }

    #help_bar {
        height: auto;
        color: #94a3b8;
        padding: 0 1 1 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_data", "Refresh"),
        ("p", "pause_selected", "Pause"),
        ("u", "resume_selected", "Resume"),
        ("s", "stop_selected", "Stop"),
        ("d", "remove_selected", "Delete"),
        ("1", "set_log_stdout", "Stdout"),
        ("2", "set_log_stderr", "Stderr"),
        ("3", "set_log_prompt", "Prompt"),
        ("4", "set_log_events", "Events"),
        ("a", "filter_active", "Active"),
        ("g", "filter_running", "Running"),
        ("l", "filter_all", "All"),
    ]

    selected_loop_id: reactive[str | None] = reactive(None)
    filter_mode: reactive[FilterMode] = reactive("running")
    log_kind: reactive[LogKind] = reactive("stdout")
    delete_armed: reactive[bool] = reactive(False)

    def __init__(self, config_path: Path, loop_id: str | None = None) -> None:
        super().__init__()
        self.config_path = config_path
        from .config import load_app_config

        app_config = load_app_config(config_path)
        self.service = LoopService(Path(app_config.paths.state_dir), emit_output=False)
        self.initial_loop_id = loop_id
        if loop_id is not None:
            self.filter_mode = "all"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("🔁 loops", classes="panel-title")
                with Horizontal(classes="toolbar"):
                    yield Button("g running", id="filter-running")
                    yield Button("a active", id="filter-active")
                    yield Button("l all", id="filter-all")
                yield DataTable(id="loops", zebra_stripes=True)
            with Vertical(id="content"):
                with Horizontal(classes="toolbar"):
                    yield Button("⟳ Refresh", id="refresh")
                    yield Button("⏸ Pause", id="pause")
                    yield Button("▶ Resume", id="resume")
                    yield Button("⏹ Stop", id="stop")
                    yield Button("✖ Delete", id="remove")
                with Horizontal(classes="toolbar"):
                    yield Button("1 stdout", id="log-stdout")
                    yield Button("2 stderr", id="log-stderr")
                    yield Button("3 prompt", id="log-prompt")
                    yield Button("4 events", id="log-events")
                yield Static(id="log_view")
            with Vertical(id="details"):
                yield Static("ℹ details", classes="panel-title")
                yield Static(id="detail_view")
        yield Static("loading...", id="help_bar")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Loop", "State", "Prog", "Run")
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()

    def _sync_button_state(self) -> None:
        state = self._selected_state()
        status = state.status if state is not None else None
        can_pause = status in {"running", "pause_requested"}
        can_resume = status in {"paused", "stopped", "failed", "idle"}
        can_stop = status in {"running", "pause_requested", "paused"}
        can_remove = status not in {None, "running", "pause_requested", "stop_requested"}
        for button_id, active in {
            "filter-running": self.filter_mode == "running",
            "filter-active": self.filter_mode == "active",
            "filter-all": self.filter_mode == "all",
            "log-stdout": self.log_kind == "stdout",
            "log-stderr": self.log_kind == "stderr",
            "log-prompt": self.log_kind == "prompt",
            "log-events": self.log_kind == "events",
        }.items():
            self.query_one(f"#{button_id}", Button).set_class(active, "active")
        self.query_one("#pause", Button).disabled = not can_pause
        self.query_one("#resume", Button).disabled = not can_resume
        self.query_one("#stop", Button).disabled = not can_stop
        self.query_one("#remove", Button).disabled = not can_remove
        self.query_one("#remove", Button).label = "✖ Confirm" if self.delete_armed else "✖ Delete"
        self._render_help_bar(state)

    def _render_help_bar(self, state: object | None) -> None:
        bar = self.query_one("#help_bar", Static)
        base = "nav ↑↓/click · filters g/a/l · logs 1/2/3/4 · r refresh · q quit"
        if state is None:
            bar.update(base + " · no loop selected")
            return
        loop_state = state.status  # type: ignore[attr-defined]
        actions: list[str] = []
        if loop_state in {"running", "pause_requested"}:
            actions.append("p pause")
        if loop_state in {"paused", "stopped", "failed", "idle"}:
            actions.append("u resume")
        if loop_state in {"running", "pause_requested", "paused"}:
            actions.append("s stop")
        if loop_state not in {"running", "pause_requested", "stop_requested"}:
            actions.append("d delete")
        if self.delete_armed:
            actions.append("d confirm delete")
        action_text = " · ".join(actions) if actions else "read only"
        bar.update(f"{base} · actions {action_text}")

    def _filtered_loops(self):
        states = self.service.list_loops()
        if self.filter_mode == "running":
            return [state for state in states if state.status in RUNNING_STATUSES]
        if self.filter_mode == "active":
            return [state for state in states if state.status in ACTIVE_STATUSES]
        return states

    def refresh_data(self) -> None:
        states = self._filtered_loops()
        table = self.query_one(DataTable)
        table.clear(columns=False)
        if self.initial_loop_id and self.selected_loop_id is None:
            self.selected_loop_id = self.initial_loop_id
        if self.selected_loop_id and not any(s.loop_id == self.selected_loop_id for s in states):
            try:
                self.service.load_loop(self.selected_loop_id)
            except FileNotFoundError:
                pass
            else:
                self.filter_mode = "all"
                states = self._filtered_loops()
        if self.selected_loop_id is None and states:
            self.selected_loop_id = states[0].loop_id
        if self.selected_loop_id and not any(s.loop_id == self.selected_loop_id for s in states):
            self.selected_loop_id = states[0].loop_id if states else None

        for state in states:
            target = state.run_config.steps
            progress = (
                f"{state.completed_iterations}/∞"
                if target is None
                else f"{state.completed_iterations}/{target}"
            )
            icon = STATUS_ICONS.get(state.status, "•")
            table.add_row(
                short_loop_id(state.loop_id),
                f"{icon} {short_status(state.status)}",
                progress,
                state.run_config.runner,
                key=state.loop_id,
            )

        if states and self.selected_loop_id:
            try:
                table.move_cursor(row=table.get_row_index(self.selected_loop_id))
            except Exception:
                pass
        self._sync_button_state()
        self._render_selected()

    def _selected_state(self):
        if not self.selected_loop_id:
            return None
        try:
            return self.service.load_loop(self.selected_loop_id)
        except FileNotFoundError:
            return None

    def _render_selected(self) -> None:
        detail = self.query_one("#detail_view", Static)
        log_view = self.query_one("#log_view", Static)
        state = self._selected_state()
        if state is None:
            detail.update("No loop selected.")
            log_view.update("No log.")
            return

        target = state.run_config.steps
        progress = (
            f"{state.completed_iterations}/∞"
            if target is None
            else f"{state.completed_iterations}/{target}"
        )
        lines = [
            f"{STATUS_ICONS.get(state.status, '•')} {state.loop_id}",
            "",
            "status",
            f"state: {short_status(state.status)}",
            f"progress: {progress}",
            f"exit: {state.last_exit_code}",
            f"failures: {state.consecutive_failures}",
            "",
            "run",
            f"runner: {state.run_config.runner}",
            f"agent: {state.run_config.agent or '-'}",
            f"control: {state.control}",
            "",
            "timing",
            f"avg: {state.average_duration_seconds:.2f}s",
            f"total: {state.total_duration_seconds:.2f}s",
            "",
            "note",
            f"last: {state.last_summary or '-'}",
        ]
        if state.run_config.task_file:
            try:
                task_state = parse_task_file(
                    Path(state.run_config.task_file),
                    state.run_config.max_doing,
                )
                lines.extend(
                    [
                        "",
                        "task file",
                        f"task file: {state.run_config.task_file}",
                        (
                            f"tasks: to do {len(task_state.todo)} · doing "
                            f"{len(task_state.doing)} · done {len(task_state.done)}"
                        ),
                    ]
                )
            except Exception as exc:
                lines.extend(["", f"task file error: {exc}"])
        detail.update("\n".join(lines))

        paths = self.service.loop_paths(state.loop_id) if state.iterations else None
        if self.log_kind == "events":
            if paths:
                log_view.update(read_events(paths["events"]))
            else:
                log_view.update("No events yet.")
            return
        if not paths:
            log_view.update("No logs yet.")
            return
        log_view.update(tail_text(paths[self.log_kind]))

    @on(DataTable.RowSelected)
    def on_loop_selected(self, event: DataTable.RowSelected) -> None:
        self.selected_loop_id = str(event.row_key.value)
        self._render_selected()

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "refresh":
            self.refresh_data()
        elif button_id == "pause":
            self.action_pause_selected()
        elif button_id == "resume":
            self.action_resume_selected()
        elif button_id == "stop":
            self.action_stop_selected()
        elif button_id == "remove":
            self.action_remove_selected()
        elif button_id == "filter-running":
            self.action_filter_running()
        elif button_id == "filter-active":
            self.action_filter_active()
        elif button_id == "filter-all":
            self.action_filter_all()
        elif button_id and button_id.startswith("log-"):
            self.log_kind = button_id.removeprefix("log-")  # type: ignore[assignment]
            self._sync_button_state()
            self._render_selected()

    def _spawn_resume(self, loop_id: str) -> None:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ailoop.cli",
                "--quiet",
                "--config",
                str(self.config_path),
                "resume",
                loop_id,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def action_refresh_data(self) -> None:
        self.refresh_data()

    def action_filter_running(self) -> None:
        self.filter_mode = "running"
        self.delete_armed = False
        self.refresh_data()

    def action_filter_active(self) -> None:
        self.filter_mode = "active"
        self.delete_armed = False
        self.refresh_data()

    def action_filter_all(self) -> None:
        self.filter_mode = "all"
        self.delete_armed = False
        self.refresh_data()

    def action_set_log_stdout(self) -> None:
        self.log_kind = "stdout"
        self._sync_button_state()
        self._render_selected()

    def action_set_log_stderr(self) -> None:
        self.log_kind = "stderr"
        self._sync_button_state()
        self._render_selected()

    def action_set_log_prompt(self) -> None:
        self.log_kind = "prompt"
        self._sync_button_state()
        self._render_selected()

    def action_set_log_events(self) -> None:
        self.log_kind = "events"
        self._sync_button_state()
        self._render_selected()

    def action_pause_selected(self) -> None:
        if self.selected_loop_id:
            self.delete_armed = False
            self.service.request_control(self.selected_loop_id, "pause")
            self.refresh_data()

    def action_resume_selected(self) -> None:
        if self.selected_loop_id:
            self.delete_armed = False
            self._spawn_resume(self.selected_loop_id)
            self.notify(f"resume sent: {self.selected_loop_id}")
            self.refresh_data()

    def action_stop_selected(self) -> None:
        if self.selected_loop_id:
            self.delete_armed = False
            self.service.request_control(self.selected_loop_id, "stop")
            self.refresh_data()

    def action_remove_selected(self) -> None:
        if self.selected_loop_id:
            if not self.delete_armed:
                self.delete_armed = True
                self.notify(f"press d again to delete: {self.selected_loop_id}")
                self._sync_button_state()
                return
            try:
                self.service.remove_loop(self.selected_loop_id, force=True)
            except Exception as exc:
                self.notify(str(exc), severity="error")
                return
            self.notify(f"removed: {self.selected_loop_id}")
            self.delete_armed = False
            self.selected_loop_id = None
            self.refresh_data()


def run_tui(config_path: Path, loop_id: str | None = None) -> None:
    app = LoopDashboard(config_path=config_path, loop_id=loop_id)
    app.run()
