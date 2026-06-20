from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from ..paths import read_last_lines
from .base import RunnerResult

CAPTURE_TAIL_LINES = 80


class LocalRunner:
    def run(
        self,
        *,
        command: str,
        args: list[str],
        env: dict[str, str],
        stdout_log: Path,
        stderr_log: Path,
    ) -> RunnerResult:
        start = time.monotonic()
        full_env = os.environ.copy()
        full_env.update(env)
        try:
            with stdout_log.open("w") as stdout_handle, stderr_log.open("w") as stderr_handle:
                process = subprocess.Popen(
                    [command, *args],
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    env=full_env,
                )
                exit_code = process.wait()
            # Keep log files as the full durable record and only load a bounded tail
            # back into memory for summaries/status output after the child exits.
            stdout = read_last_lines(stdout_log, CAPTURE_TAIL_LINES)
            stderr = read_last_lines(stderr_log, CAPTURE_TAIL_LINES)
        except OSError as exc:
            stdout = ""
            stderr = str(exc)
            exit_code = 127
            stdout_log.write_text(stdout)
            stderr_log.write_text(stderr)
        duration = time.monotonic() - start
        return RunnerResult(
            command=[command, *args],
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )
