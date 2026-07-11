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
from .workspace_history import (
    WorkspaceHistoryStore,
    canonical_workspace_root,
    workspace_prompt_signature,
)


class LoopService:
    def __init__(self, state_root: Path, emit_output: bool = True):
        self.store = StateStore(state_root)
        self.runner = LocalRunner()
        self.state_root = state_root
        self.emit_output = emit_output
        self.workspace_history = WorkspaceHistoryStore(state_root)

    def _normalize_workspace_root(self, workspace_root: str | None) -> str | None:
        normalized = canonical_workspace_root(workspace_root)
        if normalized is None:
            return None
        path = Path(normalized)
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"Workspace root not found: {normalized}")
        return normalized

    def create_loop(self, run_config: LoopRunConfig, loop_id: str | None = None) -> LoopState:
        run_config.workspace_root = self._normalize_workspace_root(run_config.workspace_root)
        resolved_loop_id = loop_id or uuid.uuid4().hex[:12]
        if raw_loop_dir(self.state_root, resolved_loop_id).exists():
            raise RuntimeError(f"Loop already exists: {resolved_loop_id}")
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
        self._record_workspace_prompt_if_changed(state)
        return state

    def _record_workspace_prompt_if_changed(self, state: LoopState) -> None:
        signature = workspace_prompt_signature(
            state.run_config.workspace_root,
            state.run_config.prompt,
        )
        if not state.run_config.workspace_history_enabled or signature is None:
            return
        if signature == state.workspace_prompt_signature:
            return
        if state.workspace_prompt_signature is None:
            latest_prompt = self.workspace_history.latest_prompt(state.run_config.workspace_root)
            if latest_prompt == state.run_config.prompt.strip():
                state.workspace_prompt_signature = signature
                self.store.save(state)
                return
        self.workspace_history.append_prompt(state.loop_id, state.run_config)
        state.workspace_prompt_signature = signature
        self.store.save(state)

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
        if control not in {"run", "pause", "stop"}:
            raise ValueError(f"Invalid control: {control}")
        state = self.store.load(loop_id)
        state.control = control
        if control == "pause" and state.status not in {"completed", "failed", "stopped"}:
            state.status = "pause_requested"
        if control == "stop" and state.status not in {"completed", "failed", "stopped"}:
            state.status = "stop_requested"
        self.store.save(state)
        self.store.append_event(loop_id, {"at": utc_now(), "event": "control", "control": control})
        return state

    def queue_follow_up(self, loop_id: str, follow_up: str, *, run_next: bool = False) -> LoopState:
        cleaned = follow_up.strip()
        if not cleaned:
            raise ValueError("Follow-up prompt is empty")
        with self.store.acquire_mutation_lock(loop_id):
            state = self.store.load(loop_id)
            if state.pending_single_iteration:
                raise RuntimeError(f"Loop already has a pending iteration: {loop_id}")
            state.queued_follow_up = cleaned
            state.queued_follow_up_token = uuid.uuid4().hex
            started_next = False
            if run_next and not self.store.is_locked(loop_id):
                if not self.should_continue(state):
                    raise RuntimeError(f"Loop has no pending iterations: {loop_id}")
                state.control = "run"
                state.pending_single_iteration = True
                started_next = True
            self.store.save(state)
        self.store.append_event(
            loop_id,
            {
                "at": utc_now(),
                "event": "follow_up_queued",
                "run_next": started_next,
            },
        )
        return state

    def clear_follow_up(self, loop_id: str) -> LoopState:
        state = self.store.load(loop_id)
        if self.store.is_locked(loop_id) or state.pending_single_iteration:
            raise RuntimeError("Cannot clear a follow-up while an iteration is active")
        state.queued_follow_up = None
        state.queued_follow_up_token = None
        self.store.save(state)
        self.store.append_event(loop_id, {"at": utc_now(), "event": "follow_up_cleared"})
        return state

    def request_single_iteration(self, loop_id: str) -> LoopState:
        with self.store.acquire_mutation_lock(loop_id):
            if self.store.is_locked(loop_id):
                raise RuntimeError(f"Loop is already active: {loop_id}")
            state = self.store.load(loop_id)
            if state.pending_single_iteration:
                raise RuntimeError(f"Loop already has a pending iteration: {loop_id}")
            if state.status in {"running", "pause_requested", "stop_requested"}:
                raise RuntimeError(f"Loop is already active: {loop_id}")
            if not self.should_continue(state):
                raise RuntimeError(f"Loop has no pending iterations: {loop_id}")
            state.control = "run"
            state.pending_single_iteration = True
            self.store.save(state)
        self.store.append_event(loop_id, {"at": utc_now(), "event": "single_iteration_requested"})
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
            state.run_config.workspace_root = self._normalize_workspace_root(
                state.run_config.workspace_root
            )
            self._record_workspace_prompt_if_changed(state)
            self._validate_task_file(state)
            if state.control == "stop":
                state.status = "stopped"
                state.updated_at = utc_now()
                self.store.save(state)
                self.store.append_event(loop_id, {"at": utc_now(), "event": "stopped"})
                return state
            if state.control == "pause":
                state.status = "paused"
                state.updated_at = utc_now()
                self.store.save(state)
                self.store.append_event(loop_id, {"at": utc_now(), "event": "paused"})
                return state
            if getattr(state, "dashboard_config", {}).get("mode") == "scheduled":
                state.status = "idle"
                state.updated_at = utc_now()
                self.store.save(state)
                self.store.append_event(loop_id, {"at": utc_now(), "event": "scheduled_waiting"})
                return state
            if state.control not in {"pause", "stop"}:
                state.control = "run"
            self.store.save(state)
            while self.should_continue(state) or state.pending_single_iteration:
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
                single_iteration_requested = state.pending_single_iteration
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
                if state.pending_single_iteration:
                    state.pending_single_iteration = False
                    self.store.save(state)
                if state.last_exit_code not in (0, None) and not state.run_config.continue_on_error:
                    state.status = "failed"
                    self.store.save(state)
                    return state
                if single_iteration_requested and self.should_continue(state):
                    state.status = "paused"
                    self.store.save(state)
                    self.store.append_event(
                        loop_id,
                        {
                            "at": utc_now(),
                            "event": "single_iteration_completed",
                            "iteration": state.current_iteration,
                        },
                    )
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
        consumed_follow_up = state.queued_follow_up
        consumed_follow_up_token = state.queued_follow_up_token
        recent_workspace_history = (
            self.workspace_history.recent_entries(
                state.run_config.workspace_root,
                limit=state.run_config.workspace_history_limit,
                max_chars=state.run_config.workspace_history_chars,
            )
            if state.run_config.workspace_history_enabled
            else []
        )
        prompt_text = build_prompt(
            state,
            iteration_number,
            recent_workspace_history=recent_workspace_history,
        )
        prompt_path.write_text(prompt_text)
        iteration.prompt_file = str(prompt_path)
        if state.run_config.workspace_history_enabled and consumed_follow_up:
            self.workspace_history.append_follow_up(
                state.run_config.workspace_root,
                state.loop_id,
                consumed_follow_up,
            )

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
                cwd=(
                    Path(state.run_config.workspace_root)
                    if state.run_config.workspace_root
                    else None
                ),
                timeout_seconds=state.run_config.iteration_timeout_seconds,
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
        iteration.timed_out = result.timed_out

        latest_state = self.store.load(state.loop_id)
        state.control = latest_state.control
        latest_follow_up = latest_state.queued_follow_up
        latest_follow_up_token = latest_state.queued_follow_up_token

        state.iterations.append(iteration)
        state.completed_iterations += 1
        state.current_iteration = iteration_number
        state.last_exit_code = result.exit_code
        state.total_duration_seconds += result.duration_seconds
        state.average_duration_seconds = state.total_duration_seconds / state.completed_iterations
        state.last_summary = iteration.summary
        state.consecutive_failures = 0 if iteration.success else state.consecutive_failures + 1
        state.status = (
            latest_state.status
            if latest_state.status in {"pause_requested", "stop_requested"}
            else "running"
        )
        state.queued_follow_up = latest_follow_up
        state.queued_follow_up_token = latest_follow_up_token
        if state.queued_follow_up_token == consumed_follow_up_token:
            state.queued_follow_up = None
            state.queued_follow_up_token = None
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
        if state.run_config.workspace_history_enabled:
            self.workspace_history.append_result(
                state.run_config.workspace_root,
                state.loop_id,
                iteration,
            )
        if self.emit_output:
            print(render_iteration_summary(state), flush=True)
        return state
