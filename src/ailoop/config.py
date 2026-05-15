from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .models import (
    AppConfig,
    LoopConfig,
    LoopRunConfig,
    PathsConfig,
    PromptConfig,
    RunnerConfig,
    TasksConfig,
)
from .paths import expand_path

DEFAULT_CONFIG: dict[str, Any] = {
    "default_runner": "opencode",
    "default_agent": "orchestrator",
    "paths": {
        "agent_file": "~/Codes/Projects/agents_md/AGENTS.md",
        "state_dir": "~/.config/ailoop/state",
    },
    "prompt": {
        "pre_prompt_enabled": True,
        "attach_agent_file": True,
        "pre_prompt": (
            "Work in small validated slices.\n"
            "Review current context before starting new work.\n"
            "Leave concise progress, blockers, and next action at the end."
        ),
    },
    "loop": {
        "steps": None,
        "pause_seconds": 30,
        "continue_on_error": True,
        "retry_count": 0,
    },
    "tasks": {
        "file": None,
        "stop_when_complete": False,
        "max_doing": 1,
    },
    "runners": {
        "opencode": {
            "command": "opencode",
            "args": ["run", "--agent", "{agent}", "{prompt}"],
            "env": {},
        },
        "codex": {
            "command": "codex",
            "args": ["{prompt}"],
            "env": {},
        },
        "claude": {
            "command": "claude",
            "args": ["{prompt}"],
            "env": {},
        },
    },
}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return data


def default_config_path() -> Path:
    return Path("~/.config/ailoop/config.yaml").expanduser()


def load_app_config(config_path: Path | None = None) -> AppConfig:
    path = config_path or default_config_path()
    merged = deep_merge(DEFAULT_CONFIG, load_yaml_file(path))
    return build_app_config(merged)


def build_app_config(data: dict[str, Any]) -> AppConfig:
    runners = {
        name: RunnerConfig(
            command=runner["command"],
            args=list(runner.get("args", [])),
            env=dict(runner.get("env", {})),
        )
        for name, runner in data["runners"].items()
    }
    return AppConfig(
        default_runner=data["default_runner"],
        default_agent=data.get("default_agent"),
        paths=PathsConfig(
            agent_file=str(expand_path(data["paths"].get("agent_file")))
            if data["paths"].get("agent_file")
            else None,
            state_dir=str(expand_path(data["paths"]["state_dir"])),
        ),
        prompt=PromptConfig(
            pre_prompt_enabled=bool(data["prompt"]["pre_prompt_enabled"]),
            attach_agent_file=bool(data["prompt"]["attach_agent_file"]),
            pre_prompt=str(data["prompt"]["pre_prompt"]),
        ),
        loop=LoopConfig(
            steps=data["loop"].get("steps"),
            pause_seconds=int(data["loop"]["pause_seconds"]),
            continue_on_error=bool(data["loop"]["continue_on_error"]),
            retry_count=int(data["loop"]["retry_count"]),
        ),
        tasks=TasksConfig(
            file=str(expand_path(data.get("tasks", {}).get("file")))
            if data.get("tasks", {}).get("file")
            else None,
            stop_when_complete=bool(data.get("tasks", {}).get("stop_when_complete", False)),
            max_doing=int(data.get("tasks", {}).get("max_doing", 1)),
        ),
        runners=runners,
    )


def resolve_run_config(
    app_config: AppConfig,
    prompt: str,
    runner: str | None = None,
    agent: str | None = None,
    steps: int | None = None,
    pause_seconds: int | None = None,
    pre_prompt_enabled: bool | None = None,
    attach_agent_file: bool | None = None,
    agent_file: str | None = None,
    task_file: str | None = None,
    stop_when_tasks_complete: bool | None = None,
) -> LoopRunConfig:
    selected_runner = runner or app_config.default_runner
    if selected_runner not in app_config.runners:
        raise ValueError(f"Unknown runner: {selected_runner}")
    runner_config = app_config.runners[selected_runner]
    return LoopRunConfig(
        prompt=prompt,
        runner=selected_runner,
        agent=agent if agent is not None else app_config.default_agent,
        steps=steps if steps is not None else app_config.loop.steps,
        pause_seconds=pause_seconds if pause_seconds is not None else app_config.loop.pause_seconds,
        continue_on_error=app_config.loop.continue_on_error,
        retry_count=app_config.loop.retry_count,
        pre_prompt_enabled=(
            pre_prompt_enabled
            if pre_prompt_enabled is not None
            else app_config.prompt.pre_prompt_enabled
        ),
        attach_agent_file=(
            attach_agent_file
            if attach_agent_file is not None
            else app_config.prompt.attach_agent_file
        ),
        pre_prompt=app_config.prompt.pre_prompt,
        agent_file=agent_file if agent_file is not None else app_config.paths.agent_file,
        task_file=(
            str(expand_path(task_file))
            if task_file is not None
            else app_config.tasks.file
        ),
        stop_when_tasks_complete=(
            stop_when_tasks_complete
            if stop_when_tasks_complete is not None
            else app_config.tasks.stop_when_complete
        ),
        max_doing=app_config.tasks.max_doing,
        runner_command=runner_config.command,
        runner_args=runner_config.args,
        runner_env=runner_config.env,
    )


def init_config_text() -> str:
    return yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False)
