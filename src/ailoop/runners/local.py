from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from ..paths import read_last_lines
from .base import RunnerResult

CAPTURE_TAIL_LINES = 80
TERMINATION_GRACE_SECONDS = 5


class LocalRunner:
    def run(
        self,
        *,
        command: str,
        args: list[str],
        env: dict[str, str],
        stdout_log: Path,
        stderr_log: Path,
        cwd: Path | None = None,
        timeout_seconds: int | None = None,
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
                    cwd=str(cwd) if cwd is not None else None,
                    start_new_session=True,
                )
                timed_out = False
                try:
                    exit_code = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    try:
                        exit_code = process.wait(timeout=TERMINATION_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        exit_code = process.wait()
                    stderr_handle.write(f"runner timed out after {timeout_seconds} seconds\n")
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
            timed_out = False
        duration = time.monotonic() - start
        return RunnerResult(
            command=[command, *args],
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            timed_out=timed_out,
        )
