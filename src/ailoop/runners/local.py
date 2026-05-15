from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .base import RunnerResult


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
            process = subprocess.run(
                [command, *args],
                check=False,
                capture_output=True,
                text=True,
                env=full_env,
            )
            stdout = process.stdout
            stderr = process.stderr
            exit_code = process.returncode
        except OSError as exc:
            stdout = ""
            stderr = str(exc)
            exit_code = 127
        duration = time.monotonic() - start
        stdout_log.write_text(stdout)
        stderr_log.write_text(stderr)
        return RunnerResult(
            command=[command, *args],
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )
