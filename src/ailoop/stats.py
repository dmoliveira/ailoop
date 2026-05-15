from __future__ import annotations

import os
import sys

from .models import LoopState

STATUS_ICONS = {
    "idle": "⏳",
    "running": "▶",
    "pause_requested": "⏸",
    "paused": "⏸",
    "stop_requested": "⏹",
    "stopped": "⏹",
    "completed": "✅",
    "failed": "❌",
}

ANSI_RESET = "\033[0m"
ANSI_DIM = "\033[2m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"

COLOR_MODE = "auto"


def get_color_mode() -> str:
    return COLOR_MODE


def set_color_mode(mode: str) -> None:
    global COLOR_MODE
    COLOR_MODE = mode


def _use_color() -> bool:
    if COLOR_MODE == "always":
        return True
    if COLOR_MODE == "never":
        return False
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _style(text: str, color: str) -> str:
    if not _use_color():
        return text
    return f"{color}{text}{ANSI_RESET}"


def _status_icon(status: str) -> str:
    return STATUS_ICONS.get(status, "•")


def _status_label(status: str) -> str:
    if status in {"completed"}:
        return _style(status, ANSI_GREEN)
    if status in {"failed", "stopped"}:
        return _style(status, ANSI_RED)
    if status in {"paused", "pause_requested", "stop_requested"}:
        return _style(status, ANSI_YELLOW)
    if status in {"running"}:
        return _style(status, ANSI_BLUE)
    return status


def _progress_label(state: LoopState) -> str:
    target = state.run_config.steps
    if target is None:
        return f"{state.completed_iterations}/∞"
    return f"{state.completed_iterations}/{target}"


def render_status(state: LoopState) -> str:
    summary = state.last_summary or "-"
    return "\n".join(
        [
            f"{_status_icon(state.status)}  {state.loop_id} · {_status_label(state.status)}",
            (
                f"↳ progress {_progress_label(state)} · current {state.current_iteration} "
                f"· exit {state.last_exit_code}"
            ),
            (
                f"↳ runner {state.run_config.runner} · agent {state.run_config.agent or '-'} "
                f"· fail {state.consecutive_failures}"
            ),
            (
                f"↳ avg {state.average_duration_seconds:.2f}s · total "
                f"{state.total_duration_seconds:.2f}s "
                f"· ctrl {state.control}"
            ),
            f"{_style('↳ last', ANSI_DIM)} {summary}",
        ]
    )


def render_iteration_summary(state: LoopState) -> str:
    latest = state.iterations[-1] if state.iterations else None
    lines = [
        (
            f"🔁 iter {state.completed_iterations} · {_status_icon(state.status)} "
            f"{_status_label(state.status)} · exit {state.last_exit_code} "
            f"· {latest.duration_seconds:.2f}s"
            if latest and latest.duration_seconds is not None
            else (
                f"🔁 iter {state.completed_iterations} · {_status_icon(state.status)} "
                f"{_status_label(state.status)}"
            )
        ),
        (
            f"↳ loop {state.loop_id} · runner {state.run_config.runner} "
            f"· agent {state.run_config.agent or '-'}"
        ),
        (
            f"↳ progress {_progress_label(state)} · fail {state.consecutive_failures} "
            f"· avg {state.average_duration_seconds:.2f}s"
        ),
    ]
    if latest:
        lines.extend(
            [
                f"↳ time {latest.started_at} → {latest.finished_at}",
                f"↳ note {latest.summary or '-'}",
            ]
        )
    return "\n".join(lines)


def render_stats(state: LoopState, recent_limit: int = 5) -> str:
    lines = [
        render_status(state),
        "",
        _style("🕘 recent:", ANSI_DIM),
    ]
    recent = state.iterations[-recent_limit:]
    if not recent:
        lines.append("- none")
        return "\n".join(lines)

    for item in recent:
        duration = f"{item.duration_seconds:.2f}s" if item.duration_seconds is not None else "-"
        lines.append(
            f"- iter {item.number} · exit {item.exit_code} · ok {item.success} · {duration}"
        )
        lines.append(f"  note {item.summary or '-'}")
    return "\n".join(lines)


def render_loop_list(states: list[LoopState]) -> str:
    if not states:
        return '\n'.join(
            [
                'No loops found.',
                '↳ first run: ailoop init-config',
                '↳ then: ailoop run "Review the repo"',
            ]
        )

    lines = [
        "ID             State         Prog   Updated              Summary",
        "-------------  ------------  -----  -------------------  -------",
    ]
    for state in states:
        progress = _progress_label(state)
        updated = state.updated_at.replace("T", " ")[:19]
        summary = (state.last_summary or "-").replace("\n", " ")[:60]
        lines.append(
            f"{state.loop_id:<13}  {_status_icon(state.status)} {_status_label(state.status):<10}  "
            f"{progress:>5}  {updated:<19}  {summary}"
        )
    return "\n".join(lines)
