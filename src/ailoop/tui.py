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
from textual.widgets import Button, DataTable, Header, Input, Static

from .memory import MemoryStore
from .service import LoopService
from .stats import STATUS_ICONS
from .tasks import parse_task_file

FilterMode = Literal["running", "active", "all"]
LogKind = Literal["stdout", "stderr", "prompt", "events", "memory"]
MemoryFilter = Literal["all", "favorites", "history", "archived", "presets"]

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

    #summary_bar {
        height: auto;
        padding: 0 1 1 1;
        color: #cbd5e1;
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

    #log_meta {
        color: #94a3b8;
        margin-bottom: 1;
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
        ("5", "set_log_memory", "Memory"),
        ("6", "set_log_memory_favorites", "Favorites"),
        ("7", "set_log_memory_history", "History"),
        ("m", "set_log_memory_presets", "Presets"),
        ("0", "set_log_memory_archived", "Archived"),
        ("b", "memory_label_prev", "Prev Label"),
        ("n", "memory_label_next", "Next Label"),
        ("c", "memory_label_clear", "Clear Label"),
        ("o", "memory_scope_toggle", "Toggle Memory Scope"),
        ("/", "memory_query_focus", "Focus Query"),
        ("escape", "memory_query_clear", "Clear Query"),
        ("8", "memory_replay", "Replay Memory"),
        ("9", "memory_favorite", "Toggle Favorite"),
        ("v", "memory_restore", "Restore Memory"),
        ("z", "memory_archive", "Archive Memory"),
        ("x", "memory_delete", "Delete Memory"),
        ("[", "memory_prev", "Prev Memory"),
        ("]", "memory_next", "Next Memory"),
        ("a", "filter_active", "Active"),
        ("g", "filter_running", "Running"),
        ("l", "filter_all", "All"),
    ]

    selected_loop_id: reactive[str | None] = reactive(None)
    filter_mode: reactive[FilterMode] = reactive("running")
    log_kind: reactive[LogKind] = reactive("stdout")
    memory_filter: reactive[MemoryFilter] = reactive("all")
    memory_label: reactive[str | None] = reactive(None)
    memory_all_folders: reactive[bool] = reactive(False)
    memory_query: reactive[str] = reactive("")
    memory_index: reactive[int] = reactive(0)
    memory_archive_armed: reactive[bool] = reactive(False)
    memory_delete_armed: reactive[bool] = reactive(False)
    delete_armed: reactive[bool] = reactive(False)

    def _summary_counts(self) -> tuple[int, int, int]:
        states = self.service.list_loops()
        running = sum(1 for state in states if state.status in RUNNING_STATUSES)
        active = sum(1 for state in states if state.status in ACTIVE_STATUSES)
        return len(states), active, running

    def __init__(self, config_path: Path, loop_id: str | None = None) -> None:
        super().__init__()
        self.config_path = config_path
        from .config import load_app_config

        app_config = load_app_config(config_path)
        self.service = LoopService(Path(app_config.paths.state_dir), emit_output=False)
        self.memory = MemoryStore(Path(app_config.paths.state_dir))
        self.initial_loop_id = loop_id
        if loop_id is not None:
            self.filter_mode = "all"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading...", id="summary_bar")
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
                    yield Button("5 memory", id="log-memory")
                    yield Button("6 favorites", id="log-memory-favorites")
                    yield Button("7 history", id="log-memory-history")
                    yield Button("m presets", id="log-memory-presets")
                    yield Button("0 archived", id="log-memory-archived")
                    yield Button("b label<", id="memory-label-prev")
                    yield Button("n label>", id="memory-label-next")
                    yield Button("c labelx", id="memory-label-clear")
                    yield Button("o scope", id="memory-scope-toggle")
                    yield Button("8 replay", id="memory-replay")
                    yield Button("9 favorite", id="memory-favorite")
                    yield Button("v restore", id="memory-restore")
                    yield Button("z archive", id="memory-archive")
                    yield Button("x delete", id="memory-delete")
                yield Input(placeholder="memory query", id="memory-query")
                yield Static(id="log_meta")
                yield Static(id="log_view")
            with Vertical(id="details"):
                yield Static("ℹ details", classes="panel-title")
                yield Static(id="detail_view")
        yield Static("loading...", id="help_bar")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Loop", "State", "Prog", "Agent", "Fail")
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()

    def _render_summary_bar(self) -> None:
        total = len(self.service.list_loops())
        active = sum(1 for state in self.service.list_loops() if state.status in ACTIVE_STATUSES)
        running = sum(1 for state in self.service.list_loops() if state.status in RUNNING_STATUSES)
        paused = sum(1 for state in self.service.list_loops() if state.status == "paused")
        failed = sum(1 for state in self.service.list_loops() if state.status == "failed")
        selected = self._selected_state()
        summary_text = self._summary_bar_text(
            total,
            active,
            running,
            paused,
            failed,
            selected,
            width=self.size.width,
        )
        self.query_one("#summary_bar", Static).update(summary_text)

    def _summary_bar_text(
        self,
        total: int,
        active: int,
        running: int,
        paused: int,
        failed: int,
        state: object | None,
        width: int | None = None,
    ) -> str:
        actual_width = width or 0
        compact = bool(actual_width and actual_width <= 80)
        selected_text = self._summary_selected_text(state, width=actual_width)
        if compact:
            base = f"all {total} · act {active} · run {running} · pause {paused} · fail {failed}"
        else:
            base = (
                f"all {total} · active {active} · running {running} · paused {paused} · "
                f"failed {failed}"
            )
        if self.log_kind == "memory":
            if compact:
                return f"{base} · f {self.filter_mode} · {selected_text}"
            return f"{base} · filter {self.filter_mode} · {selected_text}"
        if compact:
            return f"{base} · f {self.filter_mode} · {self.log_kind} · {selected_text}"
        return f"{base} · filter {self.filter_mode} · log {self.log_kind} · {selected_text}"

    def _summary_selected_text(self, state: object | None, width: int | None = None) -> str:
        actual_width = width or 0
        compact = bool(actual_width and actual_width <= 80)
        if self.log_kind == "memory":
            entry = self._primary_memory_entry()
            label_count = len(self._memory_labels())
            if entry is None:
                if compact:
                    return f"mem {self.memory_filter} · lab {label_count} · sel none"
                return f"memory {self.memory_filter} · labels {label_count} · selected none"
            if compact:
                compact_id = entry.id[:8]
                return (
                    f"mem {self.memory_filter} · lab {label_count} · "
                    f"sel {compact_id}"
                )
            return f"memory {self.memory_filter} · labels {label_count} · selected {entry.id}"
        if state is None:
            if compact:
                return "sel none"
            return "selected none"
        if compact:
            return f"sel {short_loop_id(state.loop_id)} · {short_status(state.status)}"  # type: ignore[attr-defined]
        return f"selected {short_loop_id(state.loop_id)} · {short_status(state.status)}"  # type: ignore[attr-defined]

    def _footer_base_text(self, width: int | None = None) -> str:
        actual_width = self.size.width if width is None else width
        if actual_width and actual_width <= 80:
            return "↑↓ g/a/l 1-7/m/0 r q"
        return "nav ↑↓/click · filters g/a/l · logs 1/2/3/4/5/6/7/m/0 · r refresh · q quit"

    def _memory_help_text(self, width: int | None = None) -> str:
        actual_width = self.size.width if width is None else width
        compact = bool(actual_width and actual_width <= 80)
        memory_actions = []
        label_count = len(self._memory_labels())
        if self._primary_memory_entry() is not None:
            memory_actions.extend(
                [
                    "[ prev",
                    "] next",
                    "b label<",
                    "n label>",
                    "c labelx",
                    "o scope",
                    "/ query",
                    "esc queryx",
                    "8 replay",
                    "9 favorite",
                    "z archive",
                    "x delete",
                ]
            )
            if self._primary_memory_entry().archived:  # type: ignore[union-attr]
                memory_actions.append("v restore")
        if self.memory_archive_armed:
            memory_actions.append("z confirm")
        if self.memory_delete_armed:
            memory_actions.append("x confirm")
        if compact:
            action_text = (
                " ".join(token.split()[0] for token in memory_actions) if memory_actions else "ro"
            )
        else:
            action_text = " · ".join(memory_actions) if memory_actions else "read only"
        base = self._footer_base_text(width=actual_width)
        entries = len(self._memory_entries())
        labels = len(self._memory_labels())
        if compact:
            return (
                f"{base} · {self.memory_filter} {self.memory_label or '-'} "
                f"{self.memory_query or '-'} "
                f"{self._memory_scope_text()} {entries}e {labels}/{label_count}l {action_text}"
            )
        return (
            f"{base} · memory {self.memory_filter} · label {self.memory_label or '-'} · "
            f"query {self.memory_query or '-'} · "
            f"scope {self._memory_scope_text()} · entries {entries} · "
            f"labels {labels}/{label_count} · "
            f"actions {action_text}"
        )

    def _sync_button_state(self) -> None:
        state = self._selected_state()
        status = state.status if state is not None else None
        can_pause = status in {"running", "pause_requested"}
        can_resume = status in {"paused", "stopped", "failed", "idle"}
        can_stop = status in {"running", "pause_requested", "paused"}
        can_remove = status not in {None, "running", "pause_requested", "stop_requested"}
        memory_entry = self._primary_memory_entry()
        for button_id, active in {
            "filter-running": self.filter_mode == "running",
            "filter-active": self.filter_mode == "active",
            "filter-all": self.filter_mode == "all",
            "log-stdout": self.log_kind == "stdout",
            "log-stderr": self.log_kind == "stderr",
            "log-prompt": self.log_kind == "prompt",
            "log-events": self.log_kind == "events",
            "log-memory": self.log_kind == "memory" and self.memory_filter == "all",
            "log-memory-favorites": self.log_kind == "memory" and self.memory_filter == "favorites",
            "log-memory-history": self.log_kind == "memory" and self.memory_filter == "history",
            "log-memory-presets": self.log_kind == "memory" and self.memory_filter == "presets",
            "log-memory-archived": self.log_kind == "memory" and self.memory_filter == "archived",
            "memory-scope-toggle": self.log_kind == "memory" and self.memory_all_folders,
        }.items():
            self.query_one(f"#{button_id}", Button).set_class(active, "active")
        self.query_one("#pause", Button).disabled = not can_pause
        self.query_one("#resume", Button).disabled = not can_resume
        self.query_one("#stop", Button).disabled = not can_stop
        self.query_one("#remove", Button).disabled = not can_remove
        self.query_one("#memory-replay", Button).disabled = not (
            self.log_kind == "memory" and memory_entry is not None
        )
        self.query_one("#memory-favorite", Button).disabled = not (
            self.log_kind == "memory" and memory_entry is not None
        )
        self.query_one("#memory-restore", Button).disabled = not (
            self.log_kind == "memory" and memory_entry is not None and memory_entry.archived
        )
        self.query_one("#memory-archive", Button).disabled = not (
            self.log_kind == "memory" and memory_entry is not None and not memory_entry.archived
        )
        self.query_one("#memory-archive", Button).label = (
            "z confirm" if self.memory_archive_armed else "z archive"
        )
        self.query_one("#memory-delete", Button).disabled = not (
            self.log_kind == "memory" and memory_entry is not None
        )
        self.query_one("#memory-delete", Button).label = (
            "x confirm" if self.memory_delete_armed else "x delete"
        )
        self.query_one("#remove", Button).label = "✖ Confirm" if self.delete_armed else "✖ Delete"
        self._render_help_bar(state)

    def _render_help_bar(self, state: object | None) -> None:
        bar = self.query_one("#help_bar", Static)
        base = self._footer_base_text()
        if self.log_kind == "memory":
            bar.update(self._memory_help_text())
            return
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

    def _empty_loop_message(self) -> str:
        total, active, running = self._summary_counts()
        if total == 0:
            return (
                "No loops yet.\n\n"
                "Start with:\n"
                '  ailoop run "Review the repo"\n\n'
                "Tip:\n"
                "  q quit · r refresh"
            )
        if self.filter_mode == "running" and running == 0:
            return (
                "No running loops in this filter.\n\n"
                f"All loops: {total} · active: {active}\n"
                "Press l for all or a for active."
            )
        if self.filter_mode == "active" and active == 0:
            return (
                "No active loops in this filter.\n\n"
                f"All loops: {total} · running: {running}\n"
                "Press l for all or g for running."
            )
        return "No loops in the current filter."

    def _unselected_detail_message(self) -> str:
        total, active, running = self._summary_counts()
        return "\n".join(
            [
                "overview",
                f"loops: {total}",
                f"active: {active}",
                f"running: {running}",
                "",
                "choose a loop with ↑↓ or click a row",
                "filters: g running · a active · l all",
                "logs: 1 stdout · 2 stderr · 3 prompt · 4 events",
                "      5 memory · 6 favorites · 7 history · m presets · 0 archived",
            ]
        )

    def _memory_entries(self):
        entries = self._memory_entries_base()
        if self.memory_label is not None:
            entries = [entry for entry in entries if self.memory_label in entry.labels]
        if self.memory_query:
            query = self.memory_query.lower()
            entries = [
                entry
                for entry in entries
                if query in " ".join([entry.id, entry.title, *entry.labels]).lower()
            ]
        return entries

    def _memory_entries_base(self):
        folder = None if self.memory_all_folders else Path.cwd()
        if self.memory_filter == "favorites":
            return self.memory.list_entries(
                folder=folder,
                favorites_only=True,
                all_folders=self.memory_all_folders,
            )
        if self.memory_filter == "history":
            return self.memory.list_entries(
                folder=folder,
                kind="history",
                all_folders=self.memory_all_folders,
            )
        if self.memory_filter == "presets":
            return self.memory.list_entries(
                folder=folder,
                kind="preset",
                all_folders=self.memory_all_folders,
            )
        if self.memory_filter == "archived":
            return [
                entry
                for entry in self.memory.list_entries(
                    folder=folder,
                    include_archived=True,
                    all_folders=self.memory_all_folders,
                )
                if entry.archived
            ]
        return self.memory.list_entries(folder=folder, all_folders=self.memory_all_folders)

    def _memory_labels(self) -> list[str]:
        return sorted({label for entry in self._memory_entries_base() for label in entry.labels})

    def _selected_memory_index(self) -> int:
        entries = self._memory_entries()
        if not entries:
            return 0
        return max(0, min(self.memory_index, len(entries) - 1))

    def _primary_memory_entry(self):
        entries = self._memory_entries()
        if not entries:
            return None
        return entries[self._selected_memory_index()]

    def _memory_log_meta(self) -> str:
        entries = self._memory_entries()
        favorites = sum(1 for entry in entries if entry.favorite)
        selected = self._selected_memory_index() + 1 if entries else 0
        return (
            f"source memory · filter {self.memory_filter} · label {self.memory_label or '-'} · "
            f"query {self.memory_query or '-'} · "
            f"selected {selected}/{len(entries)} · "
            f"favorites {favorites} · scope {self._memory_scope_text()}"
        )

    def _memory_scope_text(self) -> str:
        return "all-folders" if self.memory_all_folders else "cwd"

    def _memory_log_text(self) -> str:
        entries = self._memory_entries()
        if entries:
            selected_index = self._selected_memory_index()
            lines = [
                "Sel  ID             Kind     Fav  Used  Labels       Title",
                "---  -------------  -------  ---  ----  -----------  -----",
            ]
            for index, entry in enumerate(entries):
                marker = ">" if index == selected_index else " "
                star = "★" if entry.favorite else "-"
                labels = ",".join(entry.labels[:2]) or "-"
                lines.append(
                    f" {marker}   {entry.id:<13}  {entry.kind:<7}  {star:<3}  "
                    f"{entry.use_count:<4}  "
                    f"{labels:<11}  {entry.title}"
                )
            return "\n".join(lines)
        if self.memory_filter == "archived":
            return (
                "No archived memory entries found.\n\n"
                f"scope: {self._memory_scope_text()} · label: {self.memory_label or '-'} · "
                f"query: {self.memory_query or '-'}\n"
                "Archive one from the memory list with z twice, or press 5 for all entries.\n"
                f"Press o to {self._memory_scope_toggle_hint()}."
            )
        return (
            "No memory entries found.\n\n"
            f"scope: {self._memory_scope_text()} · filter: {self.memory_filter} · "
            f"label: {self.memory_label or '-'} · query: {self.memory_query or '-'}\n"
            "Create one with:\n"
            '  ailoop memory save "Quick review" "Review the repo" --runner opencode\n\n'
            f"Then press {self._memory_filter_hint()} to refresh this list. "
            f"Press o to {self._memory_scope_toggle_hint()}."
        )

    def _memory_filter_hint(self) -> str:
        return {
            "all": "5",
            "favorites": "6",
            "history": "7",
            "presets": "m",
            "archived": "0",
        }[self.memory_filter]

    def _memory_scope_toggle_hint(self) -> str:
        return "return to cwd scope" if self.memory_all_folders else "show all folders"

    def _empty_memory_detail_text(self) -> str:
        lines = [
            "memory overview",
            f"filter: {self.memory_filter}",
            f"scope: {self._memory_scope_text()}",
            f"label: {self.memory_label or '-'}",
            f"query: {self.memory_query or '-'}",
            "",
        ]
        if self.memory_filter == "archived":
            lines.extend(
                [
                    "no archived entries match this view",
                    "archive one from memory mode with z twice",
                    "press 5 to return to all entries",
                    f"press o to {self._memory_scope_toggle_hint()}",
                ]
            )
        else:
            lines.extend(
                [
                    "no memory entry is selected",
                    "save one with ailoop memory save ...",
                    f"press {self._memory_filter_hint()} to refresh this filter",
                    f"press o to {self._memory_scope_toggle_hint()}",
                ]
            )
        return "\n".join(lines)

    def _memory_detail_text(self) -> str:
        entry = self._primary_memory_entry()
        if entry is None:
            return self._empty_memory_detail_text()
        show_command = f"ailoop memory show {entry.id}"
        edit_command = f"ailoop memory edit {entry.id} --title {shlex.quote(entry.title)}"
        favorite_command = (
            f"ailoop memory favorite {entry.id}"
            if not entry.favorite
            else f"ailoop memory favorite {entry.id} --off"
        )
        archive_command = (
            f"ailoop memory archive {entry.id} --off"
            if entry.archived
            else f"ailoop memory archive {entry.id}"
        )
        return "\n".join(
            [
                f"memory {entry.id}",
                "",
                "summary",
                f"title: {entry.title}",
                f"kind: {entry.kind}",
                f"favorite: {entry.favorite}",
                f"archived: {entry.archived}",
                f"labels: {', '.join(entry.labels) or '-'}",
                f"active label: {self.memory_label or '-'}",
                f"scope: {self._memory_scope_text()}",
                f"query: {self.memory_query or '-'}",
                f"available labels: {len(self._memory_labels())}",
                "",
                "usage",
                f"used: {entry.use_count}",
                f"last used: {entry.last_used_at or '-'}",
                f"versions: {entry.latest_version}",
                "",
                "run",
                f"runner: {entry.current.runner}",
                f"agent: {entry.current.agent or '-'}",
                f"steps: {entry.current.steps}",
                "",
                "commands",
                show_command,
                edit_command,
                favorite_command,
                archive_command,
                "",
                "actions",
                "[ previous entry",
                "] next entry",
                "b previous label",
                "n next label",
                "c clear label",
                "o toggle scope",
                "/ focus query",
                "esc clear query",
                "8 replay top entry",
                "9 toggle favorite",
                "v restore selected entry",
                "z archive selected entry",
                "x delete selected entry",
            ]
        )

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

        if not states:
            table.add_row("-", self.filter_mode, "-", "-", key="empty")

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
                (state.run_config.agent or "-")[:12],
                str(state.consecutive_failures),
                key=state.loop_id,
            )

        if states and self.selected_loop_id:
            try:
                table.move_cursor(row=table.get_row_index(self.selected_loop_id))
            except Exception:
                pass
        self._sync_button_state()
        self._render_summary_bar()
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
        log_meta = self.query_one("#log_meta", Static)
        log_view = self.query_one("#log_view", Static)
        state = self._selected_state()
        if self.log_kind == "memory":
            detail.update(self._memory_detail_text())
            log_meta.update(self._memory_log_meta())
            log_view.update(self._memory_log_text())
            return
        if state is None:
            detail.update(self._unselected_detail_message())
            log_meta.update(f"source {self.log_kind} · no loop selected")
            log_view.update(self._empty_loop_message())
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
            "summary",
            f"status: {short_status(state.status)}",
            f"runner: {state.run_config.runner}",
            f"progress: {progress}",
            f"last: {state.last_summary or '-'}",
            "",
            "status",
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
        log_meta.update(
            f"source {self.log_kind} · loop {short_loop_id(state.loop_id)} · refresh 1s"
        )
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
        elif button_id == "log-memory-presets":
            self.action_set_log_memory_presets()
        elif button_id == "memory-label-prev":
            self.action_memory_label_prev()
        elif button_id == "memory-label-next":
            self.action_memory_label_next()
        elif button_id == "memory-label-clear":
            self.action_memory_label_clear()
        elif button_id == "memory-scope-toggle":
            self.action_memory_scope_toggle()
        elif button_id == "memory-replay":
            self.action_memory_replay()
        elif button_id == "memory-favorite":
            self.action_memory_favorite()
        elif button_id == "memory-restore":
            self.action_memory_restore()
        elif button_id == "memory-archive":
            self.action_memory_archive()
        elif button_id == "memory-delete":
            self.action_memory_delete()
        elif button_id and button_id.startswith("log-"):
            self.log_kind = button_id.removeprefix("log-")  # type: ignore[assignment]
            self._sync_button_state()
            self._render_selected()

    @on(Input.Changed, "#memory-query")
    def on_memory_query_changed(self, event: Input.Changed) -> None:
        self._apply_memory_query(event.value)

    def _apply_memory_query(self, value: str) -> None:
        self.memory_query = value.strip()
        self.memory_index = 0
        self.memory_archive_armed = False
        self.memory_delete_armed = False
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

    def _spawn_replay(self, entry_id: str) -> None:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ailoop.cli",
                "--quiet",
                "--config",
                str(self.config_path),
                "replay",
                entry_id,
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

    def _activate_memory_filter(self, memory_filter: MemoryFilter) -> None:
        self.log_kind = "memory"
        self.memory_filter = memory_filter
        self.memory_index = 0
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._sync_button_state()
        self._render_selected()

    def action_set_log_memory(self) -> None:
        self._activate_memory_filter("all")

    def action_set_log_memory_favorites(self) -> None:
        self._activate_memory_filter("favorites")

    def action_set_log_memory_history(self) -> None:
        self._activate_memory_filter("history")

    def action_set_log_memory_presets(self) -> None:
        self._activate_memory_filter("presets")

    def action_set_log_memory_archived(self) -> None:
        self._activate_memory_filter("archived")

    def _move_memory_selection(self, delta: int) -> None:
        entries = self._memory_entries()
        if not entries:
            self.memory_index = 0
            return
        self.memory_index = (self._selected_memory_index() + delta) % len(entries)

    def action_memory_prev(self) -> None:
        self._move_memory_selection(-1)
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._sync_button_state()
        self._render_selected()

    def _move_memory_label(self, delta: int) -> None:
        labels = self._memory_labels()
        if not labels:
            self.memory_label = None
            self.memory_index = 0
            return
        if self.memory_label not in labels:
            self.memory_label = labels[0]
            self.memory_index = 0
            return
        index = labels.index(self.memory_label)
        self.memory_label = labels[(index + delta) % len(labels)]
        self.memory_index = 0

    def action_memory_label_prev(self) -> None:
        self._move_memory_label(-1)
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._sync_button_state()
        self._render_selected()

    def action_memory_label_next(self) -> None:
        self._move_memory_label(1)
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._sync_button_state()
        self._render_selected()

    def action_memory_label_clear(self) -> None:
        self.memory_label = None
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self.memory_index = 0
        self._sync_button_state()
        self._render_selected()

    def action_memory_scope_toggle(self) -> None:
        if self.log_kind != "memory":
            return
        self.memory_all_folders = not self.memory_all_folders
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self.memory_index = 0
        self._sync_button_state()
        self._render_selected()

    def action_memory_query_focus(self) -> None:
        if self.log_kind != "memory":
            return
        self.query_one("#memory-query", Input).focus()

    def action_memory_query_clear(self) -> None:
        if self.log_kind != "memory" and not self.memory_query:
            return
        self._apply_memory_query("")
        self.query_one("#memory-query", Input).value = ""

    def action_memory_next(self) -> None:
        self._move_memory_selection(1)
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._sync_button_state()
        self._render_selected()

    def action_memory_replay(self) -> None:
        entry = self._primary_memory_entry()
        if entry is None:
            return
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._spawn_replay(entry.id)
        self.notify(f"replay sent: {entry.id}")
        self.refresh_data()

    def action_memory_favorite(self) -> None:
        entry = self._primary_memory_entry()
        if entry is None:
            return
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        updated = self.memory.edit(entry.id, favorite=not entry.favorite, folder=Path.cwd())
        state = "favorite on" if updated.favorite else "favorite off"
        self.notify(f"{state}: {updated.id}")
        self.refresh_data()

    def action_memory_restore(self) -> None:
        entry = self._primary_memory_entry()
        if entry is None or not entry.archived:
            return
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self.memory.edit(entry.id, archived=False, folder=Path.cwd())
        self.notify(f"memory restored: {entry.id}")
        self.refresh_data()

    def action_memory_archive(self) -> None:
        entry = self._primary_memory_entry()
        if entry is None:
            return
        if not self.memory_archive_armed:
            self.memory_archive_armed = True
            self.memory_delete_armed = False
            self.notify(f"press z again to archive memory: {entry.id}")
            self._sync_button_state()
            return
        self.memory.edit(entry.id, archived=True, folder=Path.cwd())
        self.memory_archive_armed = False
        self.memory_index = 0
        self.notify(f"memory archived: {entry.id}")
        self.refresh_data()

    def action_memory_delete(self) -> None:
        entry = self._primary_memory_entry()
        if entry is None:
            return
        if not self.memory_delete_armed:
            self.memory_delete_armed = True
            self.memory_archive_armed = False
            self.notify(f"press x again to delete memory: {entry.id}")
            self._sync_button_state()
            return
        self.memory.delete(entry.id, folder=Path.cwd())
        self.memory_delete_armed = False
        self.memory_index = 0
        self.notify(f"memory deleted: {entry.id}")
        self.refresh_data()

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
