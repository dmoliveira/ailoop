from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from ..paths import read_last_lines
from .base import RunnerResult

CAPTURE_TAIL_LINES = 80
TERMINATION_GRACE_SECONDS = 5
CONTROL_POLL_SECONDS = 0.25


class LocalRunner:
    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

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
        should_stop: Callable[[], bool] | None = None,
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
                cancelled = False
                deadline = (
                    time.monotonic() + timeout_seconds if timeout_seconds is not None else None
                )
                while True:
                    try:
                        wait_timeout = CONTROL_POLL_SECONDS if should_stop else timeout_seconds
                        if deadline is not None:
                            wait_timeout = min(
                                wait_timeout or CONTROL_POLL_SECONDS,
                                max(0, deadline - time.monotonic()),
                            )
                        exit_code = process.wait(timeout=wait_timeout)
                        break
                    except subprocess.TimeoutExpired:
                        if should_stop and should_stop():
                            cancelled = True
                        elif deadline is not None and time.monotonic() >= deadline:
                            timed_out = True
                        else:
                            continue
                    self._terminate_process_group(process)
                    try:
                        exit_code = process.wait(timeout=TERMINATION_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        self._kill_process_group(process)
                        exit_code = process.wait()
                    if timed_out:
                        stderr_handle.write(f"runner timed out after {timeout_seconds} seconds\n")
                    else:
                        stderr_handle.write("runner stopped by loop control\n")
                    break
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
            cancelled = False
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
            cancelled=cancelled,
        )
