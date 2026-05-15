from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from .models import IterationRecord, LoopRunConfig, LoopState, utc_now
from .paths import log_dir, raw_loop_dir
from .prompting import build_prompt, summarize_output
from .runners import LocalRunner
from .state import StateStore
from .stats import render_iteration_summary
from .tasks import parse_task_file


class LoopService:
    def __init__(self, state_root: Path, emit_output: bool = True):
        self.store = StateStore(state_root)
        self.runner = LocalRunner()
        self.state_root = state_root
        self.emit_output = emit_output

    def create_loop(self, run_config: LoopRunConfig, loop_id: str | None = None) -> LoopState:
        resolved_loop_id = loop_id or uuid.uuid4().hex[:12]
        state = LoopState(
            loop_id=resolved_loop_id,
            created_at=utc_now(),
            updated_at=utc_now(),
            status="idle",
            control="run",
            run_config=run_config,
        )
        self.store.save(state)
        self.store.append_event(state.loop_id, {"at": utc_now(), "event": "created"})
        return state

    def load_loop(self, loop_id: str) -> LoopState:
        return self.store.load(loop_id)

    def list_loops(self) -> list[LoopState]:
        return self.store.list_states()

    def loop_paths(self, loop_id: str, iteration: int | None = None) -> dict[str, Path]:
        state = self.store.load(loop_id)
        if not state.iterations and iteration is None:
            raise FileNotFoundError(f"Loop has no iterations yet: {loop_id}")

        selected_iteration = iteration or state.completed_iterations or state.current_iteration
        logs = log_dir(self.state_root, loop_id)
        prefix = logs / f"iteration-{selected_iteration:04d}"
        return {
            "state": raw_loop_dir(self.state_root, loop_id) / "state.json",
            "events": raw_loop_dir(self.state_root, loop_id) / "events.jsonl",
            "prompt": prefix.with_suffix(".prompt.txt"),
            "stdout": prefix.with_suffix(".stdout.log"),
            "stderr": prefix.with_suffix(".stderr.log"),
        }

    def remove_loop(self, loop_id: str, force: bool = False) -> None:
        state = self.store.load(loop_id)
        if self.store.is_locked(loop_id):
            raise RuntimeError(f"Refusing to remove locked/running loop: {loop_id}")
        active_statuses = {"running", "pause_requested", "stop_requested", "paused", "idle"}
        if state.status in active_statuses and not force:
            raise RuntimeError(f"Refusing to remove active loop without --force: {loop_id}")
        shutil.rmtree(raw_loop_dir(self.state_root, loop_id))

    def request_control(self, loop_id: str, control: str) -> LoopState:
        state = self.store.load(loop_id)
        state.control = control
        if control == "pause" and state.status not in {"completed", "failed", "stopped"}:
            state.status = "pause_requested"
        if control == "stop" and state.status not in {"completed", "failed", "stopped"}:
            state.status = "stop_requested"
        self.store.save(state)
        self.store.append_event(loop_id, {"at": utc_now(), "event": "control", "control": control})
        return state

    def should_continue(self, state: LoopState) -> bool:
        target = state.run_config.steps
        if state.control == "stop":
            return False
        if state.run_config.stop_when_tasks_complete and state.run_config.task_file:
            task_state = parse_task_file(
                Path(state.run_config.task_file),
                state.run_config.max_doing,
            )
            if task_state.is_complete:
                return False
        if target is None:
            return True
        return state.completed_iterations < target

    def run_loop(self, loop_id: str) -> LoopState:
        with self.store.acquire_lock(loop_id):
            state = self.store.load(loop_id)
            self._validate_task_file(state)
            state.control = "run"
            self.store.save(state)
            while self.should_continue(state):
                state = self.store.load(loop_id)
                if state.control == "pause":
                    state.status = "paused"
                    self.store.save(state)
                    self.store.append_event(loop_id, {"at": utc_now(), "event": "paused"})
                    return state
                if state.control == "stop":
                    state.status = "stopped"
                    self.store.save(state)
                    self.store.append_event(loop_id, {"at": utc_now(), "event": "stopped"})
                    return state
                state = self._run_iteration(state)
                state = self.store.load(loop_id)
                if state.control == "pause":
                    state.status = "paused"
                    self.store.save(state)
                    self.store.append_event(loop_id, {"at": utc_now(), "event": "paused"})
                    return state
                if state.control == "stop":
                    state.status = "stopped"
                    self.store.save(state)
                    self.store.append_event(loop_id, {"at": utc_now(), "event": "stopped"})
                    return state
                if state.last_exit_code not in (0, None) and not state.run_config.continue_on_error:
                    state.status = "failed"
                    self.store.save(state)
                    return state
                if self.should_continue(state):
                    time.sleep(state.run_config.pause_seconds)
            state.status = "completed"
            if state.run_config.stop_when_tasks_complete and state.run_config.task_file:
                state.last_summary = state.last_summary or "task file complete"
            self.store.save(state)
            self.store.append_event(loop_id, {"at": utc_now(), "event": "completed"})
            return state

    def _validate_task_file(self, state: LoopState) -> None:
        if state.run_config.task_file:
            parse_task_file(Path(state.run_config.task_file), state.run_config.max_doing)

    def _run_iteration(self, state: LoopState) -> LoopState:
        iteration_number = state.completed_iterations + 1
        state.current_iteration = iteration_number
        state.status = "running"
        iteration = IterationRecord(number=iteration_number, started_at=utc_now())
        logs = log_dir(self.state_root, state.loop_id)
        prompt_path = logs / f"iteration-{iteration_number:04d}.prompt.txt"
        stdout_path = logs / f"iteration-{iteration_number:04d}.stdout.log"
        stderr_path = logs / f"iteration-{iteration_number:04d}.stderr.log"
        prompt_text = build_prompt(state, iteration_number)
        prompt_path.write_text(prompt_text)
        iteration.prompt_file = str(prompt_path)
        self.store.save(state)

        attempt = 0
        result = None
        while attempt <= state.run_config.retry_count:
            args = [
                item.format(
                    prompt=prompt_text,
                    prompt_file=str(prompt_path),
                    agent=state.run_config.agent or "",
                    loop_id=state.loop_id,
                    iteration=iteration_number,
                )
                for item in state.run_config.runner_args
            ]
            env = {
                **state.run_config.runner_env,
                "AILOOP_LOOP_ID": state.loop_id,
                "AILOOP_ITERATION": str(iteration_number),
            }
            result = self.runner.run(
                command=state.run_config.runner_command,
                args=args,
                env=env,
                stdout_log=stdout_path,
                stderr_log=stderr_path,
            )
            if result.exit_code == 0:
                break
            attempt += 1

        assert result is not None
        iteration.finished_at = utc_now()
        iteration.duration_seconds = result.duration_seconds
        iteration.exit_code = result.exit_code
        iteration.success = result.exit_code == 0
        iteration.stdout_log = str(result.stdout_log)
        iteration.stderr_log = str(result.stderr_log)
        iteration.summary = summarize_output(result.stdout or result.stderr)

        latest_state = self.store.load(state.loop_id)
        state.control = latest_state.control

        state.iterations.append(iteration)
        state.completed_iterations += 1
        state.current_iteration = iteration_number
        state.last_exit_code = result.exit_code
        state.total_duration_seconds += result.duration_seconds
        state.average_duration_seconds = state.total_duration_seconds / state.completed_iterations
        state.last_summary = iteration.summary
        state.consecutive_failures = 0 if iteration.success else state.consecutive_failures + 1
        state.status = "running"
        self.store.save(state)
        self.store.append_event(
            state.loop_id,
            {
                "at": utc_now(),
                "event": "iteration_completed",
                "iteration": iteration_number,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            },
        )
        if self.emit_output:
            print(render_iteration_summary(state), flush=True)
        return state
