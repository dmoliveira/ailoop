from __future__ import annotations

from pathlib import Path

from .models import LoopState
from .tasks import TASK_FILE_RULES


def build_prompt(state: LoopState, iteration_number: int) -> str:
    parts: list[str] = []
    config = state.run_config
    if config.pre_prompt_enabled and config.pre_prompt.strip():
        parts.append(config.pre_prompt.strip())

    if config.attach_agent_file and config.agent_file:
        agent_path = Path(config.agent_file)
        if agent_path.exists():
            parts.append(f"AGENTS.md / instructions file:\n{agent_path.read_text().strip()}")

    if config.task_file:
        lines = ["Task file:", f"- path: {config.task_file}"]
        if config.stop_when_tasks_complete:
            lines.append("- stop when To do and Doing are empty")
        lines.append(TASK_FILE_RULES)
        parts.append("\n".join(lines))

    parts.append(f"User prompt:\n{config.prompt.strip()}")
    parts.append(
        "Loop context:\n"
        f"- Loop ID: {state.loop_id}\n"
        f"- Iteration: {iteration_number}\n"
        f"- Completed iterations: {state.completed_iterations}\n"
        f"- Previous summary: {state.last_summary or 'none'}\n"
        "- Continue safely from the current state."
    )
    return "\n\n".join(parts).strip() + "\n"


def summarize_output(text: str, max_lines: int = 8) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "no output"
    return " | ".join(lines[-max_lines:])[:500]
