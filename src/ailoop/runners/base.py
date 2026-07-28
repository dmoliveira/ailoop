from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class RunnerResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    stdout_log: Path
    stderr_log: Path
    timed_out: bool = False
    cancelled: bool = False


@dataclass(slots=True)
class RunnerLifecycle:
    process_group_id: int | None = None
    direct_child_reaped: bool = True
    cleanup_confirmed: bool = True


class ProcessCleanupError(RuntimeError):
    pass
