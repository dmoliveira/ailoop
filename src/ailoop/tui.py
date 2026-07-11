from __future__ import annotations

import json
import os
import re
import resource
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from textual import events, on
from textual.app import App, ComposeResult, ScreenStackError
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, Checkbox, DataTable, Header, Input, Select, Static, TextArea

from .memory import MemoryStore
from .paths import read_last_lines
from .service import LoopService
from .stats import STATUS_ICONS
from .tasks import parse_task_file, render_task_file_error

FilterMode = Literal["running", "active", "all"]
LogKind = Literal["stdout", "stderr", "prompt", "events", "memory", "metrics", "history"]
MemoryFilter = Literal["all", "favorites", "history", "archived", "presets"]

RUNNING_STATUSES = {"running", "pause_requested", "stop_requested"}
ACTIVE_STATUSES = RUNNING_STATUSES | {"paused", "idle"}
COMPACT_LAYOUT_WIDTH = 100


def launch_in_tmux(config_path: Path, loop_id: str | None = None) -> None:
    try:
        launch_dir = Path.cwd()
    except FileNotFoundError:
        launch_dir = Path.home()
    command_text = (
        f"cd {shlex.quote(str(launch_dir))} && {shlex.quote(sys.executable)} -m ailoop.cli "
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
    return read_last_lines(path, lines)


def read_events(path: Path, limit: int = 80) -> str:
    if not path.exists():
        return "<missing>"
    return read_last_lines(path, limit)


def short_status(status: str) -> str:
    return {
        "pause_requested": "pausing",
        "stop_requested": "stopping",
    }.get(status, status)


def short_loop_id(loop_id: str) -> str:
    return loop_id if len(loop_id) <= 12 else loop_id[:12]


def render_progress_text(completed: int, target: int | None, width: int = 4) -> str:
    if target is None or target <= 0:
        return f"∞ {completed}"
    ratio = min(max(completed / target, 0), 1)
    filled = min(width, max(0, round(ratio * width)))
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {completed}/{target}"


def effective_iteration_count(completed: int, current: int, status: str) -> int:
    if status in ACTIVE_STATUSES and current > completed:
        return current
    return completed


def format_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def is_local_today(value: str | None) -> bool:
    if not value:
        return False
    try:
        current_day = datetime.now().astimezone().date()
        return datetime.fromisoformat(value).astimezone().date() == current_day
    except ValueError:
        return False


def format_compact_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M")
    except ValueError:
        return value


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def loop_mode_text(steps: int | None) -> str:
    return "Infinite" if steps is None else "Fixed Count"


def interval_text(pause_seconds: int) -> tuple[str, str]:
    if pause_seconds <= 0:
        return "continuous", "0"
    if pause_seconds % 3600 == 0:
        return "hours", str(max(1, pause_seconds // 3600))
    if pause_seconds % 60 == 0:
        return "minutes", str(max(1, pause_seconds // 60))
    return "minutes", str(max(1, round(pause_seconds / 60)))


def step_status_lines(completed: int, target: int | None, status: str) -> list[str]:
    if completed <= 0 and status in {"idle", "paused", "stopped"}:
        return [
            "[dim]○[/] Build context",
            "[dim]○[/] Analyse code",
            "[dim]○[/] Make changes",
            "[dim]○[/] Validate",
            "[dim]○[/] Commit",
            "[dim]○[/] Push",
        ]
    if target is not None and completed >= target and status == "completed":
        return [
            "[green]●[/] Build context",
            "[green]●[/] Analyse code",
            "[green]●[/] Make changes",
            "[green]●[/] Validate",
            "[green]●[/] Commit",
            "[green]●[/] Push",
        ]
    return [
        "[green]●[/] Build context",
        "[green]●[/] Analyse code",
        "[green]●[/] Make changes",
        "[green]●[/] Validate",
        "[yellow]◐[/] Commit",
        "[dim]○[/] Push",
    ]


def branch_strategy_label(value: str) -> str:
    return {
        "current": "current branch",
        "new": "new branch",
        "per-iteration": "branch per iteration",
    }.get(value, value)


def autonomy_label(value: str) -> str:
    return {
        "level-1": "Level 1 Observe",
        "level-2": "Level 2 Suggest",
        "level-3": "Level 3 Edit",
        "level-4": "Level 4 Edit + Commit",
        "level-5": "Level 5 Edit + Commit + Push",
    }.get(value, value)


def schedule_type_label(value: str, raw_value: str) -> str:
    minute_label = "minute" if raw_value == "1" else "minutes"
    hour_label = "hour" if raw_value == "1" else "hours"
    return {
        "continuous": "continuous",
        "minutes": f"every {raw_value} {minute_label}",
        "hours": f"every {raw_value} {hour_label}",
        "daily": f"daily at {raw_value}",
        "weekly": f"weekly at {raw_value}",
        "cron": f"cron {raw_value}",
    }.get(value, value)


def compact_countdown_text(value: str) -> str:
    if value.startswith("in ") and value.endswith((" minute", " minutes")):
        trimmed = value.removeprefix("in ")
        trimmed = trimmed.removesuffix(" minutes").removesuffix(" minute")
        return f"next {trimmed}m"
    if value.startswith("in ") and value.endswith((" hour", " hours")):
        trimmed = value.removeprefix("in ")
        trimmed = trimmed.removesuffix(" hours").removesuffix(" hour")
        return f"next {trimmed}h"
    if value == "continuous":
        return "next cont"
    return f"next {value}"


def mini_bar(completed: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    ratio = min(max(completed / total, 0), 1)
    filled = min(width, max(0, round(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def format_storage_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(max(value, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def process_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if sys.platform == "darwin" else rss * 1024)


def extract_modified_files(summary: str | None) -> int:
    if not summary:
        return 0
    match = re.search(r"modified\s+(\d+)\s+files?", summary, re.IGNORECASE)
    if not match:
        return 0
    return int(match.group(1))


def extract_commit_signal(summary: str | None) -> int:
    if not summary:
        return 0
    return 1 if re.search(r"\bcommit(?:ted|ting)?\b", summary, re.IGNORECASE) else 0


def extract_token_usage(summary: str | None) -> int:
    if not summary:
        return 0
    patterns = [
        r"token(?:s| usage)?[:= ]+(\d+)",
        r"(\d+)\s+tokens?",
    ]
    for pattern in patterns:
        match = re.search(pattern, summary, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def extract_cost_usage(summary: str | None) -> float:
    if not summary:
        return 0.0
    patterns = [
        r"cost(?: usage)?[:= ]+\$?(\d+(?:\.\d+)?)",
        r"\$(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, summary, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return 0.0


def colorize_log_line(line: str) -> str:
    if not line.strip():
        return line
    line = re.sub(
        r"^(\d{2}:\d{2}:\d{2})",
        r"[cyan]\1[/]",
        line,
        count=1,
    )
    replacements = {
        "[INFO]": "[green][INFO][/green]",
        "[PLAN]": "[blue][PLAN][/blue]",
        "[ANALYZE]": "[blue][ANALYZE][/blue]",
        "[CHANGE]": "[yellow][CHANGE][/yellow]",
        "[VALIDATE]": "[green][VALIDATE][/green]",
        "[COMMIT]": "[magenta][COMMIT][/magenta]",
        "[PUSH]": "[magenta][PUSH][/magenta]",
        "[ERROR]": "[red][ERROR][/red]",
        "[STDERR]": "[red][STDERR][/red]",
        "[STDOUT]": "[green][STDOUT][/green]",
        "[ok]": "[green][ok][/green]",
        "[fail]": "[red][fail][/red]",
    }
    for raw, styled in replacements.items():
        line = line.replace(raw, styled)
    return line


def colorize_log_text(text: str) -> str:
    return "\n".join(colorize_log_line(line) for line in text.splitlines())


class LoopDashboard(App[None]):
    CSS = """
    Screen {
        background: #08111f;
        color: #f8fafc;
    }

    #main {
        height: 1fr;
        padding: 0 1 1 1;
    }

    .compact-layout #sidebar {
        width: 28;
        min-width: 24;
    }

    .compact-layout #details {
        display: none;
    }

    .compact-layout #content {
        padding: 0;
    }

    #summary_bar {
        height: auto;
        padding: 0 2 1 2;
        color: #dbe7f4;
        background: #07101c;
        text-style: bold;
    }

    #sidebar {
        width: 30;
        min-width: 28;
        border: round #1f4f91;
        padding: 1 1 0 1;
        margin-right: 1;
        background: #0a1322;
    }

    #content {
        width: 1fr;
        padding: 0 1 0 0;
    }

    #details {
        width: 38;
        min-width: 30;
        padding: 0;
        margin-left: 1;
        overflow-y: auto;
    }

    #loops {
        height: 1fr;
        margin-top: 1;
    }

    #loop-query {
        margin-top: 1;
        margin-bottom: 1;
        border: round #2b3b52;
        background: #0c1626;
        color: #dbe7f4;
    }

    #sidebar_stats {
        color: #8ea3bf;
        margin-bottom: 1;
    }

    .panel-title {
        text-style: bold;
        color: #4ea3ff;
        margin-bottom: 1;
    }

    .section-title {
        color: #8ea3bf;
        text-style: bold;
        margin: 0 0 1 0;
    }

    .card {
        border: round #1f4f91;
        background: #0a1322;
        padding: 1;
        margin-bottom: 1;
    }

    .card-static {
        border: round #1f4f91;
        background: #0a1322;
        padding: 1;
        margin-bottom: 1;
        height: auto;
    }

    .card-row {
        height: auto;
    }

    #top_row > .card,
    #top_row > .card-static,
    #middle_row > .card {
        width: 1fr;
        margin-right: 1;
    }

    #top_row > .card:last-child,
    #top_row > .card-static:last-child,
    #middle_row > .card:last-child {
        margin-right: 0;
    }

    #loop_summary,
    #workspace_scope,
    #ops_snapshot,
    #iteration_progress,
    #iteration_history,
    #schedule_card,
    #safety_card,
    #metrics_today,
    #notifications_card {
        height: auto;
    }

    .toolbar {
        height: auto;
        margin-bottom: 1;
    }

    .toolbar Button {
        margin-right: 1;
        margin-bottom: 1;
        background: #0e1728;
        color: #dbe7f4;
        border: round #2b3b52;
        text-style: bold;
        padding: 0 1;
    }

    .toolbar Button.active {
        background: #123a82;
        color: #f8fafc;
        text-style: bold;
        border: round #4ea3ff;
    }

    Button:disabled {
        background: #0b1220;
        color: #61758f;
        border: round #223247;
        text-style: dim;
    }

    Input:disabled,
    Select:disabled,
    Checkbox:disabled {
        color: #61758f;
        background: #0b1220;
        border: round #223247;
    }

    .primary-toolbar Button {
        background: #101b2e;
        border: round #355070;
    }

    .action-toolbar Button {
        width: 1fr;
        min-width: 16;
    }

    #start-continue {
        background: #10371f;
        border: round #1f9d55;
    }

    #pause {
        background: #2a2210;
        border: round #d4a017;
    }

    #stop,
    #memory-delete {
        background: #2a1219;
        border: round #7f1d1d;
    }

    #restart,
    #restart-reset {
        background: #1d1631;
        border: round #7c3aed;
    }

    #next-iteration,
    #refresh {
        background: #101b2e;
        border: round #4ea3ff;
    }

    #run-loop,
    #save-config {
        width: 1fr;
    }

    #run-loop {
        background: #10371f;
        border: round #1f9d55;
    }

    .log-toolbar Button {
        background: #0d1829;
        border: round #2d4666;
    }

    .memory-filter-toolbar Button {
        background: #101a2b;
        border: round #2d4666;
    }

    .primary-toolbar #refresh {
        background: #123a82;
        border: round #4ea3ff;
    }

    .primary-toolbar #stop,
    .memory-toolbar #memory-delete {
        background: #2a1219;
        border: round #7f1d1d;
    }

    .memory-toolbar {
        margin-bottom: 0;
    }

    .memory-action-toolbar Button {
        background: #0f1726;
        border: round #36506f;
    }

    .memory-ui-hidden {
        display: none;
    }

    #memory-query {
        margin: 0 0 1 0;
        border: round #2b3b52;
        background: #0c1626;
        color: #dbe7f4;
    }

    #config-prompt {
        height: 10;
        margin-bottom: 1;
        border: round #2b3b52;
        background: #091322;
    }

    #workspace-include,
    #workspace-exclude {
        height: 5;
        margin-bottom: 1;
        border: round #2b3b52;
        background: #091322;
    }

    .form-row {
        height: auto;
        margin-bottom: 1;
    }

    .field-group {
        width: 1fr;
        margin-right: 1;
    }

    .field-group:last-child {
        margin-right: 0;
    }

    .field-group Input,
    .field-group Select,
    .field-group Checkbox {
        width: 1fr;
    }

    .right-card {
        border: round #1f4f91;
        background: #0a1322;
        padding: 1;
        margin-bottom: 1;
        height: auto;
    }

    .right-card .field-group,
    .right-card .compact-field {
        margin-right: 1;
    }

    .right-card Checkbox {
        margin-bottom: 1;
    }

    .mini-note {
        color: #8ea3bf;
        margin-top: 1;
    }

    #workspace-root-status.root-valid {
        color: #6ddf9d;
    }

    #workspace-root-status.root-invalid {
        color: #ffb86c;
    }

    .detail-preview-hidden {
        display: none;
    }

    .compact-field {
        width: 12;
        margin-right: 1;
    }

    .schedule-value-field {
        width: 1fr;
    }

    .compact-field:last-child {
        margin-right: 0;
    }

    #summary_bar,
    #help_bar,
    #log_meta {
        link-style: bold;
    }

    #log_view {
        border: round #2b3b52;
        height: 1fr;
        padding: 1 1 0 1;
        background: #091322;
    }

    #log_meta {
        color: #8ea3bf;
        margin-bottom: 1;
    }

    #log_card {
        height: 1fr;
    }

    #log_view {
        min-height: 16;
    }

    #help_bar {
        height: auto;
        color: #8ea3bf;
        padding: 0 2 1 2;
        background: #07101c;
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
        ("5", "set_log_metrics", "Metrics"),
        ("6", "set_log_history", "History"),
        ("7", "set_log_memory", "Memory"),
        ("f", "set_log_memory_favorites", "Favorites"),
        ("h", "set_log_memory_history", "History"),
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
        ("ctrl+j", "loop_next", "Next Loop"),
        ("ctrl+k", "loop_prev", "Prev Loop"),
        ("shift+n", "next_iteration", "Next Iter"),
        ("i", "follow_up_focus", "Focus Follow-up"),
        ("ctrl+enter", "queue_follow_up", "Queue Follow-up"),
    ]

    selected_loop_id: reactive[str | None] = reactive(None)
    filter_mode: reactive[FilterMode] = reactive("running")
    loop_query: reactive[str] = reactive("")
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
        self.app_config = app_config
        self.service = LoopService(Path(app_config.paths.state_dir), emit_output=False)
        self.memory = MemoryStore(Path(app_config.paths.state_dir))
        self._config_bound_loop_id: str | None = None
        self._draft_loop_selected = False
        try:
            self.launch_cwd = Path(os.getcwd())
        except FileNotFoundError:
            self.launch_cwd = None
            self.memory_all_folders = True
        self.current_branch = self._detect_branch()
        self.initial_loop_id = loop_id
        if loop_id is not None:
            self.filter_mode = "all"

    def _memory_folder(self) -> Path | None:
        if self.memory_all_folders:
            return None
        return self._active_workspace_root() or self.launch_cwd

    def _can_toggle_memory_scope(self) -> bool:
        return self.launch_cwd is not None

    def _text_input_has_focus(self) -> bool:
        try:
            focused = self.focused
        except Exception:
            return False
        return isinstance(focused, Input | TextArea)

    def _active_workspace_root(self, state: object | None = None) -> Path | None:
        if self.is_mounted:
            try:
                root_text = self.query_one("#workspace-root", Input).value.strip()
            except Exception:
                root_text = ""
            if root_text:
                try:
                    return Path(root_text).expanduser().resolve()
                except FileNotFoundError:
                    return self.launch_cwd
        loop_state = state or self._selected_state()
        if loop_state is not None and getattr(loop_state.run_config, "workspace_root", None):  # type: ignore[attr-defined]
            return Path(loop_state.run_config.workspace_root)  # type: ignore[attr-defined]
        return self.launch_cwd

    def _follow_up_text(self) -> str:
        return self.query_one("#follow-up-prompt", TextArea).text.strip()

    def _detect_branch_for(self, path: Path | None) -> str:
        if path is None:
            return "-"
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            return "-"
        return result.stdout.strip() or "-"

    def _refresh_workspace_branch(self, state: object | None = None) -> None:
        self.current_branch = self._detect_branch_for(self._active_workspace_root(state))
        try:
            self.query_one("#workspace-current-branch", Static).update(self.current_branch)
        except Exception:
            pass

    def _detect_branch(self) -> str:
        if self.launch_cwd is None:
            return "-"
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.launch_cwd,
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            return "-"
        return result.stdout.strip() or "-"

    def _default_run_config(self) -> tuple[str, object]:
        runner_name = self.app_config.default_runner
        runner = self.app_config.runners[runner_name]
        return runner_name, runner

    def _default_prompt_text(self) -> str:
        return (
            "Review the workspace, build context, make safe improvements, validate changes, "
            "and leave a concise summary."
        )

    def _config_form_defaults(self) -> dict[str, object]:
        runner_name, _runner = self._default_run_config()
        pause_seconds = self.app_config.loop.pause_seconds
        interval_kind, interval_value = interval_text(pause_seconds)
        return {
            "prompt": self._default_prompt_text(),
            "mode": "fixed" if self.app_config.loop.steps is not None else "infinite",
            "iterations": str(self.app_config.loop.steps or 5),
            "interval": interval_kind,
            "interval_value": interval_value,
            "quiet_hours": False,
            "quiet_start": "22:00",
            "quiet_end": "07:00",
            "jitter": False,
            "jitter_value": "0-5",
            "runner": runner_name,
            "schedule_type": "continuous",
            "schedule_every": interval_value,
            "schedule_start": "Now",
            "schedule_timezone": "local",
            "autonomy": "level-3",
            "branch_strategy": "current",
            "ask_before_commit": True,
            "ask_before_push": True,
            "auto_commit": False,
            "auto_push": False,
            "create_backup_branch": True,
            "auto_stop_on_limit": True,
            "max_runtime": "4h",
            "max_files_changed": "100",
            "max_commits": "10",
            "max_token_usage": "not tracked",
            "max_cost": "not tracked",
            "notify_start": True,
            "notify_success": True,
            "notify_failure": True,
            "notify_limit": True,
            "notify_complete": True,
            "notify_terminal": True,
            "notify_slack": False,
            "notify_email": False,
        }

    def _recent_workspace_options(self, default_root: str) -> list[tuple[str, str]]:
        roots = [default_root, *self.service.workspace_history.recent_workspace_roots()]
        unique_roots = list(dict.fromkeys(roots))
        return [(root, root) for root in unique_roots]

    def _workspace_form_defaults(self) -> dict[str, str]:
        fallback_root = str(self.launch_cwd or Path.home())
        recent_roots = self.service.workspace_history.recent_workspace_roots(limit=1)
        return {
            "root": recent_roots[0] if recent_roots else fallback_root,
            "include": "src/**\ntests/**\ndocs/**",
            "exclude": ".git/**\n.venv/**\n.ailoop/**\nnode_modules/**",
        }

    def _dashboard_form_values(self) -> dict[str, object]:
        return {
            "prompt": self._textarea_value("#config-prompt", self._default_prompt_text()),
            "mode": self._config_mode_value(),
            "iterations": self._input_value("#config-iterations", "5"),
            "interval": self._config_interval_value(),
            "interval_value": self._input_value("#config-interval-value", "0"),
            "quiet_hours": self._checkbox_value("#config-quiet-hours", False),
            "quiet_start": self._input_value("#config-quiet-start", "22:00"),
            "quiet_end": self._input_value("#config-quiet-end", "07:00"),
            "jitter": self._checkbox_value("#config-jitter", False),
            "jitter_value": self._input_value("#config-jitter-value", "0-5"),
            "schedule_type": self._select_value("#schedule-type", "continuous"),
            "schedule_every": self._input_value("#schedule-every", "0"),
            "schedule_start": self._input_value("#schedule-start-time", "Now"),
            "schedule_timezone": self._select_value("#schedule-timezone", "local"),
            "autonomy": self._select_value("#safety-autonomy", "level-3"),
            "branch_strategy": self._select_value("#workspace-branch-strategy", "current"),
            "ask_before_commit": self._checkbox_value("#safety-ask-before-commit", True),
            "ask_before_push": self._checkbox_value("#safety-ask-before-push", True),
            "auto_commit": self._checkbox_value("#safety-auto-commit", False),
            "auto_push": self._checkbox_value("#safety-auto-push", False),
            "create_backup_branch": self._checkbox_value("#safety-create-backup-branch", True),
            "auto_stop_on_limit": self._checkbox_value("#safety-auto-stop-on-limit", True),
            "max_runtime": self._input_value("#safety-max-runtime", "4h"),
            "max_files_changed": self._input_value("#safety-max-files-changed", "100"),
            "max_commits": self._input_value("#safety-max-commits", "10"),
            "max_token_usage": self._input_value("#safety-max-token-usage", "not tracked"),
            "max_cost": self._input_value("#safety-max-cost", "not tracked"),
            "notify_start": self._checkbox_value("#notify-start", True),
            "notify_success": self._checkbox_value("#notify-success", True),
            "notify_failure": self._checkbox_value("#notify-failure", True),
            "notify_limit": self._checkbox_value("#notify-limit", True),
            "notify_complete": self._checkbox_value("#notify-complete", True),
            "notify_terminal": self._checkbox_value("#notify-terminal", True),
            "notify_slack": self._checkbox_value("#notify-slack", False),
            "notify_email": self._checkbox_value("#notify-email", False),
        }

    def _workspace_form_values(self) -> dict[str, str]:
        defaults = self._workspace_form_defaults()
        return {
            "root": self._input_value("#workspace-root", defaults["root"]),
            "include": self._textarea_value("#workspace-include", defaults["include"]),
            "exclude": self._textarea_value("#workspace-exclude", defaults["exclude"]),
        }

    def compose(self) -> ComposeResult:
        defaults = self._config_form_defaults()
        workspace_defaults = self._workspace_form_defaults()
        recent_workspace_options = self._recent_workspace_options(workspace_defaults["root"])
        yield Header(show_clock=True)
        yield Static("loading...", id="summary_bar")
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("LOOPS", classes="panel-title")
                with Horizontal(classes="toolbar primary-toolbar"):
                    yield Button("+ New Loop", id="new-loop")
                with Horizontal(classes="toolbar primary-toolbar"):
                    yield Button("g running", id="filter-running")
                    yield Button("a active", id="filter-active")
                    yield Button("l all", id="filter-all")
                yield Input(placeholder="Search loops...", id="loop-query")
                yield Static(id="sidebar_stats")
                yield DataTable(id="loops", zebra_stripes=True)
                yield Static(id="system_stats", classes="card-static")
            with Vertical(id="content"):
                with Horizontal(id="top_row", classes="card-row"):
                    yield Static(id="loop_summary", classes="card-static")
                    with Vertical(id="actions_card", classes="card"):
                        yield Static("ACTIONS", classes="panel-title")
                        with Horizontal(classes="toolbar action-toolbar"):
                            yield Button("▶ Start", id="start-continue")
                            yield Button("⏸ Pause", id="pause")
                            yield Button("⏹ Stop", id="stop")
                        with Horizontal(classes="toolbar action-toolbar"):
                            yield Button("↻ Restart", id="restart")
                            yield Button("↺ Reset Counter", id="restart-reset")
                            yield Button("≫ Next Iteration", id="next-iteration")
                        with Horizontal(classes="toolbar action-toolbar"):
                            yield Button("⟳ Refresh", id="refresh")
                        yield Static(id="actions-status", classes="mini-note")
                        yield Static("Follow-up for next iteration", classes="section-title")
                        yield TextArea("", id="follow-up-prompt")
                        with Horizontal(classes="toolbar action-toolbar"):
                            yield Button("Queue & Run Follow-up", id="queue-follow-up")
                            yield Button("Clear Queued", id="clear-follow-up")
                with Horizontal(id="middle_row", classes="card-row"):
                    with Vertical(id="config_card", classes="card"):
                        yield Static("AI LOOP CONFIG", classes="panel-title")
                        yield TextArea(defaults["prompt"], id="config-prompt")
                        with Horizontal(classes="form-row"):
                            with Vertical(classes="field-group"):
                                yield Static("Loop mode", classes="section-title")
                                yield Select(
                                    [
                                        ("Fixed Count", "fixed"),
                                        ("Infinite", "infinite"),
                                        ("Scheduled", "scheduled"),
                                    ],
                                    value=str(defaults["mode"]),
                                    id="config-mode",
                                )
                            with Vertical(classes="field-group"):
                                yield Static("Iterations", classes="section-title")
                                yield Input(str(defaults["iterations"]), id="config-iterations")
                        with Horizontal(classes="form-row"):
                            with Vertical(classes="field-group"):
                                yield Static("Interval", classes="section-title")
                                yield Select(
                                    [
                                        ("Continuous", "continuous"),
                                        ("Every X minutes", "minutes"),
                                        ("Every X hours", "hours"),
                                        ("Daily", "daily"),
                                        ("Weekly", "weekly"),
                                        ("Cron", "cron"),
                                    ],
                                    value=str(defaults["interval"]),
                                    id="config-interval",
                                )
                            with Vertical(classes="field-group"):
                                yield Static("Value", classes="section-title")
                                yield Input(
                                    str(defaults["interval_value"]),
                                    id="config-interval-value",
                                )
                        with Horizontal(classes="form-row"):
                            with Vertical(classes="field-group"):
                                yield Checkbox(
                                    "Quiet hours",
                                    value=bool(defaults["quiet_hours"]),
                                    id="config-quiet-hours",
                                )
                            with Vertical(classes="compact-field"):
                                yield Input(
                                    str(defaults["quiet_start"]),
                                    placeholder="22:00",
                                    id="config-quiet-start",
                                )
                            with Vertical(classes="compact-field"):
                                yield Input(
                                    str(defaults["quiet_end"]),
                                    placeholder="07:00",
                                    id="config-quiet-end",
                                )
                            with Vertical(classes="field-group"):
                                yield Checkbox(
                                    "Jitter",
                                    value=bool(defaults["jitter"]),
                                    id="config-jitter",
                                )
                            with Vertical(classes="compact-field"):
                                yield Input(
                                    str(defaults["jitter_value"]),
                                    placeholder="0-5",
                                    id="config-jitter-value",
                                )
                        with Horizontal(classes="toolbar action-toolbar"):
                            yield Button("Save Config", id="save-config")
                            yield Button("Run Loop", id="run-loop")
                        yield Static(id="config-status", classes="mini-note")
                    with Vertical(id="workspace_card", classes="card"):
                        yield Static("WORKSPACE & SCOPE", classes="panel-title")
                        yield Static("Root directory", classes="section-title")
                        yield Input(workspace_defaults["root"], id="workspace-root")
                        yield Static(
                            "Enter an existing directory",
                            id="workspace-root-status",
                            classes="mini-note",
                        )
                        yield Static("Recent workspace", classes="section-title")
                        yield Select(
                            recent_workspace_options,
                            value=workspace_defaults["root"],
                            id="workspace-recent",
                        )
                        yield Static(
                            "Root is enforced as runner cwd. Other scope/safety settings "
                            "are saved planning metadata.",
                            classes="mini-note",
                        )
                        with Horizontal(classes="form-row"):
                            with Vertical(classes="field-group"):
                                yield Static("Current branch", classes="section-title")
                                yield Static(self.current_branch, id="workspace-current-branch")
                            with Vertical(classes="field-group"):
                                yield Static("Branch strategy", classes="section-title")
                                yield Select(
                                    [
                                        ("Current branch", "current"),
                                        ("New branch", "new"),
                                        ("Branch per iteration", "per-iteration"),
                                    ],
                                    value=str(defaults["branch_strategy"]),
                                    id="workspace-branch-strategy",
                                )
                        yield Static("Included paths", classes="section-title")
                        yield TextArea(workspace_defaults["include"], id="workspace-include")
                        yield Static("Excluded paths", classes="section-title")
                        yield TextArea(workspace_defaults["exclude"], id="workspace-exclude")
                        yield Static(id="workspace_scope", classes="mini-note")
                with Vertical(id="log_card", classes="card"):
                    yield Static("LOGS & OBSERVABILITY", classes="panel-title")
                    with Horizontal(classes="toolbar log-toolbar"):
                        yield Button("1 stdout", id="log-stdout")
                        yield Button("2 stderr", id="log-stderr")
                        yield Button("3 prompt", id="log-prompt")
                        yield Button("4 events", id="log-events")
                        yield Button("5 metrics", id="log-metrics")
                        yield Button("6 history", id="log-history")
                    with Horizontal(
                        id="memory-filter-toolbar",
                        classes="toolbar memory-filter-toolbar",
                    ):
                        yield Button("7 memory", id="log-memory")
                        yield Button("f favorites", id="log-memory-favorites")
                        yield Button("h mem-history", id="log-memory-history")
                        yield Button("m presets", id="log-memory-presets")
                        yield Button("0 archived", id="log-memory-archived")
                    with Horizontal(
                        id="memory-action-toolbar",
                        classes="toolbar memory-action-toolbar",
                    ):
                        yield Button("b prev label", id="memory-label-prev")
                        yield Button("n next label", id="memory-label-next")
                        yield Button("c clear label", id="memory-label-clear")
                        yield Button("o folders", id="memory-scope-toggle")
                        yield Button("8 replay", id="memory-replay")
                        yield Button("9 favorite", id="memory-favorite")
                        yield Button("v restore", id="memory-restore")
                        yield Button("z archive", id="memory-archive")
                        yield Button("x delete", id="memory-delete")
                    yield Input(placeholder=self._memory_query_placeholder(), id="memory-query")
                    yield Static(id="log_meta")
                    yield Static(id="log_view")
            with Vertical(id="details"):
                yield Static(id="iteration_progress", classes="card-static")
                yield Static(id="iteration_history", classes="card-static")
                yield Static(id="ops_snapshot", classes="card-static")
                yield Static(id="metrics_today", classes="card-static")
                with Vertical(id="schedule_card", classes="right-card"):
                    yield Static("SCHEDULING", classes="panel-title")
                    yield Static(id="schedule-preview", classes="mini-note detail-preview-hidden")
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Static("Schedule type", classes="section-title")
                            yield Select(
                                [
                                    ("Continuous", "continuous"),
                                    ("Every X minutes", "minutes"),
                                    ("Every X hours", "hours"),
                                    ("Daily", "daily"),
                                    ("Weekly", "weekly"),
                                    ("Cron", "cron"),
                                ],
                                value=str(defaults["schedule_type"]),
                                id="schedule-type",
                            )
                        with Vertical(classes="compact-field schedule-value-field"):
                            yield Static("Every", classes="section-title")
                            yield Input(
                                str(defaults["schedule_every"]),
                                id="schedule-every",
                            )
                        with Vertical(classes="compact-field"):
                            yield Static("Timezone", classes="section-title")
                            yield Select(
                                [
                                    ("Local", "local"),
                                    ("UTC", "utc"),
                                ],
                                value=str(defaults["schedule_timezone"]),
                                id="schedule-timezone",
                            )
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="compact-field"):
                            yield Static("Start time", classes="section-title")
                            yield Input(
                                str(defaults["schedule_start"]),
                                id="schedule-start-time",
                            )
                with Vertical(id="safety_card", classes="right-card"):
                    yield Static("BEHAVIOUR & SAFETY", classes="panel-title")
                    yield Static(id="safety-preview", classes="mini-note detail-preview-hidden")
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Static("Autonomy level", classes="section-title")
                            yield Select(
                                [
                                    ("Level 1 Observe", "level-1"),
                                    ("Level 2 Suggest", "level-2"),
                                    ("Level 3 Edit", "level-3"),
                                    ("Level 4 Edit + Commit", "level-4"),
                                    ("Level 5 Edit + Commit + Push", "level-5"),
                                ],
                                value=str(defaults["autonomy"]),
                                id="safety-autonomy",
                            )
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Checkbox(
                                "Ask before commit",
                                value=bool(defaults["ask_before_commit"]),
                                id="safety-ask-before-commit",
                            )
                            yield Checkbox(
                                "Ask before push",
                                value=bool(defaults["ask_before_push"]),
                                id="safety-ask-before-push",
                            )
                            yield Checkbox(
                                "Auto commit",
                                value=bool(defaults["auto_commit"]),
                                id="safety-auto-commit",
                            )
                            yield Checkbox(
                                "Auto push",
                                value=bool(defaults["auto_push"]),
                                id="safety-auto-push",
                            )
                        with Vertical(classes="field-group"):
                            yield Checkbox(
                                "Create backup branch",
                                value=bool(defaults["create_backup_branch"]),
                                id="safety-create-backup-branch",
                            )
                            yield Checkbox(
                                "Auto-stop on limit",
                                value=bool(defaults["auto_stop_on_limit"]),
                                id="safety-auto-stop-on-limit",
                            )
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Static("Max runtime", classes="section-title")
                            yield Input(str(defaults["max_runtime"]), id="safety-max-runtime")
                        with Vertical(classes="field-group"):
                            yield Static("Max files changed", classes="section-title")
                            yield Input(
                                str(defaults["max_files_changed"]),
                                id="safety-max-files-changed",
                            )
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Static("Max commits", classes="section-title")
                            yield Input(str(defaults["max_commits"]), id="safety-max-commits")
                        with Vertical(classes="field-group"):
                            yield Static("Max token usage", classes="section-title")
                            yield Input(
                                str(defaults["max_token_usage"]),
                                id="safety-max-token-usage",
                            )
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Static("Max cost", classes="section-title")
                            yield Input(str(defaults["max_cost"]), id="safety-max-cost")
                with Vertical(id="notifications_card", classes="right-card"):
                    yield Static("NOTIFICATIONS", classes="panel-title")
                    yield Static(
                        id="notifications-preview",
                        classes="mini-note detail-preview-hidden",
                    )
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Checkbox(
                                "On iteration start",
                                value=bool(defaults["notify_start"]),
                                id="notify-start",
                            )
                            yield Checkbox(
                                "On iteration success",
                                value=bool(defaults["notify_success"]),
                                id="notify-success",
                            )
                            yield Checkbox(
                                "On iteration failure",
                                value=bool(defaults["notify_failure"]),
                                id="notify-failure",
                            )
                        with Vertical(classes="field-group"):
                            yield Checkbox(
                                "On limit reached",
                                value=bool(defaults["notify_limit"]),
                                id="notify-limit",
                            )
                            yield Checkbox(
                                "On loop complete",
                                value=bool(defaults["notify_complete"]),
                                id="notify-complete",
                            )
                    with Horizontal(classes="form-row"):
                        with Vertical(classes="field-group"):
                            yield Static("Channels", classes="section-title")
                            yield Checkbox(
                                "terminal",
                                value=bool(defaults["notify_terminal"]),
                                id="notify-terminal",
                            )
                            yield Checkbox(
                                "Slack",
                                value=bool(defaults["notify_slack"]),
                                id="notify-slack",
                            )
                            yield Checkbox(
                                "email",
                                value=bool(defaults["notify_email"]),
                                id="notify-email",
                            )
        yield Static("loading...", id="help_bar")

    def on_mount(self) -> None:
        self._sync_layout_mode()
        table = self.query_one(DataTable)
        table.add_columns("Loop", "Status", "Iter", "Mode", "Agent")
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()
        self._update_workspace_root_status()

    def on_resize(self, event: events.Resize) -> None:
        self._sync_layout_mode(event.size.width)

    def _sync_layout_mode(self, width: int | None = None) -> None:
        actual_width = self.size.width if width is None else width
        self.set_class(actual_width <= COMPACT_LAYOUT_WIDTH, "compact-layout")

    def _config_mode_value(self) -> str:
        try:
            return str(self.query_one("#config-mode", Select).value or "fixed")
        except Exception:
            return "fixed"

    def _config_interval_value(self) -> str:
        try:
            return str(self.query_one("#config-interval", Select).value or "continuous")
        except Exception:
            return "continuous"

    def _config_interval_seconds(self) -> int:
        interval_kind = self._config_interval_value()
        raw_value = self.query_one("#config-interval-value", Input).value.strip() or "0"
        if interval_kind == "continuous":
            return 0
        try:
            value = max(0, int(raw_value))
        except ValueError:
            return 0
        if interval_kind == "hours":
            return value * 3600
        if interval_kind == "minutes":
            return value * 60
        return 0

    def _schedule_interval_seconds(self) -> int:
        interval_kind = self._select_value("#schedule-type", "continuous")
        raw_value = self._input_value("#schedule-every", "0")
        if interval_kind == "continuous":
            return 0
        try:
            value = max(0, int(raw_value))
        except ValueError:
            return 0
        if interval_kind == "hours":
            return value * 3600
        if interval_kind == "minutes":
            return value * 60
        return 0

    def _form_supports_run(self) -> bool:
        mode = self._config_mode_value()
        if mode == "scheduled":
            return True
        return self._config_interval_value() in {"continuous", "minutes", "hours"}

    def _validate_workspace_root(self) -> bool:
        """Keep invalid workspace paths out of persisted state and runner spawns."""
        try:
            workspace_root = self.query_one("#workspace-root", Input)
        except ScreenStackError:
            # Programmatic action tests and non-UI callers have no form to validate.
            return True
        entered_root = workspace_root.value
        try:
            normalized_root = self.service._normalize_workspace_root(entered_root)
        except FileNotFoundError:
            self._update_workspace_root_status(entered_root)
            workspace_root.focus()
            self.notify(
                f"workspace root must be an existing directory: {entered_root}",
                severity="warning",
            )
            return False
        if normalized_root is not None:
            workspace_root.value = normalized_root
        self._update_workspace_root_status(workspace_root.value)
        return True

    def _update_workspace_root_status(self, root: str | None = None) -> None:
        try:
            status = self.query_one("#workspace-root-status", Static)
            entered_root = (
                root if root is not None else self.query_one("#workspace-root", Input).value
            )
        except ScreenStackError:
            return
        if not entered_root.strip():
            status.update("Enter an existing directory")
            status.set_class(False, "root-valid")
            status.set_class(False, "root-invalid")
            return
        try:
            normalized_root = self.service._normalize_workspace_root(entered_root)
        except FileNotFoundError:
            status.update("⚠ Workspace must be an existing directory")
            status.set_class(False, "root-valid")
            status.set_class(True, "root-invalid")
            return
        status.set_class(True, "root-valid")
        status.set_class(False, "root-invalid")
        if normalized_root and normalized_root != entered_root:
            status.update(f"✓ Valid workspace — runner cwd: {normalized_root}")
            return
        status.update("✓ Valid workspace — runner cwd")

    def _state_mode_and_schedule(self, state: object | None) -> tuple[str, str, str]:
        if state is not None:
            loop_state = state
            dashboard_config = getattr(loop_state, "dashboard_config", {}) or {}
            if dashboard_config:
                mode = str(dashboard_config.get("mode", "fixed"))
                schedule_type = str(
                    dashboard_config.get(
                        "schedule_type",
                        dashboard_config.get("interval", self._config_interval_value()),
                    )
                )
                schedule_every = str(
                    dashboard_config.get(
                        "schedule_every",
                        dashboard_config.get(
                            "interval_value",
                            self._input_value("#schedule-every", "0"),
                        ),
                    )
                )
                return mode, schedule_type, schedule_every
            interval_kind, interval_value = interval_text(loop_state.run_config.pause_seconds)  # type: ignore[attr-defined]
            mode = "fixed" if loop_state.run_config.steps is not None else "infinite"  # type: ignore[attr-defined]
            return mode, interval_kind, interval_value
        return (
            self._config_mode_value(),
            self._select_value("#schedule-type", self._config_interval_value()),
            self._input_value("#schedule-every", self._input_value("#config-interval-value", "0")),
        )

    def _schedule_countdown_from(self, interval_kind: str, raw_value: str, start_time: str) -> str:
        minute_label = "minute" if raw_value == "1" else "minutes"
        hour_label = "hour" if raw_value == "1" else "hours"
        countdown = "manual"
        if interval_kind == "minutes":
            countdown = f"in {raw_value} {minute_label}"
        elif interval_kind == "hours":
            countdown = f"in {raw_value} {hour_label}"
        elif interval_kind == "daily":
            countdown = f"next daily window from {start_time}"
        elif interval_kind == "weekly":
            countdown = f"next weekly window from {start_time}"
        elif interval_kind == "cron":
            countdown = "cron-driven"
        elif interval_kind == "continuous":
            countdown = "continuous"
        return countdown

    def _selected_schedule_countdown_text(self, state: object | None) -> str:
        mode, schedule_type, schedule_every = self._state_mode_and_schedule(state)
        dashboard_config = getattr(state, "dashboard_config", {}) if state is not None else {}
        start_time = str(
            dashboard_config.get(
                "schedule_start",
                self._input_value("#schedule-start-time", "Now"),
            )
        )
        if mode == "scheduled":
            return self._schedule_countdown_from(schedule_type, schedule_every, start_time)
        return self._schedule_countdown_text()

    def _sync_form_controls(self) -> None:
        mode = self._config_mode_value()
        interval = self._config_interval_value()
        schedule_type = self._select_value("#schedule-type", interval)
        quiet_hours = self._checkbox_value("#config-quiet-hours", False)
        jitter_enabled = self._checkbox_value("#config-jitter", False)

        def set_disabled(selector: str, disabled: bool, widget_type: object) -> None:
            try:
                self.query_one(selector, widget_type).disabled = disabled  # type: ignore[attr-defined]
            except Exception:
                return

        set_disabled("#config-iterations", mode != "fixed", Input)
        set_disabled("#config-interval-value", interval == "continuous", Input)
        set_disabled("#config-quiet-start", not quiet_hours, Input)
        set_disabled("#config-quiet-end", not quiet_hours, Input)
        set_disabled("#config-jitter-value", not jitter_enabled, Input)
        set_disabled("#schedule-every", schedule_type == "continuous", Input)
        set_disabled("#schedule-start-time", schedule_type == "continuous", Input)
        set_disabled("#schedule-timezone", schedule_type == "continuous", Select)

    def _sync_schedule_with_config(self) -> None:
        mode = self._config_mode_value()
        interval = self._config_interval_value()
        interval_value = self._input_value("#config-interval-value", "0")
        if mode == "scheduled":
            return
        try:
            self.query_one("#schedule-type", Select).value = interval
            self.query_one("#schedule-every", Input).value = interval_value
        except Exception:
            return

    def _select_value(self, selector: str, default: str) -> str:
        try:
            return str(self.query_one(selector, Select).value or default)
        except Exception:
            return default

    def _input_value(self, selector: str, default: str) -> str:
        try:
            value = self.query_one(selector, Input).value.strip()
        except Exception:
            return default
        return value or default

    def _textarea_value(self, selector: str, default: str) -> str:
        try:
            value = self.query_one(selector, TextArea).text.strip()
        except Exception:
            return default
        return value or default

    def _checkbox_value(self, selector: str, default: bool) -> bool:
        try:
            return bool(self.query_one(selector, Checkbox).value)
        except Exception:
            return default

    def _sync_config_form_from_state(self, state: object | None) -> None:
        def set_checkbox(selector: str, value: object) -> None:
            self.query_one(selector, Checkbox).value = bool(value)

        def set_input(selector: str, value: object) -> None:
            self.query_one(selector, Input).value = str(value)

        defaults = self._config_form_defaults()
        workspace_defaults = self._workspace_form_defaults()
        if state is None:
            bound_id = "__defaults__"
            if self._config_bound_loop_id == bound_id:
                return
            values = defaults
            workspace_values = workspace_defaults
        else:
            loop_state = state
            bound_id = loop_state.loop_id  # type: ignore[attr-defined]
            if self._config_bound_loop_id == bound_id:
                return
            interval_kind, interval_value = interval_text(loop_state.run_config.pause_seconds)  # type: ignore[attr-defined]
            values = {
                **defaults,
                **getattr(loop_state, "dashboard_config", {}),
                "prompt": loop_state.run_config.prompt,  # type: ignore[attr-defined]
                "mode": getattr(loop_state, "dashboard_config", {}).get(
                    "mode",
                    "fixed" if loop_state.run_config.steps is not None else "infinite",  # type: ignore[attr-defined]
                ),
                "iterations": str(loop_state.run_config.steps or 5),  # type: ignore[attr-defined]
                "interval": interval_kind,
                "interval_value": interval_value,
            }
            workspace_values = {
                **workspace_defaults,
                **getattr(loop_state, "workspace_config", {}),
            }
            if getattr(loop_state.run_config, "workspace_root", None):  # type: ignore[attr-defined]
                workspace_values["root"] = loop_state.run_config.workspace_root  # type: ignore[attr-defined]
        self.query_one("#config-prompt", TextArea).text = str(values["prompt"])
        self.query_one("#config-mode", Select).value = str(values["mode"])
        set_input("#config-iterations", values["iterations"])
        self.query_one("#config-interval", Select).value = str(values["interval"])
        set_input("#config-interval-value", values["interval_value"])
        set_checkbox("#config-quiet-hours", values["quiet_hours"])
        set_input("#config-quiet-start", values["quiet_start"])
        set_input("#config-quiet-end", values["quiet_end"])
        set_checkbox("#config-jitter", values["jitter"])
        set_input("#config-jitter-value", values["jitter_value"])
        set_input("#workspace-root", workspace_values["root"])
        workspace_recent = self.query_one("#workspace-recent", Select)
        workspace_recent.set_options(self._recent_workspace_options(str(workspace_values["root"])))
        workspace_recent.value = str(workspace_values["root"])
        self.query_one("#workspace-include", TextArea).text = str(workspace_values["include"])
        self.query_one("#workspace-exclude", TextArea).text = str(workspace_values["exclude"])
        follow_up_text = (
            (getattr(loop_state, "queued_follow_up", None) or "")
            if state is not None
            else ""
        )
        self.query_one("#follow-up-prompt", TextArea).text = follow_up_text
        try:
            self.query_one("#schedule-type", Select).value = str(values["schedule_type"])
            set_input("#schedule-every", values["schedule_every"])
        except Exception:
            pass
        set_input("#schedule-start-time", values["schedule_start"])
        self.query_one("#schedule-timezone", Select).value = str(values["schedule_timezone"])
        self.query_one("#safety-autonomy", Select).value = str(values["autonomy"])
        self.query_one("#workspace-branch-strategy", Select).value = str(values["branch_strategy"])
        set_checkbox("#safety-ask-before-commit", values["ask_before_commit"])
        set_checkbox("#safety-ask-before-push", values["ask_before_push"])
        set_checkbox("#safety-auto-commit", values["auto_commit"])
        set_checkbox("#safety-auto-push", values["auto_push"])
        set_checkbox("#safety-create-backup-branch", values["create_backup_branch"])
        set_checkbox("#safety-auto-stop-on-limit", values["auto_stop_on_limit"])
        set_input("#safety-max-runtime", values["max_runtime"])
        set_input("#safety-max-files-changed", values["max_files_changed"])
        set_input("#safety-max-commits", values["max_commits"])
        set_input("#safety-max-token-usage", values["max_token_usage"])
        set_input("#safety-max-cost", values["max_cost"])
        set_checkbox("#notify-start", values["notify_start"])
        set_checkbox("#notify-success", values["notify_success"])
        set_checkbox("#notify-failure", values["notify_failure"])
        set_checkbox("#notify-limit", values["notify_limit"])
        set_checkbox("#notify-complete", values["notify_complete"])
        set_checkbox("#notify-terminal", values["notify_terminal"])
        set_checkbox("#notify-slack", values["notify_slack"])
        set_checkbox("#notify-email", values["notify_email"])
        self._config_bound_loop_id = bound_id

    def _build_run_config_from_form(self, state: object | None = None):
        prompt_widget = self.query_one("#config-prompt", TextArea)
        prompt = prompt_widget.text.strip() or self._default_prompt_text()
        mode = self._config_mode_value()
        try:
            iterations = max(
                1,
                int(self.query_one("#config-iterations", Input).value.strip() or "1"),
            )
        except ValueError:
            iterations = 1
        if state is None:
            runner_name, runner = self._default_run_config()
            agent = self.app_config.default_agent
            continue_on_error = self.app_config.loop.continue_on_error
            retry_count = self.app_config.loop.retry_count
            pre_prompt_enabled = self.app_config.prompt.pre_prompt_enabled
            attach_agent_file = self.app_config.prompt.attach_agent_file
            pre_prompt = self.app_config.prompt.pre_prompt
            agent_file = self.app_config.paths.agent_file
            runner_command = runner.command
            runner_args = list(runner.args)
            runner_env = dict(runner.env)
            task_file = self.app_config.tasks.file
            stop_when_tasks_complete = self.app_config.tasks.stop_when_complete
            max_doing = self.app_config.tasks.max_doing
            workspace_root = self._input_value(
                "#workspace-root",
                str(self.launch_cwd or Path.home()),
            ).strip()
            workspace_history_enabled = True
            workspace_history_limit = 5
            workspace_history_chars = 1200
        else:
            loop_state = state
            runner_name = loop_state.run_config.runner  # type: ignore[attr-defined]
            agent = loop_state.run_config.agent  # type: ignore[attr-defined]
            continue_on_error = loop_state.run_config.continue_on_error  # type: ignore[attr-defined]
            retry_count = loop_state.run_config.retry_count  # type: ignore[attr-defined]
            pre_prompt_enabled = loop_state.run_config.pre_prompt_enabled  # type: ignore[attr-defined]
            attach_agent_file = loop_state.run_config.attach_agent_file  # type: ignore[attr-defined]
            pre_prompt = loop_state.run_config.pre_prompt  # type: ignore[attr-defined]
            agent_file = loop_state.run_config.agent_file  # type: ignore[attr-defined]
            runner_command = loop_state.run_config.runner_command  # type: ignore[attr-defined]
            runner_args = list(loop_state.run_config.runner_args)  # type: ignore[attr-defined]
            runner_env = dict(loop_state.run_config.runner_env)  # type: ignore[attr-defined]
            task_file = loop_state.run_config.task_file  # type: ignore[attr-defined]
            stop_when_tasks_complete = loop_state.run_config.stop_when_tasks_complete  # type: ignore[attr-defined]
            max_doing = loop_state.run_config.max_doing  # type: ignore[attr-defined]
            workspace_root = self._input_value(
                "#workspace-root",
                str(loop_state.run_config.workspace_root or self.launch_cwd or Path.home()),
            ).strip()  # type: ignore[attr-defined]
            workspace_history_enabled = loop_state.run_config.workspace_history_enabled  # type: ignore[attr-defined]
            workspace_history_limit = loop_state.run_config.workspace_history_limit  # type: ignore[attr-defined]
            workspace_history_chars = loop_state.run_config.workspace_history_chars  # type: ignore[attr-defined]
        from .models import LoopRunConfig

        return LoopRunConfig(
            prompt=prompt,
            runner=runner_name,
            agent=agent,
            steps=iterations if mode == "fixed" else None,
            pause_seconds=(
                self._schedule_interval_seconds()
                if mode == "scheduled"
                else self._config_interval_seconds()
            ),
            continue_on_error=continue_on_error,
            retry_count=retry_count,
            pre_prompt_enabled=pre_prompt_enabled,
            attach_agent_file=attach_agent_file,
            pre_prompt=pre_prompt,
            agent_file=agent_file,
            runner_command=runner_command,
            runner_args=runner_args,
            runner_env=runner_env,
            task_file=task_file,
            stop_when_tasks_complete=stop_when_tasks_complete,
            max_doing=max_doing,
            workspace_root=workspace_root or None,
            workspace_history_enabled=workspace_history_enabled,
            workspace_history_limit=workspace_history_limit,
            workspace_history_chars=workspace_history_chars,
        )

    def _status_markup(self, status: str) -> str:
        color = {
            "running": "green",
            "pause_requested": "yellow",
            "paused": "yellow",
            "stop_requested": "red",
            "stopped": "red",
            "failed": "red",
            "completed": "cyan",
            "idle": "blue",
        }.get(status, "white")
        return f"[{color}]{short_status(status)}[/]"

    def _render_summary_bar(self) -> None:
        if not self.is_mounted:
            return
        states = self.service.list_loops()
        total = len(states)
        active = sum(1 for state in states if state.status in ACTIVE_STATUSES)
        running = sum(1 for state in states if state.status in RUNNING_STATUSES)
        paused = sum(1 for state in states if state.status == "paused")
        failed = sum(1 for state in states if state.status == "failed")
        scheduled = sum(
            1 for state in states if self._state_mode_and_schedule(state)[0] == "scheduled"
        )
        selected = self._selected_state()
        summary_text = self._summary_bar_text(
            total,
            active,
            running,
            paused,
            failed,
            scheduled,
            selected,
            width=self.size.width,
        )
        try:
            self.query_one("#summary_bar", Static).update(summary_text)
        except Exception:
            return

    def _render_sidebar_stats(self, states: list[object]) -> None:
        try:
            sidebar_stats = self.query_one("#sidebar_stats", Static)
        except Exception:
            return
        query = self.loop_query or "-"
        running = sum(1 for state in states if state.status in RUNNING_STATUSES)
        active = sum(1 for state in states if state.status in ACTIVE_STATUSES)
        paused = sum(1 for state in states if state.status == "paused")
        failed = sum(1 for state in states if state.status == "failed")
        scheduled = sum(
            1 for state in states if self._state_mode_and_schedule(state)[0] == "scheduled"
        )
        selected = short_loop_id(self.selected_loop_id) if self.selected_loop_id else "none"
        counts_text = (
            f"loops {len(states)} · active {active} · running {running} · paused {paused} · "
            f"scheduled {scheduled} · failed {failed}"
        )
        sidebar_stats.update(
            f"{counts_text}\n"
            f"filter {self.filter_mode} · query {query} · selected {selected}"
        )

    def _render_system_stats(self, states: list[object]) -> None:
        try:
            system_stats = self.query_one("#system_stats", Static)
        except Exception:
            return
        load_1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        rss_bytes = process_rss_bytes()
        disk_root = self.launch_cwd or Path.home()
        free_bytes = shutil.disk_usage(disk_root)[2]
        selected = short_loop_id(self.selected_loop_id) if self.selected_loop_id else "draft"
        system_stats.update(
            "\n".join(
                [
                    "[b][#4ea3ff]SYSTEM[/][/]",
                    f"Load 1m: {load_1:.2f}",
                    f"App RSS: {format_storage_bytes(rss_bytes)}",
                    f"Disk free: {format_storage_bytes(free_bytes)}",
                    f"Target: {selected}",
                ]
            )
        )

    def _summary_bar_text(
        self,
        total: int,
        active: int,
        running: int,
        paused: int,
        failed: int,
        scheduled: int,
        state: object | None,
        width: int | None = None,
    ) -> str:
        actual_width = width or 0
        compact = bool(actual_width and actual_width <= COMPACT_LAYOUT_WIDTH)
        selected_text = self._summary_selected_text(state, width=actual_width)
        if compact:
            counts_text = (
                f"L{total} · A{active} · R{running} · P{paused} · S{scheduled} · F{failed}"
            )
        else:
            counts_text = (
                f"loops {total} · active {active} · running {running} · paused {paused} · "
                f"scheduled {scheduled} · failed {failed}"
            )
        if self.log_kind == "memory":
            if compact:
                return f"{selected_text} · f {self.filter_mode} · {counts_text}"
            return f"{selected_text} · filter {self.filter_mode} · {counts_text}"
        if compact:
            return f"{selected_text} · f {self.filter_mode} · view {self.log_kind} · {counts_text}"
        return f"{selected_text} · filter {self.filter_mode} · view {self.log_kind} · {counts_text}"

    def _summary_selected_text(self, state: object | None, width: int | None = None) -> str:
        actual_width = width or 0
        compact = bool(actual_width and actual_width <= COMPACT_LAYOUT_WIDTH)
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
        loop_state = state
        mode, _schedule_type, _schedule_every = self._state_mode_and_schedule(loop_state)
        schedule_hint = compact_countdown_text(self._selected_schedule_countdown_text(loop_state))
        target = loop_state.run_config.steps  # type: ignore[attr-defined]
        mode_short = {"fixed": "fix", "infinite": "inf", "scheduled": "sched"}.get(mode, mode)
        iteration_text = (
            "iter "
            f"{loop_state.current_iteration or loop_state.completed_iterations}/"  # type: ignore[attr-defined]
            f"{target or '∞'}"
        )
        if compact:
            return (
                f"sel {short_loop_id(loop_state.loop_id)} · "  # type: ignore[attr-defined]
                f"{short_status(loop_state.status)} · "  # type: ignore[attr-defined]
                f"{iteration_text} · {mode_short} · {schedule_hint}"
            )
        return (
            f"selected {short_loop_id(loop_state.loop_id)} · "  # type: ignore[attr-defined]
            f"state {short_status(loop_state.status)} · {iteration_text} · "  # type: ignore[attr-defined]
            f"mode {mode} · {schedule_hint} · branch {self.current_branch} · "
            f"agent {(loop_state.run_config.agent or '-')[:12]}"  # type: ignore[attr-defined]
        )

    def _footer_base_text(self, width: int | None = None) -> str:
        actual_width = self.size.width if width is None else width
        if actual_width and actual_width <= COMPACT_LAYOUT_WIDTH:
            return "↑↓ filt g/a/l · 1-7/f/h/m/0 · r/q"
        return "nav ↑↓/click · filters g/a/l · logs 1-7/f/h/m/0 · r refresh · q quit"

    def _memory_compact_actions(self) -> str:
        parts = ["[ ]", "b/n/c"]
        if self._can_toggle_memory_scope():
            parts.append("o")
        parts.append("/")
        if self._primary_memory_entry() is not None:
            archive_token = "z!" if self.memory_archive_armed else "z"
            delete_token = "x!" if self.memory_delete_armed else "x"
            parts.append(f"8/9/{archive_token}/{delete_token}")
            if self._primary_memory_entry().archived:  # type: ignore[union-attr]
                parts.append("v")
        return " ".join(parts) if parts else "read"

    def _memory_help_text(self, width: int | None = None) -> str:
        actual_width = self.size.width if width is None else width
        compact = bool(actual_width and actual_width <= COMPACT_LAYOUT_WIDTH)
        memory_actions = []
        label_count = len(self._memory_labels())
        if self._primary_memory_entry() is not None:
            memory_actions.extend(
                [
                    "[ prev",
                    "] next",
                    "b label<",
                    "n label>",
                    "c clear label",
                    *(["o scope"] if self._can_toggle_memory_scope() else []),
                    "/ query",
                    "esc clear query",
                    "8 replay",
                    "9 favorite",
                    "z confirm archive" if self.memory_archive_armed else "z archive",
                    "x confirm delete" if self.memory_delete_armed else "x delete",
                ]
            )
            if self._primary_memory_entry().archived:  # type: ignore[union-attr]
                memory_actions.append("v restore")
        if compact:
            action_text = self._memory_compact_actions()
        else:
            action_text = " · ".join(memory_actions) if memory_actions else "read only"
        base = self._footer_base_text(width=actual_width)
        entries = len(self._memory_entries())
        labels = len(self._memory_labels())
        if compact:
            return (
                f"{base} · mem:{self.memory_filter} · {self._memory_scope_text(compact=True)} · "
                f"ent:{entries} · act:{action_text}"
            )
        return (
            f"{base} · memory {self.memory_filter} · label {self.memory_label or '-'} · "
            f"query {self.memory_query or '-'} · "
            f"scope {self._memory_scope_text()} · entries {entries} · "
            f"labels {labels}/{label_count} · "
            f"actions {action_text}"
        )

    def _loop_summary_text(self, state: object | None) -> str:
        if state is None:
            return (
                "[b][#4ea3ff]LOOP SUMMARY[/][/]\n\nNo loop selected.\n"
                "Use the left sidebar to choose a loop or run a new one from the config panel."
            )
        loop_state = state
        target = loop_state.run_config.steps  # type: ignore[attr-defined]
        mode, schedule_type, schedule_every = self._state_mode_and_schedule(loop_state)
        dashboard_config = getattr(loop_state, "dashboard_config", {}) or {}
        workspace_config = getattr(loop_state, "workspace_config", {}) or {}
        progress_count = effective_iteration_count(
            loop_state.completed_iterations,  # type: ignore[attr-defined]
            loop_state.current_iteration,  # type: ignore[attr-defined]
            loop_state.status,  # type: ignore[attr-defined]
        )
        progress = render_progress_text(progress_count, target, width=8)
        interval_label = schedule_type_label(schedule_type, schedule_every)
        autonomy = autonomy_label(
            str(dashboard_config.get("autonomy", self._select_value("#safety-autonomy", "level-3")))
        )
        branch_strategy = branch_strategy_label(
            str(
                dashboard_config.get(
                    "branch_strategy",
                    self._select_value("#workspace-branch-strategy", "current"),
                )
            )
        )
        next_run = self._selected_schedule_countdown_text(loop_state)
        workspace_root = str(
            getattr(loop_state.run_config, "workspace_root", None)
            or workspace_config.get("root", self.launch_cwd or Path.home())
        )
        loop_line = (
            f"Loop: {loop_state.loop_id} · {self._status_markup(loop_state.status)} · {progress}"  # type: ignore[attr-defined]
        )
        mode_label = mode.title() if mode != "fixed" else loop_mode_text(target)
        mode_line = f"Mode: {mode_label} · {interval_label}"
        return "\n".join(
            [
                "[b][#4ea3ff]LOOP SUMMARY[/][/]",
                "",
                loop_line,
                mode_line,
                f"Next: {next_run} · {branch_strategy} · {autonomy}",
                f"Scope: {workspace_root} · branch {self.current_branch}",
                (
                    f"Runner/Agent: {loop_state.run_config.runner} · "  # type: ignore[attr-defined]
                    f"{loop_state.run_config.agent or '-'}"  # type: ignore[attr-defined]
                ),
                f"Started: {format_timestamp(loop_state.created_at)}",  # type: ignore[attr-defined]
                (
                    f"Updated/Avg: {format_timestamp(loop_state.updated_at)} · "  # type: ignore[attr-defined]
                    f"{format_duration(loop_state.average_duration_seconds)}"  # type: ignore[attr-defined]
                ),
                f"Last: {loop_state.last_summary or '-'}",  # type: ignore[attr-defined]
            ]
        )

    def _workspace_scope_text(self, state: object | None) -> str:
        root = self._input_value("#workspace-root", str(self.launch_cwd or Path.home()))
        include_paths = self._textarea_value("#workspace-include", "src/**\ntests/**\ndocs/**")
        exclude_paths = self._textarea_value(
            "#workspace-exclude",
            ".git/**\n.venv/**\n.ailoop/**\nnode_modules/**",
        )
        branch_strategy = branch_strategy_label(
            self._select_value("#workspace-branch-strategy", "current")
        )
        _mode, schedule_type, schedule_every = self._state_mode_and_schedule(state)
        schedule_scope = schedule_type_label(schedule_type, schedule_every)
        quiet_hours = "on" if self._checkbox_value("#config-quiet-hours", False) else "off"
        include_count = len([line for line in include_paths.splitlines() if line.strip()])
        exclude_count = len([line for line in exclude_paths.splitlines() if line.strip()])
        return "\n".join(
            [
                f"root: {root}",
                f"include: {include_count} patterns",
                f"exclude: {exclude_count} patterns",
                f"branch: {self.current_branch}",
                f"strategy: {branch_strategy}",
                f"schedule: {schedule_scope}",
                f"quiet-hours: {quiet_hours}",
                "workspace root is enforced as runner cwd",
            ]
        )

    def _config_status_text(self, state: object | None) -> str:
        mode, schedule_type, schedule_every = self._state_mode_and_schedule(state)
        schedule_scope = schedule_type_label(schedule_type, schedule_every)
        if state is None:
            return (
                "Draft config · new loop launch · "
                f"mode {mode} · schedule {schedule_scope}"
            )
        loop_state = state
        return (
            f"Editing loop {short_loop_id(loop_state.loop_id)} · "  # type: ignore[attr-defined]
            f"status {short_status(loop_state.status)} · "  # type: ignore[attr-defined]
            f"mode {mode} · schedule {schedule_scope}"
        )

    def _iteration_progress_text(self, state: object | None) -> str:
        if state is None:
            return "[b][#4ea3ff]ITERATION PROGRESS[/][/]\n\nNo loop selected."
        loop_state = state
        target = loop_state.run_config.steps  # type: ignore[attr-defined]
        progress_count = effective_iteration_count(
            loop_state.completed_iterations,  # type: ignore[attr-defined]
            loop_state.current_iteration,  # type: ignore[attr-defined]
            loop_state.status,  # type: ignore[attr-defined]
        )
        progress = render_progress_text(progress_count, target, width=12)
        return "\n".join(
            [
                "[b][#4ea3ff]ITERATION PROGRESS[/][/]",
                "",
                (
                    "Iter: "
                    f"{loop_state.current_iteration or loop_state.completed_iterations} / "
                    f"{target or '∞'}"
                ),
                f"Bar: {progress}",
                "",
                "Steps",
                *step_status_lines(loop_state.completed_iterations, target, loop_state.status),  # type: ignore[attr-defined]
            ]
        )

    def _iteration_history_card_text(self, state: object | None) -> str:
        if state is None:
            return "[b][#4ea3ff]ITERATION HISTORY[/][/]\n\nNo loop selected."
        loop_state = state
        lines = ["[b][#4ea3ff]ITERATION HISTORY[/][/]", ""]
        target = loop_state.run_config.steps  # type: ignore[attr-defined]
        if not loop_state.iterations:  # type: ignore[attr-defined]
            lines.append("#1 Queue · waiting")
            if target:
                for number in range(2, min(target, 5) + 1):
                    lines.append(f"#{number} Queue · pending")
            return "\n".join(lines)
        has_unfinished_current = False
        for item in loop_state.iterations[-6:]:  # type: ignore[attr-defined]
            if item.success is True:
                state_label = "Done"
            elif item.success is False:
                state_label = "Fail"
            else:
                state_label = "Run"
            if item.number == loop_state.current_iteration and item.success is None:  # type: ignore[attr-defined]
                has_unfinished_current = True
            lines.append(
                f"#{item.number} {state_label} · {format_compact_timestamp(item.started_at)} · "
                f"{format_duration(item.duration_seconds)}"
            )
        if loop_state.status == "running" and not has_unfinished_current:  # type: ignore[attr-defined]
            lines.append(f"#{loop_state.current_iteration} Run · now")  # type: ignore[attr-defined]
        queued_start = len(loop_state.iterations) + 1  # type: ignore[attr-defined]
        if target:
            for number in range(queued_start, min(target, queued_start + 2) + 1):
                if loop_state.status == "running" and number == loop_state.current_iteration:  # type: ignore[attr-defined]
                    continue
                lines.append(f"#{number} Queue · pending")
        return "\n".join(lines)

    def _actions_status_text(self, state: object | None) -> str:
        if state is None:
            return "Draft loop ready to start from the current config."
        loop_state = state
        status = loop_state.status  # type: ignore[attr-defined]
        mode, _schedule_type, _schedule_every = self._state_mode_and_schedule(loop_state)
        actions: list[str] = []
        if status in {"paused", "stopped", "failed", "idle"} and mode != "scheduled":
            actions.append("continue ready")
        elif status == "idle" and mode == "scheduled":
            actions.append("continue waiting")
        if status in {"running", "pause_requested"}:
            actions.append("pause ready")
        if status in {"running", "pause_requested", "paused"}:
            actions.append("stop ready")
        if status in {"paused", "stopped", "failed", "completed"}:
            actions.append("restart ready")
        actions.append("next ready" if self._can_next_iteration(loop_state) else "next blocked")
        if getattr(loop_state, "queued_follow_up", None):
            actions.append("follow-up queued")
        action_text = " · ".join(actions)
        return f"{short_loop_id(loop_state.loop_id)} · {short_status(status)} · {action_text}"

    def _can_queue_follow_up(self, state: object | None) -> bool:
        if state is None:
            return False
        loop_state = state
        if loop_state.status == "completed" and not self.service.should_continue(loop_state):  # type: ignore[arg-type]
            return False
        return True

    def _can_next_iteration(self, state: object | None) -> bool:
        if state is None:
            return False
        loop_state = state
        mode, _schedule_type, _schedule_every = self._state_mode_and_schedule(loop_state)
        if mode == "scheduled":
            return False
        if loop_state.status not in {"idle", "paused", "stopped", "failed"}:  # type: ignore[attr-defined]
            return False
        return self.service.should_continue(loop_state)  # type: ignore[arg-type]

    def _schedule_countdown_text(self) -> str:
        interval_kind = self._select_value("#schedule-type", self._config_interval_value())
        raw_value = self._input_value("#schedule-every", "0")
        start_time = self._input_value("#schedule-start-time", "Now")
        return self._schedule_countdown_from(interval_kind, raw_value, start_time)

    def _schedule_card_text(self, state: object | None) -> str:
        _mode, interval_kind, raw_value = self._state_mode_and_schedule(state)
        dashboard_config = getattr(state, "dashboard_config", {}) if state is not None else {}
        start_time = str(
            dashboard_config.get(
                "schedule_start",
                self._input_value("#schedule-start-time", "Now"),
            )
        )
        timezone = str(
            dashboard_config.get(
                "schedule_timezone",
                self._select_value("#schedule-timezone", "local"),
            )
        ).upper()
        type_label = {
            "continuous": "continuous",
            "minutes": "minutes",
            "hours": "hours",
            "daily": "daily",
            "weekly": "weekly",
            "cron": "cron",
        }.get(interval_kind, interval_kind)
        countdown = self._selected_schedule_countdown_text(state)
        return (
            f"Sched: {type_label} · every {raw_value} · start {start_time} · "
            f"tz {timezone} · {compact_countdown_text(countdown)}"
        )

    def _safety_card_text(self, state: object | None) -> str:
        autonomy = {
            "level-1": "Level 1 Observe",
            "level-2": "Level 2 Suggest",
            "level-3": "Level 3 Edit",
            "level-4": "Level 4 Edit + Commit",
            "level-5": "Level 5 Edit + Commit + Push",
        }.get(self._select_value("#safety-autonomy", "level-3"), "Level 3 Edit")
        branch_strategy = {
            "current": "current branch",
            "new": "new branch",
            "per-iteration": "branch per iteration",
        }.get(self._select_value("#workspace-branch-strategy", "current"), "current branch")
        ask_before_commit = self._checkbox_value("#safety-ask-before-commit", True)
        ask_before_push = self._checkbox_value("#safety-ask-before-push", True)
        auto_commit = self._checkbox_value("#safety-auto-commit", False)
        auto_push = self._checkbox_value("#safety-auto-push", False)
        backup_branch = self._checkbox_value("#safety-create-backup-branch", True)
        auto_stop = self._checkbox_value("#safety-auto-stop-on-limit", True)
        ask_commit = "on" if ask_before_commit else "off"
        ask_push = "on" if ask_before_push else "off"
        auto_commit_text = "on" if auto_commit else "off"
        auto_push_text = "on" if auto_push else "off"
        backup_text = "on" if backup_branch else "off"
        stop_text = "on" if auto_stop else "off"
        limits = (
            f"{self._input_value('#safety-max-runtime', '4h')}/"
            f"{self._input_value('#safety-max-files-changed', '100')}/"
            f"{self._input_value('#safety-max-commits', '10')}"
        )
        return (
            f"Safety: {autonomy} · {branch_strategy} · ask C {ask_commit}/P {ask_push} · "
            f"auto C {auto_commit_text}/P {auto_push_text} · backup {backup_text} · "
            f"limits {limits} · stop {stop_text}"
        )

    def _metrics_today_text(self) -> str:
        states = self.service.list_loops()
        iterations = [
            item for state in states for item in state.iterations if is_local_today(item.started_at)
        ]
        total_runs = len(iterations)
        successful = sum(1 for item in iterations if item.success)
        modified_files = sum(extract_modified_files(item.summary) for item in iterations)
        commits_created = sum(extract_commit_signal(item.summary) for item in iterations)
        token_usage = sum(extract_token_usage(item.summary) for item in iterations)
        cost_usage = sum(extract_cost_usage(item.summary) for item in iterations)
        success_rate = int((successful / total_runs) * 100) if total_runs else 0
        avg_runtime = (
            sum((item.duration_seconds or 0) for item in iterations) / total_runs
            if total_runs
            else 0
        )
        success_bar = mini_bar(successful, total_runs or 1)
        return "\n".join(
            [
                "[b][#4ea3ff]METRICS TODAY[/][/]",
                "",
                f"Runs: {total_runs}",
                f"Success rate: {success_rate}%  {success_bar}",
                f"Average runtime: {format_duration(avg_runtime)}",
                f"Files modified: {modified_files}",
                f"Commits created: {commits_created}",
                f"Token usage: {token_usage}",
                f"Cost usage: ${cost_usage:.2f}",
            ]
        )

    def _notifications_text(self) -> str:
        notify_terminal = self._checkbox_value("#notify-terminal", True)
        notify_slack = self._checkbox_value("#notify-slack", False)
        notify_email = self._checkbox_value("#notify-email", False)
        return (
            "Notify: "
            f"start {'on' if self._checkbox_value('#notify-start', True) else 'off'} · "
            f"success {'on' if self._checkbox_value('#notify-success', True) else 'off'} · "
            f"failure {'on' if self._checkbox_value('#notify-failure', True) else 'off'} · "
            f"limit {'on' if self._checkbox_value('#notify-limit', True) else 'off'} · "
            f"complete {'on' if self._checkbox_value('#notify-complete', True) else 'off'} · "
            f"chan T {'on' if notify_terminal else 'off'}/"
            f"S {'on' if notify_slack else 'off'}/"
            f"E {'on' if notify_email else 'off'}"
        )

    def _ops_snapshot_text(self, state: object | None) -> str:
        if state is None:
            return "[b][#4ea3ff]OPS SNAPSHOT[/][/]\n\nNo loop selected."
        countdown = compact_countdown_text(self._schedule_countdown_text()).removeprefix("next ")
        autonomy_raw = self._select_value("#safety-autonomy", "level-3")
        autonomy = f"L{autonomy_raw.removeprefix('level-')}"
        branch = {
            "current": "current",
            "new": "new",
            "per-iteration": "per-it",
        }.get(self._select_value("#workspace-branch-strategy", "current"), "current")
        limits = (
            f"{self._input_value('#safety-max-runtime', '4h')}/"
            f"{self._input_value('#safety-max-files-changed', '100')}/"
            f"{self._input_value('#safety-max-commits', '10')}"
        )
        ask_commit = "on" if self._checkbox_value("#safety-ask-before-commit", True) else "off"
        ask_push = "on" if self._checkbox_value("#safety-ask-before-push", True) else "off"
        auto_commit = "on" if self._checkbox_value("#safety-auto-commit", False) else "off"
        auto_push = "on" if self._checkbox_value("#safety-auto-push", False) else "off"
        notify_states = "/".join(
            [
                "on" if self._checkbox_value("#notify-start", True) else "off",
                "on" if self._checkbox_value("#notify-success", True) else "off",
                "on" if self._checkbox_value("#notify-failure", True) else "off",
                "on" if self._checkbox_value("#notify-limit", True) else "off",
                "on" if self._checkbox_value("#notify-complete", True) else "off",
            ]
        )
        notify_channels = "/".join(
            [
                "on" if self._checkbox_value("#notify-terminal", True) else "off",
                "on" if self._checkbox_value("#notify-slack", False) else "off",
                "on" if self._checkbox_value("#notify-email", False) else "off",
            ]
        )
        return "\n".join(
            [
                "[b][#4ea3ff]OPS SNAPSHOT[/][/]",
                f"Sched {countdown} · Safe {autonomy} {branch} · lim {limits}",
                (
                    f"C/P {ask_commit}/{ask_push} · aC/P {auto_commit}/{auto_push} · "
                    f"N {notify_states} · ch {notify_channels}"
                ),
            ]
        )

    def _legacy_detail_text(self, state: object | None) -> str:
        if state is None:
            return self._unselected_detail_message()
        loop_state = state
        target = loop_state.run_config.steps  # type: ignore[attr-defined]
        progress = render_progress_text(loop_state.completed_iterations, target, width=5)  # type: ignore[attr-defined]
        lines = [
            f"RUN {loop_state.loop_id}",  # type: ignore[attr-defined]
            "",
            "OVERVIEW",
            f"status: {short_status(loop_state.status)}",  # type: ignore[attr-defined]
            f"progress: {progress}",
            f"runner: {loop_state.run_config.runner}",  # type: ignore[attr-defined]
            f"agent: {loop_state.run_config.agent or '-'}",  # type: ignore[attr-defined]
            f"last: {loop_state.last_summary or '-'}",  # type: ignore[attr-defined]
            "",
            "CONTROL",
            f"control: {loop_state.control}",  # type: ignore[attr-defined]
            f"exit: {loop_state.last_exit_code}",  # type: ignore[attr-defined]
            f"failures: {loop_state.consecutive_failures}",  # type: ignore[attr-defined]
            "",
            "TIMING",
            f"avg: {loop_state.average_duration_seconds:.2f}s",  # type: ignore[attr-defined]
            f"total: {loop_state.total_duration_seconds:.2f}s",  # type: ignore[attr-defined]
            "",
            "NOTES",
            f"summary: {loop_state.last_summary or '-'}",  # type: ignore[attr-defined]
        ]
        if loop_state.run_config.task_file:  # type: ignore[attr-defined]
            try:
                task_state = parse_task_file(
                    Path(loop_state.run_config.task_file),  # type: ignore[attr-defined]
                    loop_state.run_config.max_doing,  # type: ignore[attr-defined]
                )
                lines.extend(
                    [
                        "",
                        "TASK FILE",
                        f"task file: {loop_state.run_config.task_file}",  # type: ignore[attr-defined]
                        (
                            f"tasks: to do {len(task_state.todo)} · doing "
                            f"{len(task_state.doing)} · done {len(task_state.done)}"
                        ),
                    ]
                )
            except Exception as exc:
                task_error = render_task_file_error(  # type: ignore[attr-defined]
                    Path(loop_state.run_config.task_file),
                    exc,
                ).splitlines()
                lines.extend(["", *task_error])
        return "\n".join(lines)

    def _history_log_text(self, state: object | None) -> str:
        workspace_root = str(self._active_workspace_root(state) or "")
        rows: list[str] = []
        if workspace_root:
            rows.append(f"WORKSPACE HISTORY · {workspace_root}")
            for entry in self.service.workspace_history.recent_entries(
                workspace_root,
                limit=20,
                max_chars=4000,
            ):
                text_value = " ".join((entry.prompt or entry.summary or "-").split())[:220]
                rows.append(
                    f"[{format_timestamp(entry.recorded_at)}] {entry.kind} · "
                    f"loop={short_loop_id(entry.loop_id)} · {text_value}"
                )
            if len(rows) == 1:
                rows.append("No workspace prompt history yet.")
        if state is None:
            return "\n".join(rows or ["No loop selected."])
        loop_state = state
        rows.extend(["", f"LOOP ITERATIONS · {loop_state.loop_id}"])
        if not loop_state.iterations:  # type: ignore[attr-defined]
            rows.append("No iteration history yet.")
            return "\n".join(rows)
        for item in reversed(loop_state.iterations[-20:]):  # type: ignore[attr-defined]
            label = "ok" if item.success else "fail"
            rows.append(
                f"[{format_timestamp(item.started_at)}] [{label}] iter={item.number} "
                f"duration={format_duration(item.duration_seconds)} exit={item.exit_code}"
            )
            if item.summary:
                rows.append(f"  summary: {item.summary}")
            if item.prompt_file:
                rows.append(f"  prompt: {item.prompt_file}")
        return "\n".join(rows)

    def _metrics_log_text(self, state: object | None) -> str:
        lines = [self._metrics_today_text().replace("[b][#4ea3ff]", "").replace("[/][/]", "")]
        if state is not None:
            loop_state = state
            lines.extend(
                [
                    "",
                    "Selected loop",
                    f"  loop_id: {loop_state.loop_id}",  # type: ignore[attr-defined]
                    f"  status: {loop_state.status}",  # type: ignore[attr-defined]
                    f"  completed_iterations: {loop_state.completed_iterations}",  # type: ignore[attr-defined]
                    f"  average_runtime: {format_duration(loop_state.average_duration_seconds)}",  # type: ignore[attr-defined]
                    f"  total_runtime: {format_duration(loop_state.total_duration_seconds)}",  # type: ignore[attr-defined]
                ]
            )
        return "\n".join(lines)

    def _events_log_text(self, loop_id: str) -> str:
        paths = self.service.loop_paths(loop_id)
        if not paths["events"].exists():
            return "No events yet."
        rows = []
        for raw_line in paths["events"].read_text().splitlines()[-80:]:
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                rows.append(raw_line)
                continue
            at = format_timestamp(payload.get("at"))
            event_name = payload.get("event", "event")
            status = payload.get("control") or payload.get("exit_code") or "ok"
            status_color = "green" if status in {"ok", 0, "run"} else "yellow"
            if status in {"stop", "pause"} or str(status).startswith("stop"):
                status_color = "red"
            rows.append(
                f"[cyan]{at}[/] [blue][{event_name}][/blue] "
                f"[{status_color}]status={status}[/{status_color}]"
            )
        return "\n".join(rows)

    def _sync_button_state(self) -> None:
        self._sync_form_controls()
        state = self._selected_state()
        status = state.status if state is not None else None
        state_mode = (
            self._state_mode_and_schedule(state)[0]
            if state is not None
            else self._config_mode_value()
        )
        can_pause = status in {"running", "pause_requested"}
        can_resume = status in {"paused", "stopped", "failed", "idle"} or (
            status is None and self._form_supports_run()
        )
        if state is not None and state_mode == "scheduled":
            can_resume = False
        can_stop = status in {"running", "pause_requested", "paused"}
        can_restart = status in {"paused", "stopped", "failed", "completed"}
        can_restart_reset = state is not None and status not in {"running", "pause_requested"}
        can_next_iteration = self._can_next_iteration(state)
        memory_entry = self._primary_memory_entry()
        for button_id, active in {
            "filter-running": self.filter_mode == "running",
            "filter-active": self.filter_mode == "active",
            "filter-all": self.filter_mode == "all",
            "log-stdout": self.log_kind == "stdout",
            "log-stderr": self.log_kind == "stderr",
            "log-prompt": self.log_kind == "prompt",
            "log-events": self.log_kind == "events",
            "log-metrics": self.log_kind == "metrics",
            "log-history": self.log_kind == "history",
            "log-memory": self.log_kind == "memory" and self.memory_filter == "all",
            "log-memory-favorites": self.log_kind == "memory" and self.memory_filter == "favorites",
            "log-memory-history": self.log_kind == "memory" and self.memory_filter == "history",
            "log-memory-presets": self.log_kind == "memory" and self.memory_filter == "presets",
            "log-memory-archived": self.log_kind == "memory" and self.memory_filter == "archived",
            "memory-scope-toggle": self.log_kind == "memory" and self.memory_all_folders,
        }.items():
            try:
                self.query_one(f"#{button_id}", Button).set_class(active, "active")
            except Exception:
                continue
        self.query_one("#pause", Button).disabled = not can_pause
        try:
            self.query_one("#start-continue", Button).disabled = not can_resume
            if state is None:
                start_label = "▶ Start"
            elif status in {"paused", "stopped", "failed"}:
                start_label = "▶ Continue"
            else:
                start_label = "▶ Start"
            self.query_one("#start-continue", Button).label = start_label
        except Exception:
            self.query_one("#resume", Button).disabled = not can_resume
        self.query_one("#stop", Button).disabled = not can_stop
        try:
            self.query_one("#restart", Button).disabled = not can_restart
            self.query_one("#restart-reset", Button).disabled = not can_restart_reset
            self.query_one("#next-iteration", Button).disabled = not can_next_iteration
            self.query_one("#queue-follow-up", Button).disabled = (
                not self._can_queue_follow_up(state) or not bool(self._follow_up_text())
            )
            self.query_one("#clear-follow-up", Button).disabled = not bool(
                state is not None
                and state.queued_follow_up
                and not state.pending_single_iteration
                and not self.service.store.is_locked(state.loop_id)
            )
            self.query_one("#save-config", Button).disabled = status in {
                "running",
                "pause_requested",
                "stop_requested",
            } 
            self.query_one("#run-loop", Button).disabled = not self._form_supports_run()
            self.query_one("#restart-reset", Button).label = "↺ Reset Counter"
            self.query_one("#next-iteration", Button).label = "≫ Next Iteration"
            self.query_one("#queue-follow-up", Button).label = "Queue & Run Follow-up"
            self.query_one("#clear-follow-up", Button).label = "Clear Queued"
            self.query_one("#save-config", Button).label = "Save Config"
            self.query_one("#run-loop", Button).label = "Run Loop"
        except Exception:
            pass
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
            "z confirm archive" if self.memory_archive_armed else "z archive"
        )
        self.query_one("#memory-delete", Button).disabled = not (
            self.log_kind == "memory" and memory_entry is not None
        )
        self.query_one("#memory-delete", Button).label = (
            "x confirm delete" if self.memory_delete_armed else "x delete"
        )
        try:
            self.query_one("#remove", Button).label = (
                "✖ Confirm delete" if self.delete_armed else "✖ Delete"
            )
        except Exception:
            pass
        memory_visible = self.log_kind == "memory"
        self.query_one("#memory-action-toolbar", Horizontal).set_class(
            not memory_visible, "memory-ui-hidden"
        )
        self.query_one("#memory-query", Input).set_class(not memory_visible, "memory-ui-hidden")
        self._render_help_bar(state)

    def _render_help_bar(self, state: object | None) -> None:
        bar = self.query_one("#help_bar", Static)
        base = self._footer_base_text()
        if self.log_kind == "memory":
            bar.update(self._memory_help_text())
            return
        if state is None:
            bar.update(base + " · no loop selected · run from config or choose a loop")
            return
        loop_state = state.status  # type: ignore[attr-defined]
        actions: list[str] = []
        if loop_state in {"running", "pause_requested"}:
            actions.append("p pause")
        if loop_state in {"paused", "stopped", "failed", "idle"}:
            actions.append("u continue")
        if loop_state in {"running", "pause_requested", "paused"}:
            actions.append("s stop")
        if (
            loop_state not in {"running", "pause_requested", "stop_requested"}
            and not self.delete_armed
        ):
            actions.append("d delete")
        if self.delete_armed:
            actions.append("d confirm delete")
        if loop_state in {"paused", "stopped", "failed", "completed"}:
            actions.append("restart")
        actions.append("ctrl+j/k switch loop")
        actions.append("i focus follow-up")
        actions.append("ctrl+enter queue/run follow-up")
        actions.append("N next iteration")
        action_text = " · ".join(actions) if actions else "read only"
        bar.update(f"{base} · actions {action_text}")

    def _filtered_loops(self):
        states = self.service.list_loops()
        if self.filter_mode == "running":
            states = [state for state in states if state.status in RUNNING_STATUSES]
        elif self.filter_mode == "active":
            states = [state for state in states if state.status in ACTIVE_STATUSES]
        if not self.loop_query:
            return states
        query = self.loop_query.lower()
        return [
            state
            for state in states
            if query in state.loop_id.lower()
            or query in state.status.lower()
            or query in (state.run_config.agent or "").lower()
            or query in state.run_config.runner.lower()
        ]

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
        return (
            "No loops in the current filter.\n\n"
            "Press l for all loops, g for running, or a for active."
        )

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
                "      5 metrics · 6 history · 7 memory · f favorites · h mem-history",
                "      m presets · 0 archived",
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
        folder = self._memory_folder()
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

    def _memory_scope_text(self, *, compact: bool = False) -> str:
        if self.launch_cwd is None:
            if compact:
                return "all(no-cwd)"
            return "all-folders (cwd unavailable)"
        return "all-folders" if self.memory_all_folders else "cwd"

    def _memory_scope_instruction(self, *, lowercase: bool = False) -> str:
        if self.launch_cwd is None:
            return "cwd scope unavailable; showing all folders"
        prefix = "press" if lowercase else "Press"
        return f"{prefix} o to {self._memory_scope_toggle_hint()}"

    def _memory_query_placeholder(self) -> str:
        return "memory query: title/id/label"

    def _memory_recovery_hint(self, *, lowercase: bool = False) -> str:
        prefix = "press" if lowercase else "Press"
        if self.memory_query:
            return f"{prefix} esc to clear the query"
        if self.memory_label is not None:
            return f"{prefix} c to clear the label"
        if self.memory_filter == "archived":
            return f"{prefix} 5 to return to all entries"
        return f"{prefix} {self._memory_filter_hint()} to switch this view"

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
                "Archive one from the memory list with z twice.\n"
                f"{self._memory_recovery_hint()}.\n"
                f"{self._memory_scope_instruction()}."
            )
        return (
            "No memory entries found.\n\n"
            f"scope: {self._memory_scope_text()} · filter: {self.memory_filter} · "
            f"label: {self.memory_label or '-'} · query: {self.memory_query or '-'}\n"
            "Create one with:\n"
            '  ailoop memory save "Quick review" "Review the repo"\n\n'
            f"Then {self._memory_recovery_hint(lowercase=True)}. "
            f"{self._memory_scope_instruction()}."
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
                    self._memory_recovery_hint(lowercase=True),
                    self._memory_scope_instruction(lowercase=True),
                ]
            )
        else:
            lines.extend(
                [
                    "no memory entry is selected",
                    "save one with ailoop memory save ...",
                    self._memory_recovery_hint(lowercase=True),
                    self._memory_scope_instruction(lowercase=True),
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
                f"MEMORY {entry.id}",
                "",
                "OVERVIEW",
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
                "RUN CONFIG",
                f"runner: {entry.current.runner}",
                f"agent: {entry.current.agent or '-'}",
                f"steps: {entry.current.steps}",
                "",
                "USAGE",
                f"used: {entry.use_count}",
                f"last used: {entry.last_used_at or '-'}",
                f"versions: {entry.latest_version}",
                "",
                "COMMANDS",
                f"show: {show_command}",
                f"edit: {edit_command}",
                f"favorite: {favorite_command}",
                f"archive: {archive_command}",
                "",
                "SHORTCUTS",
                "browse: [ ]",
                "labels: b n c",
                "scope/query: o / esc",
                "actions: 8 9 v z x",
            ]
        )

    def refresh_data(self) -> None:
        states = self._filtered_loops()
        table = self.query_one(DataTable)
        table.clear(columns=False)
        self._render_sidebar_stats(states)
        self._render_system_stats(states)
        if self.initial_loop_id and self.selected_loop_id is None and not self._draft_loop_selected:
            self.selected_loop_id = self.initial_loop_id
        if self.selected_loop_id and not any(s.loop_id == self.selected_loop_id for s in states):
            try:
                self.service.load_loop(self.selected_loop_id)
            except FileNotFoundError:
                pass
            else:
                self.filter_mode = "all"
                states = self._filtered_loops()
        if self.selected_loop_id is None and states and not self._draft_loop_selected:
            self.selected_loop_id = states[0].loop_id
        if self.selected_loop_id and not any(s.loop_id == self.selected_loop_id for s in states):
            self.selected_loop_id = states[0].loop_id if states else None

        if not states:
            table.add_row("-", self.filter_mode, "-", "-", "-", key="empty")

        for state in states:
            target = state.run_config.steps
            progress_count = effective_iteration_count(
                state.completed_iterations,
                state.current_iteration,
                state.status,
            )
            iteration_text = f"{progress_count}/{target or '∞'}"
            icon = STATUS_ICONS.get(state.status, "•")
            mode, _schedule_type, _schedule_every = self._state_mode_and_schedule(state)
            mode_label = {
                "fixed": "fixed",
                "infinite": "infinite",
                "scheduled": "scheduled",
            }.get(mode, mode)
            table.add_row(
                short_loop_id(state.loop_id),
                f"{icon} {short_status(state.status)}",
                iteration_text,
                mode_label,
                (state.run_config.agent or "-")[:12],
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
        try:
            loop_summary = self.query_one("#loop_summary", Static)
            actions_status = self.query_one("#actions-status", Static)
            config_status = self.query_one("#config-status", Static)
            workspace_scope = self.query_one("#workspace_scope", Static)
            iteration_progress = self.query_one("#iteration_progress", Static)
            iteration_history = self.query_one("#iteration_history", Static)
            ops_snapshot = self.query_one("#ops_snapshot", Static)
            schedule_preview = self.query_one("#schedule-preview", Static)
            safety_preview = self.query_one("#safety-preview", Static)
            metrics_today = self.query_one("#metrics_today", Static)
            notifications_preview = self.query_one("#notifications-preview", Static)
            modern_layout = True
        except Exception:
            modern_layout = False
        log_meta = self.query_one("#log_meta", Static)
        log_view = self.query_one("#log_view", Static)
        state = self._selected_state()
        if modern_layout:
            self._sync_config_form_from_state(state)
        self._refresh_workspace_branch(state)
        self._render_summary_bar()
        if modern_layout:
            loop_summary.update(self._loop_summary_text(state))
            actions_status.update(self._actions_status_text(state))
            config_status.update(self._config_status_text(state))
            workspace_scope.update(self._workspace_scope_text(state))
            iteration_progress.update(self._iteration_progress_text(state))
            iteration_history.update(self._iteration_history_card_text(state))
            ops_snapshot.update(self._ops_snapshot_text(state))
            schedule_preview.update(self._schedule_card_text(state))
            safety_preview.update(self._safety_card_text(state))
            metrics_today.update(self._metrics_today_text())
            notifications_preview.update(self._notifications_text())
            schedule_preview.remove_class("detail-preview-hidden")
            safety_preview.remove_class("detail-preview-hidden")
            notifications_preview.remove_class("detail-preview-hidden")
        else:
            self.query_one("#detail_view", Static).update(self._legacy_detail_text(state))
        if self.log_kind == "memory":
            log_meta.update(self._memory_log_meta())
            log_view.update(self._memory_log_text())
            return
        if state is None:
            log_meta.update(f"source {self.log_kind} · no loop selected")
            log_view.update(self._empty_loop_message())
            return

        paths = self.service.loop_paths(state.loop_id) if state.iterations else None
        log_meta.update(
            f"source {self.log_kind} · loop {short_loop_id(state.loop_id)} · "
            f"workspace {state.run_config.workspace_root or '-'} · refresh 1s"
        )
        if self.log_kind == "events":
            if paths:
                log_view.update(self._events_log_text(state.loop_id))
            else:
                log_view.update("No events yet.")
            return
        if self.log_kind == "history":
            log_view.update(colorize_log_text(self._history_log_text(state)))
            return
        if self.log_kind == "metrics":
            log_view.update(colorize_log_text(self._metrics_log_text(state)))
            return
        if not paths:
            log_view.update("No logs yet.")
            return
        log_view.update(colorize_log_text(tail_text(paths[self.log_kind])))

    @on(DataTable.RowSelected)
    def on_loop_selected(self, event: DataTable.RowSelected) -> None:
        self.selected_loop_id = str(event.row_key.value)
        self._draft_loop_selected = False
        self._render_selected()

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "refresh":
            self.refresh_data()
        elif button_id == "new-loop":
            self.action_new_loop()
        elif button_id == "pause":
            self.action_pause_selected()
        elif button_id == "start-continue":
            self.action_resume_selected()
        elif button_id == "stop":
            self.action_stop_selected()
        elif button_id == "restart":
            self.action_restart_selected()
        elif button_id == "restart-reset":
            self.action_restart_reset_selected()
        elif button_id == "next-iteration":
            self.action_next_iteration()
        elif button_id == "queue-follow-up":
            self.action_queue_follow_up()
        elif button_id == "clear-follow-up":
            self.action_clear_follow_up()
        elif button_id == "save-config":
            self.action_save_config()
        elif button_id == "run-loop":
            self.action_run_loop()
        elif button_id == "filter-running":
            self.action_filter_running()
        elif button_id == "filter-active":
            self.action_filter_active()
        elif button_id == "filter-all":
            self.action_filter_all()
        elif button_id == "log-metrics":
            self.action_set_log_metrics()
        elif button_id == "log-history":
            self.action_set_log_history()
        elif button_id == "log-memory":
            self.action_set_log_memory()
        elif button_id == "log-memory-favorites":
            self.action_set_log_memory_favorites()
        elif button_id == "log-memory-history":
            self.action_set_log_memory_history()
        elif button_id == "log-memory-presets":
            self.action_set_log_memory_presets()
        elif button_id == "log-memory-archived":
            self.action_set_log_memory_archived()
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

    @on(Input.Changed, "#loop-query")
    def on_loop_query_changed(self, event: Input.Changed) -> None:
        self.loop_query = event.value.strip()
        self.delete_armed = False
        self.refresh_data()

    @on(Select.Changed, "#workspace-recent")
    def on_recent_workspace_changed(self, event: Select.Changed) -> None:
        if event.value == Select.NULL:
            return
        root = str(event.value)
        if not root:
            return
        try:
            picker = self.query_one("#workspace-recent", Select)
            if str(picker.value) != root:
                return
            self.query_one("#workspace-root", Input).value = root
        except Exception:
            return
        self._refresh_workspace_branch()
        self._render_selected()

    @on(Input.Changed, "#workspace-root")
    def on_workspace_root_changed(self, event: Input.Changed) -> None:
        root = event.value.strip()
        if root != self.query_one("#workspace-root", Input).value.strip():
            return
        if root:
            picker = self.query_one("#workspace-recent", Select)
            picker.set_options(self._recent_workspace_options(root))
            picker.value = root
        self._update_workspace_root_status(root)
        self._refresh_workspace_branch()
        self._render_selected()

    @on(
        TextArea.Changed,
        "#workspace-include, #workspace-exclude, #config-prompt, #follow-up-prompt",
    )
    def on_textarea_changed(self, _event: TextArea.Changed) -> None:
        self._sync_button_state()
        self._render_selected()

    @on(
        Input.Changed,
        "#config-iterations, #config-interval-value, #config-quiet-start, #config-quiet-end, "
        "#config-jitter-value, #schedule-every, #schedule-start-time, #safety-max-runtime, "
        "#safety-max-files-changed, #safety-max-commits, #safety-max-token-usage, #safety-max-cost",
    )
    def on_dashboard_input_changed(self, _event: Input.Changed) -> None:
        self._sync_schedule_with_config()
        self._sync_button_state()
        self._render_selected()

    @on(
        Select.Changed,
        "#config-mode, #config-interval, #schedule-type, #schedule-timezone, #safety-autonomy, "
        "#workspace-branch-strategy",
    )
    def on_dashboard_select_changed(self, _event: Select.Changed) -> None:
        self._sync_schedule_with_config()
        self._sync_button_state()
        self._render_selected()

    @on(
        Checkbox.Changed,
        "#config-quiet-hours, #config-jitter, #safety-ask-before-commit, "
        "#safety-ask-before-push, #safety-auto-commit, #safety-auto-push, "
        "#safety-create-backup-branch, #safety-auto-stop-on-limit, #notify-start, "
        "#notify-success, #notify-failure, #notify-limit, "
        "#notify-complete, #notify-terminal, #notify-slack, #notify-email",
    )
    def on_dashboard_checkbox_changed(self, _event: Checkbox.Changed) -> None:
        self._sync_button_state()
        self._render_selected()

    def _apply_memory_query(self, value: str) -> None:
        self.memory_query = value.strip()
        self.memory_index = 0
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._sync_button_state()
        self._render_summary_bar()
        self._render_selected()

    def _spawn_cwd(self) -> Path:
        return self._active_workspace_root() or self.launch_cwd or Path.home()

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
            cwd=self._spawn_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _spawn_replay(self, entry_id: str, *, all_folders: bool = False) -> None:
        command = [
            sys.executable,
            "-m",
            "ailoop.cli",
            "--quiet",
            "--config",
            str(self.config_path),
            "replay",
            entry_id,
        ]
        if all_folders:
            command.append("--all-folders")
        subprocess.Popen(
            command,
            cwd=self._spawn_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def action_refresh_data(self) -> None:
        self.refresh_data()

    def action_new_loop(self) -> None:
        self.selected_loop_id = None
        self._config_bound_loop_id = None
        self.delete_armed = False
        self._draft_loop_selected = True
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
        self._render_summary_bar()
        self._render_selected()

    def action_set_log_stderr(self) -> None:
        self.log_kind = "stderr"
        self._sync_button_state()
        self._render_summary_bar()
        self._render_selected()

    def action_set_log_prompt(self) -> None:
        self.log_kind = "prompt"
        self._sync_button_state()
        self._render_summary_bar()
        self._render_selected()

    def action_set_log_events(self) -> None:
        self.log_kind = "events"
        self._sync_button_state()
        self._render_summary_bar()
        self._render_selected()

    def action_set_log_metrics(self) -> None:
        self.log_kind = "metrics"
        self._sync_button_state()
        self._render_summary_bar()
        self._render_selected()

    def action_set_log_history(self) -> None:
        self.log_kind = "history"
        self._sync_button_state()
        self._render_summary_bar()
        self._render_selected()

    def _activate_memory_filter(self, memory_filter: MemoryFilter) -> None:
        self.log_kind = "memory"
        self.memory_filter = memory_filter
        self.memory_index = 0
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self._sync_button_state()
        self._render_summary_bar()
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
        if not self._can_toggle_memory_scope():
            self.memory_all_folders = True
            self.memory_archive_armed = False
            self.memory_delete_armed = False
            self.notify("cwd scope unavailable; showing all folders")
            self._sync_button_state()
            self._render_selected()
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
        self._spawn_replay(entry.id, all_folders=self.memory_all_folders)
        self.notify(f"replay sent: {entry.id}")
        self.refresh_data()

    def action_memory_favorite(self) -> None:
        entry = self._primary_memory_entry()
        if entry is None:
            return
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        updated = self.memory.edit(
            entry.id,
            favorite=not entry.favorite,
            folder=self._memory_folder(),
        )
        state = "favorite on" if updated.favorite else "favorite off"
        self.notify(f"{state}: {updated.id}")
        self.refresh_data()

    def action_memory_restore(self) -> None:
        entry = self._primary_memory_entry()
        if entry is None or not entry.archived:
            return
        self.memory_archive_armed = False
        self.memory_delete_armed = False
        self.memory.edit(entry.id, archived=False, folder=self._memory_folder())
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
        self.memory.edit(entry.id, archived=True, folder=self._memory_folder())
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
        self.memory.delete(entry.id, folder=self._memory_folder())
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
            state = self._selected_state()
            if state is not None:
                if not self._validate_workspace_root():
                    return
                if self._state_mode_and_schedule(state)[0] == "scheduled":
                    self.notify(
                        "scheduled loops wait for their configured run window",
                        severity="warning",
                    )
                    return
                state.control = "run"  # type: ignore[attr-defined]
                self.service.store.save(state)
            self._spawn_resume(self.selected_loop_id)
            self.notify(f"resume sent: {self.selected_loop_id}")
            self.refresh_data()
            return
        if not self._form_supports_run():
            self.notify(
                "scheduled mode is visible in the dashboard but not executable yet",
                severity="warning",
            )
            return
        self.action_run_loop()

    def action_stop_selected(self) -> None:
        if self.selected_loop_id:
            self.delete_armed = False
            self.service.request_control(self.selected_loop_id, "stop")
            self.refresh_data()

    def action_save_config(self) -> None:
        if not self._validate_workspace_root():
            return
        state = self._selected_state()
        if state is None:
            self.notify("config draft captured in the form")
            self._sync_button_state()
            self._render_selected()
            return
        if state.status in {"running", "pause_requested", "stop_requested"}:
            self.notify("stop or pause the loop before saving config changes", severity="warning")
            return
        state.run_config = self._build_run_config_from_form(state)
        state.dashboard_config = self._dashboard_form_values()  # type: ignore[attr-defined]
        state.workspace_config = self._workspace_form_values()  # type: ignore[attr-defined]
        state.updated_at = datetime.now(UTC).isoformat()
        self.service.store.save(state)
        self.notify(f"config saved: {state.loop_id}")
        self.refresh_data()

    def action_run_loop(self) -> None:
        if not self._form_supports_run():
            self.notify("current loop configuration cannot run yet", severity="warning")
            return
        if not self._validate_workspace_root():
            return
        mode = self._config_mode_value()
        state = self._selected_state()
        if state is not None and state.status not in {
            "running",
            "pause_requested",
            "stop_requested",
        }:
            state.run_config = self._build_run_config_from_form(state)
            state.dashboard_config = self._dashboard_form_values()  # type: ignore[attr-defined]
            state.workspace_config = self._workspace_form_values()  # type: ignore[attr-defined]
            state.control = "run"  # type: ignore[attr-defined]
            if mode == "scheduled":
                state.status = "idle"  # type: ignore[attr-defined]
            state.updated_at = datetime.now(UTC).isoformat()
            self.service.store.save(state)
            if mode == "scheduled":
                self.notify(f"schedule saved: {state.loop_id}")
                self.refresh_data()
                return
            self._spawn_resume(state.loop_id)
            self.notify(f"run sent: {state.loop_id}")
            self.refresh_data()
            return
        run_config = self._build_run_config_from_form()
        created = self.service.create_loop(run_config)
        created.dashboard_config = self._dashboard_form_values()  # type: ignore[attr-defined]
        created.workspace_config = self._workspace_form_values()  # type: ignore[attr-defined]
        self.service.store.save(created)
        self.selected_loop_id = created.loop_id
        self._config_bound_loop_id = None
        if mode == "scheduled":
            self.notify(f"scheduled loop saved: {created.loop_id}")
            self.refresh_data()
            return
        self._spawn_resume(created.loop_id)
        self.notify(f"loop started: {created.loop_id}")
        self.refresh_data()

    def _move_loop_selection(self, delta: int) -> None:
        if self._text_input_has_focus():
            return
        states = self._filtered_loops()
        if not states:
            return
        selected = self.selected_loop_id
        index = 0
        if selected:
            for idx, item in enumerate(states):
                if item.loop_id == selected:
                    index = idx
                    break
        index = (index + delta) % len(states)
        self.selected_loop_id = states[index].loop_id
        self._draft_loop_selected = False
        self.refresh_data()

    def action_loop_next(self) -> None:
        self._move_loop_selection(1)

    def action_loop_prev(self) -> None:
        self._move_loop_selection(-1)

    def action_follow_up_focus(self) -> None:
        if self.log_kind == "memory":
            return
        self.query_one("#follow-up-prompt", TextArea).focus()

    def action_queue_follow_up(self) -> None:
        state = self._selected_state()
        if state is None:
            self.notify("select a loop before queueing a follow-up", severity="warning")
            return
        follow_up = self._follow_up_text()
        if not follow_up:
            self.notify("follow-up prompt is empty", severity="warning")
            return
        if not self._can_queue_follow_up(state):
            self.notify("follow-up queueing is not available for this loop", severity="warning")
            return
        run_next = state.status in {"idle", "paused", "stopped", "failed"}
        if run_next and not self._validate_workspace_root():
            return
        state = self.service.queue_follow_up(state.loop_id, follow_up, run_next=run_next)
        self.query_one("#follow-up-prompt", TextArea).text = ""
        if state.pending_single_iteration:
            self._spawn_resume(state.loop_id)
            self.notify(f"follow-up queued and next iteration started: {state.loop_id}")
        else:
            self.notify(f"follow-up queued: {state.loop_id}")
        self.refresh_data()

    def action_clear_follow_up(self) -> None:
        state = self._selected_state()
        if state is None or not state.queued_follow_up:
            self.notify("no queued follow-up to clear", severity="warning")
            return
        if state.pending_single_iteration or self.service.store.is_locked(state.loop_id):
            self.notify("follow-up is already committed to the next iteration", severity="warning")
            return
        self.service.clear_follow_up(state.loop_id)
        self.notify(f"queued follow-up cleared: {state.loop_id}")
        self.refresh_data()

    def action_next_iteration(self) -> None:
        if self._text_input_has_focus():
            return
        state = self._selected_state()
        if state is None:
            self.notify("select a loop before queueing the next iteration", severity="warning")
            return
        if not self._can_next_iteration(state):
            self.notify("next iteration is not available right now", severity="warning")
            return
        if not self._validate_workspace_root():
            return
        self.service.request_single_iteration(state.loop_id)  # type: ignore[attr-defined]
        self._spawn_resume(state.loop_id)  # type: ignore[attr-defined]
        self.notify(f"next iteration queued: {state.loop_id}")  # type: ignore[attr-defined]
        self.refresh_data()

    def action_restart_selected(self) -> None:
        state = self._selected_state()
        if state is None:
            return
        if not self._validate_workspace_root():
            return
        state.run_config = self._build_run_config_from_form(state)
        state.dashboard_config = self._dashboard_form_values()  # type: ignore[attr-defined]
        state.workspace_config = self._workspace_form_values()  # type: ignore[attr-defined]
        state.control = "run"
        state.pending_single_iteration = False  # type: ignore[attr-defined]
        state.status = "idle"
        state.updated_at = datetime.now(UTC).isoformat()
        self.service.store.save(state)
        self._spawn_resume(state.loop_id)
        self.notify(f"restart sent: {state.loop_id}")
        self.refresh_data()

    def action_restart_reset_selected(self) -> None:
        state = self._selected_state()
        if state is None:
            return
        if not self._validate_workspace_root():
            return
        state.run_config = self._build_run_config_from_form(state)
        state.dashboard_config = self._dashboard_form_values()  # type: ignore[attr-defined]
        state.workspace_config = self._workspace_form_values()  # type: ignore[attr-defined]
        state.control = "run"
        state.pending_single_iteration = False  # type: ignore[attr-defined]
        state.status = "idle"
        state.current_iteration = 0
        state.completed_iterations = 0
        state.last_exit_code = None
        state.consecutive_failures = 0
        state.total_duration_seconds = 0.0
        state.average_duration_seconds = 0.0
        state.last_summary = None
        state.iterations = []
        state.updated_at = datetime.now(UTC).isoformat()
        self.service.store.save(state)
        self._spawn_resume(state.loop_id)
        self.notify(f"restart reset sent: {state.loop_id}")
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
