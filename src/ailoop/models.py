from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
    runner_env: dict[str, str] = field(default_factory=dict)
    task_file: str | None = None
    stop_when_tasks_complete: bool = False
    max_doing: int = 1

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
    iterations: list[IterationRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["run_config"] = self.run_config.to_dict()
        payload["iterations"] = [item.to_dict() for item in self.iterations]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoopState:
        run_config = LoopRunConfig(**data["run_config"])
        iterations = [IterationRecord(**item) for item in data.get("iterations", [])]
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
            iterations=iterations,
        )
