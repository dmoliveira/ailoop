from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _known_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = {item.name for item in fields(cls)}
    return {name: value for name, value in data.items() if name in names}


@dataclass(slots=True)
class RunnerConfig:
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PathsConfig:
    agent_file: str | None
    state_dir: str


@dataclass(slots=True)
class PromptConfig:
    pre_prompt_enabled: bool
    attach_agent_file: bool
    pre_prompt: str


@dataclass(slots=True)
class LoopConfig:
    steps: int | None
    pause_seconds: int
    continue_on_error: bool
    retry_count: int
    iteration_timeout_seconds: int | None = None


@dataclass(slots=True)
class TasksConfig:
    file: str | None
    stop_when_complete: bool
    max_doing: int


@dataclass(slots=True)
class AppConfig:
    default_runner: str
    default_agent: str | None
    paths: PathsConfig
    prompt: PromptConfig
    loop: LoopConfig
    tasks: TasksConfig
    runners: dict[str, RunnerConfig]


@dataclass(slots=True)
class LoopRunConfig:
    prompt: str
    runner: str
    agent: str | None
    steps: int | None
    pause_seconds: int
    continue_on_error: bool
    retry_count: int
    pre_prompt_enabled: bool
    attach_agent_file: bool
    pre_prompt: str
    agent_file: str | None
    runner_command: str
    runner_args: list[str]
    iteration_timeout_seconds: int | None = None
    runner_env: dict[str, str] = field(default_factory=dict)
    task_file: str | None = None
    stop_when_tasks_complete: bool = False
    max_doing: int = 1
    workspace_root: str | None = None
    workspace_history_enabled: bool = True
    workspace_history_limit: int = 5
    workspace_history_chars: int = 1200

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IterationRecord:
    number: int
    started_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    success: bool | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
    prompt_file: str | None = None
    summary: str | None = None
    timed_out: bool = False
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LoopState:
    loop_id: str
    created_at: str
    updated_at: str
    status: str
    control: str
    run_config: LoopRunConfig
    current_iteration: int = 0
    completed_iterations: int = 0
    last_exit_code: int | None = None
    consecutive_failures: int = 0
    total_duration_seconds: float = 0.0
    average_duration_seconds: float = 0.0
    last_summary: str | None = None
    pending_single_iteration: bool = False
    queued_follow_up: str | None = None
    queued_follow_up_token: str | None = None
    workspace_prompt_signature: str | None = None
    dashboard_config: dict[str, Any] = field(default_factory=dict)
    workspace_config: dict[str, str] = field(default_factory=dict)
    iterations: list[IterationRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["run_config"] = self.run_config.to_dict()
        payload["iterations"] = [item.to_dict() for item in self.iterations]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoopState:
        run_config = LoopRunConfig(**_known_fields(LoopRunConfig, data["run_config"]))
        iterations = [
            IterationRecord(**_known_fields(IterationRecord, item))
            for item in data.get("iterations", [])
        ]
        return cls(
            loop_id=data["loop_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            status=data["status"],
            control=data.get("control", "run"),
            run_config=run_config,
            current_iteration=data.get("current_iteration", 0),
            completed_iterations=data.get("completed_iterations", 0),
            last_exit_code=data.get("last_exit_code"),
            consecutive_failures=data.get("consecutive_failures", 0),
            total_duration_seconds=data.get("total_duration_seconds", 0.0),
            average_duration_seconds=data.get("average_duration_seconds", 0.0),
            last_summary=data.get("last_summary"),
            pending_single_iteration=data.get("pending_single_iteration", False),
            queued_follow_up=data.get("queued_follow_up"),
            queued_follow_up_token=data.get("queued_follow_up_token"),
            workspace_prompt_signature=data.get("workspace_prompt_signature"),
            dashboard_config=data.get("dashboard_config", {}),
            workspace_config=data.get("workspace_config", {}),
            iterations=iterations,
        )
