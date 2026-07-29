from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from ..paths import read_last_lines
from .base import ProcessCleanupError, RunnerLifecycle, RunnerResult

CAPTURE_TAIL_LINES = 80
TERMINATION_GRACE_SECONDS = 5
FINAL_KILL_GRACE_SECONDS = 1
CONTROL_POLL_SECONDS = 0.25
GROUP_POLL_SECONDS = 0.05


class LocalRunner:
    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        if os.name != "posix":
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    @staticmethod
    def _signal_process_group(process_group_id: int, signal_number: int) -> None:
        try:
            os.killpg(process_group_id, signal_number)
        except (OSError, ProcessLookupError):
            pass

    @classmethod
    def _cleanup_process_group(
        cls,
        process: subprocess.Popen[str],
        process_group_id: int,
    ) -> bool:
        if os.name != "posix":
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return process.poll() is not None

        cls._signal_process_group(process_group_id, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        while cls._process_group_exists(process_group_id) and time.monotonic() < deadline:
            if process.poll() is None:
                try:
                    process.wait(timeout=GROUP_POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
            time.sleep(GROUP_POLL_SECONDS)

        if cls._process_group_exists(process_group_id):
            cls._signal_process_group(process_group_id, signal.SIGKILL)
            deadline = time.monotonic() + FINAL_KILL_GRACE_SECONDS
            while cls._process_group_exists(process_group_id) and time.monotonic() < deadline:
                if process.poll() is None:
                    try:
                        process.wait(timeout=GROUP_POLL_SECONDS)
                    except subprocess.TimeoutExpired:
                        continue
                time.sleep(GROUP_POLL_SECONDS)

        if process.poll() is None:
            try:
                process.wait(timeout=GROUP_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        return process.poll() is not None and not cls._process_group_exists(process_group_id)

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
        lifecycle: RunnerLifecycle | None = None,
    ) -> RunnerResult:
        start = time.monotonic()
        full_env = os.environ.copy()
        full_env.update(env)
        lifecycle = lifecycle or RunnerLifecycle()

        log_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        stdout_fd = os.open(stdout_log, log_flags, 0o600)
        try:
            stderr_fd = os.open(stderr_log, log_flags, 0o600)
        except BaseException:
            os.close(stdout_fd)
            raise
        with (
            os.fdopen(stdout_fd, "w", encoding="utf-8") as stdout_handle,
            os.fdopen(stderr_fd, "w", encoding="utf-8") as stderr_handle,
        ):
            try:
                process = subprocess.Popen(
                    [command, *args],
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    env=full_env,
                    cwd=str(cwd) if cwd is not None else None,
                    start_new_session=True,
                )
            except OSError as exc:
                exit_code = 127
                timed_out = False
                cancelled = False
                stderr_handle.write(str(exc))
            else:
                process_group_id = process.pid
                lifecycle.process_group_id = process_group_id
                lifecycle.direct_child_reaped = False
                lifecycle.cleanup_confirmed = False
                timed_out = False
                cancelled = False
                exit_code: int | None = None
                deadline = (
                    time.monotonic() + timeout_seconds if timeout_seconds is not None else None
                )
                execution_error: BaseException | None = None
                try:
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
                        break
                except BaseException as exc:
                    execution_error = exc
                    raise
                finally:
                    try:
                        lifecycle.cleanup_confirmed = self._cleanup_process_group(
                            process,
                            process_group_id,
                        )
                    except BaseException:
                        lifecycle.cleanup_confirmed = False
                    lifecycle.direct_child_reaped = process.poll() is not None
                    if execution_error is not None and not lifecycle.cleanup_confirmed:
                        execution_error.add_note(
                            f"runner process group {process_group_id} cleanup was not confirmed"
                        )

                if not lifecycle.cleanup_confirmed:
                    raise ProcessCleanupError(
                        f"Runner process group cleanup was not confirmed: {process_group_id}"
                    )
                if exit_code is None:
                    exit_code = process.returncode
                if exit_code is None:
                    raise ProcessCleanupError(f"Runner child was not reaped: {process_group_id}")
                if timed_out:
                    stderr_handle.write(f"runner timed out after {timeout_seconds} seconds\n")
                elif cancelled:
                    stderr_handle.write("runner stopped by loop control\n")

        stdout = read_last_lines(stdout_log, CAPTURE_TAIL_LINES, errors="replace")
        stderr = read_last_lines(stderr_log, CAPTURE_TAIL_LINES, errors="replace")
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
