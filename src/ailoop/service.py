from __future__ import annotations

import shutil
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import IterationRecord, LoopRunConfig, LoopState, utc_now
from .paths import ensure_dir, log_dir, raw_loop_dir, trash_dir, validate_loop_id
from .prompting import build_prompt, summarize_output
from .runners import LocalRunner, RunnerLifecycle
from .state import StateStore
from .stats import render_iteration_summary
from .tasks import parse_task_file
from .workspace_history import (
    WorkspaceHistoryStore,
    canonical_workspace_root,
    workspace_prompt_signature,
)

CONTROL_POLL_SECONDS = 0.25
CLAIMED_STATUSES = {"running", "pause_requested", "stop_requested"}
FAIL_CLOSED_STATUSES = CLAIMED_STATUSES | {"cleanup_failed"}


@dataclass(slots=True)
class IterationClaim:
    state: LoopState
    number: int
    single_iteration_requested: bool
    consumed_follow_up: str | None
    consumed_follow_up_token: str | None


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

    @staticmethod
    def _is_scheduled(state: LoopState) -> bool:
        return state.dashboard_config.get("mode") == "scheduled"

    def create_loop(self, run_config: LoopRunConfig, loop_id: str | None = None) -> LoopState:
        run_config.workspace_root = self._normalize_workspace_root(run_config.workspace_root)
        resolved_loop_id = validate_loop_id(loop_id or uuid.uuid4().hex[:12])
        state = LoopState(
            loop_id=resolved_loop_id,
            created_at=utc_now(),
            updated_at=utc_now(),
            status="idle",
            control="run",
            run_config=run_config,
        )
        with self.store.acquire_lock(resolved_loop_id):
            self.store.create_if_absent(state)
            self.store.append_event(state.loop_id, {"at": utc_now(), "event": "created"})
            with self.store.mutate(state.loop_id) as created:
                self._record_workspace_prompt_if_changed(created)
                state = created
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
                return
        self.workspace_history.append_prompt(state.loop_id, state.run_config)
        state.workspace_prompt_signature = signature

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
        directory = raw_loop_dir(self.state_root, loop_id)
        return {
            "state": directory / "state.json",
            "events": directory / "events.jsonl",
            "prompt": prefix.with_suffix(".prompt.txt"),
            "stdout": prefix.with_suffix(".stdout.log"),
            "stderr": prefix.with_suffix(".stderr.log"),
        }

    def remove_loop(self, loop_id: str, force: bool = False) -> None:
        validate_loop_id(loop_id)
        with self.store.acquire_lock(loop_id):
            with self.store.acquire_mutation_lock(loop_id):
                retirement = self.store.retirement_record(loop_id)
                directory = raw_loop_dir(self.state_root, loop_id)
                if retirement is None:
                    state = self.store.load(loop_id)
                    if state.status in FAIL_CLOSED_STATUSES:
                        raise RuntimeError(f"Refusing to remove loop marked active: {loop_id}")
                    if state.status in {"paused", "idle"} and not force:
                        raise RuntimeError(
                            f"Refusing to remove active loop without --force: {loop_id}"
                        )
                    tombstone_name = f"{loop_id}-{uuid.uuid4().hex}"
                    self.store.reserve_retirement(loop_id, tombstone_name)
                else:
                    tombstone_name = retirement["tombstone"]
                    if not tombstone_name or Path(tombstone_name).name != tombstone_name:
                        raise RuntimeError(f"Invalid retirement record for loop: {loop_id}")

                tombstone_root = ensure_dir(trash_dir(self.state_root))
                tombstone = tombstone_root / tombstone_name
                if directory.exists():
                    directory.rename(tombstone)

            if tombstone.exists():
                shutil.rmtree(tombstone)

    def request_control(self, loop_id: str, control: str) -> LoopState:
        if control not in {"run", "pause", "stop"}:
            raise ValueError(f"Invalid control: {control}")
        if control == "run":
            raise ValueError("Use resume to request the run control")
        with self.store.mutate(loop_id) as state:
            if state.status == "cleanup_failed":
                raise RuntimeError(f"Loop process cleanup was not confirmed: {loop_id}")
            state.control = control
            if control == "pause":
                if state.status == "running":
                    state.status = "pause_requested"
                elif state.status == "idle":
                    state.status = "paused"
            elif state.status in {"running", "pause_requested", "stop_requested"}:
                state.status = "stop_requested"
            elif state.status not in {"completed", "failed"}:
                state.status = "stopped"
        self.store.append_event(loop_id, {"at": utc_now(), "event": "control", "control": control})
        return state

    def _prepare_resume(self, state: LoopState) -> None:
        if self._is_scheduled(state):
            raise RuntimeError(f"Scheduled loops cannot be resumed manually: {state.loop_id}")
        if state.status in FAIL_CLOSED_STATUSES:
            raise RuntimeError(
                f"Loop is marked active and cannot be resumed safely: {state.loop_id}"
            )
        if not (self.should_continue(state) or state.pending_single_iteration):
            raise RuntimeError(f"Loop has no pending iterations: {state.loop_id}")
        state.control = "run"
        state.status = "idle"

    def request_resume(self, loop_id: str) -> LoopState:
        with self.store.acquire_lock(loop_id):
            with self.store.mutate(loop_id) as state:
                self._prepare_resume(state)
            self.store.append_event(loop_id, {"at": utc_now(), "event": "resume_requested"})
        return state

    def queue_follow_up(self, loop_id: str, follow_up: str, *, run_next: bool = False) -> LoopState:
        cleaned = follow_up.strip()
        if not cleaned:
            raise ValueError("Follow-up prompt is empty")
        execution_context = self.store.acquire_lock(loop_id) if run_next else nullcontext()
        with execution_context:
            with self.store.mutate(loop_id) as state:
                if state.pending_single_iteration:
                    raise RuntimeError(f"Loop already has a pending iteration: {loop_id}")
                if run_next:
                    if self._is_scheduled(state):
                        raise RuntimeError(
                            f"Scheduled loops cannot run follow-ups manually: {loop_id}"
                        )
                    if state.status in FAIL_CLOSED_STATUSES:
                        raise RuntimeError(f"Loop is already active: {loop_id}")
                    if not self.should_continue(state):
                        raise RuntimeError(f"Loop has no pending iterations: {loop_id}")
                state.queued_follow_up = cleaned
                state.queued_follow_up_token = uuid.uuid4().hex
                if run_next:
                    state.control = "run"
                    state.status = "idle"
                    state.pending_single_iteration = True
        self.store.append_event(
            loop_id,
            {"at": utc_now(), "event": "follow_up_queued", "run_next": run_next},
        )
        return state

    def clear_follow_up(self, loop_id: str) -> LoopState:
        with self.store.acquire_lock(loop_id):
            with self.store.mutate(loop_id) as state:
                if state.status in FAIL_CLOSED_STATUSES or state.pending_single_iteration:
                    raise RuntimeError("Cannot clear a follow-up while an iteration is active")
                state.queued_follow_up = None
                state.queued_follow_up_token = None
        self.store.append_event(loop_id, {"at": utc_now(), "event": "follow_up_cleared"})
        return state

    def request_single_iteration(self, loop_id: str) -> LoopState:
        with self.store.acquire_lock(loop_id):
            with self.store.mutate(loop_id) as state:
                if self._is_scheduled(state):
                    raise RuntimeError(
                        f"Scheduled loops cannot run single iterations manually: {loop_id}"
                    )
                if state.pending_single_iteration:
                    raise RuntimeError(f"Loop already has a pending iteration: {loop_id}")
                if state.status in FAIL_CLOSED_STATUSES:
                    raise RuntimeError(f"Loop is already active: {loop_id}")
                if not self.should_continue(state):
                    raise RuntimeError(f"Loop has no pending iterations: {loop_id}")
                state.control = "run"
                state.status = "idle"
                state.pending_single_iteration = True
        self.store.append_event(
            loop_id,
            {"at": utc_now(), "event": "single_iteration_requested"},
        )
        return state

    def update_loop_config(
        self,
        loop_id: str,
        run_config: LoopRunConfig,
        dashboard_config: dict[str, Any],
        workspace_config: dict[str, str],
    ) -> LoopState:
        with self.store.mutate(loop_id) as state:
            if state.status in FAIL_CLOSED_STATUSES:
                raise RuntimeError("Stop or pause the loop before saving config changes")
            state.run_config = run_config
            state.dashboard_config = dashboard_config
            state.workspace_config = workspace_config
            if self._is_scheduled(state):
                state.status = "idle"
        return state

    def request_restart(
        self,
        loop_id: str,
        run_config: LoopRunConfig,
        dashboard_config: dict[str, Any],
        workspace_config: dict[str, str],
        *,
        reset: bool = False,
    ) -> LoopState:
        with self.store.acquire_lock(loop_id):
            with self.store.mutate(loop_id) as state:
                if self._is_scheduled(state):
                    raise RuntimeError(f"Scheduled loops cannot be restarted manually: {loop_id}")
                if state.status in FAIL_CLOSED_STATUSES:
                    raise RuntimeError(f"Loop is already active: {loop_id}")
                state.run_config = run_config
                state.dashboard_config = dashboard_config
                state.workspace_config = workspace_config
                state.control = "run"
                state.pending_single_iteration = False
                state.status = "idle"
                if reset:
                    state.current_iteration = 0
                    state.completed_iterations = 0
                    state.last_exit_code = None
                    state.consecutive_failures = 0
                    state.total_duration_seconds = 0.0
                    state.average_duration_seconds = 0.0
                    state.last_summary = None
                    state.iterations = []
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
            return self._run_loop_locked(loop_id)

    def resume_loop(self, loop_id: str) -> LoopState:
        with self.store.acquire_lock(loop_id):
            with self.store.mutate(loop_id) as state:
                self._prepare_resume(state)
            self.store.append_event(loop_id, {"at": utc_now(), "event": "resumed"})
            return self._run_loop_locked(loop_id)

    def _run_loop_locked(self, loop_id: str) -> LoopState:
        admitted = False
        startup_event: str | None = None
        with self.store.mutate(loop_id) as state:
            if state.status in FAIL_CLOSED_STATUSES:
                raise RuntimeError(f"Loop is marked active and cannot be started safely: {loop_id}")
            state.run_config.workspace_root = self._normalize_workspace_root(
                state.run_config.workspace_root
            )
            self._record_workspace_prompt_if_changed(state)
            self._validate_task_file(state)
            if state.control == "stop":
                state.status = "stopped"
                startup_event = "stopped"
            elif state.control == "pause":
                state.status = "paused"
                startup_event = "paused"
            elif self._is_scheduled(state):
                state.status = "idle"
                startup_event = "scheduled_waiting"
            elif state.status != "idle":
                raise RuntimeError(f"Loop has no committed run intent: {loop_id}")
            elif not (self.should_continue(state) or state.pending_single_iteration):
                state.status = "completed"
                startup_event = "completed"
            else:
                state.control = "run"
                admitted = True

        if not admitted:
            if startup_event is not None:
                self.store.append_event(loop_id, {"at": utc_now(), "event": startup_event})
            return state

        while True:
            claim, terminal_state, terminal_event = self._claim_or_finish(loop_id)
            if claim is None:
                if terminal_event is not None:
                    self.store.append_event(
                        loop_id,
                        {"at": utc_now(), "event": terminal_event},
                    )
                assert terminal_state is not None
                return terminal_state

            lifecycle = RunnerLifecycle()
            finalized = False
            try:
                iteration = self._execute_iteration(claim, lifecycle)
                state, events, deferred_error = self._finalize_iteration(claim, iteration)
                finalized = True
            except BaseException as exc:
                if not finalized:
                    self._abort_iteration(claim, lifecycle, exc)
                raise

            try:
                self._record_finalized_iteration(claim, iteration, state, events)
                if deferred_error is not None:
                    raise deferred_error
            except BaseException as exc:
                self._fail_after_finalized_iteration(claim, exc)
                raise
            if state.status != "running":
                return state
            if self._wait_between_iterations(state):
                continue

    def _claim_or_finish(
        self,
        loop_id: str,
    ) -> tuple[IterationClaim | None, LoopState | None, str | None]:
        claim: IterationClaim | None = None
        terminal_event: str | None = None
        with self.store.mutate(loop_id) as state:
            if state.control == "stop":
                state.status = "stopped"
                terminal_event = "stopped"
            elif state.control == "pause":
                state.status = "paused"
                terminal_event = "paused"
            elif not (self.should_continue(state) or state.pending_single_iteration):
                state.status = "completed"
                if state.run_config.stop_when_tasks_complete and state.run_config.task_file:
                    state.last_summary = state.last_summary or "task file complete"
                terminal_event = "completed"
            else:
                iteration_number = state.completed_iterations + 1
                state.current_iteration = iteration_number
                state.status = "running"
                claim = IterationClaim(
                    state=state,
                    number=iteration_number,
                    single_iteration_requested=state.pending_single_iteration,
                    consumed_follow_up=state.queued_follow_up,
                    consumed_follow_up_token=state.queued_follow_up_token,
                )
        if claim is not None:
            return claim, None, None
        return None, state, terminal_event

    def _wait_between_iterations(self, state: LoopState) -> bool:
        """Wait between iterations while promptly observing pause/stop requests."""
        deadline = time.monotonic() + state.run_config.pause_seconds
        while True:
            latest_state = self.store.load(state.loop_id)
            if latest_state.control in {"pause", "stop"}:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(CONTROL_POLL_SECONDS, remaining))

    def _validate_task_file(self, state: LoopState) -> None:
        if state.run_config.task_file:
            parse_task_file(Path(state.run_config.task_file), state.run_config.max_doing)

    def _execute_iteration(
        self,
        claim: IterationClaim,
        lifecycle: RunnerLifecycle,
    ) -> IterationRecord:
        state = claim.state
        iteration_number = claim.number
        iteration = IterationRecord(number=iteration_number, started_at=utc_now())
        logs = ensure_dir(log_dir(self.state_root, state.loop_id))
        prompt_path = logs / f"iteration-{iteration_number:04d}.prompt.txt"
        stdout_path = logs / f"iteration-{iteration_number:04d}.stdout.log"
        stderr_path = logs / f"iteration-{iteration_number:04d}.stderr.log"
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
        prompt_path.write_text(prompt_text, encoding="utf-8")
        iteration.prompt_file = str(prompt_path)

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
            lifecycle.process_group_id = None
            lifecycle.direct_child_reaped = True
            lifecycle.cleanup_confirmed = True
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
                should_stop=lambda: self.store.load(state.loop_id).control == "stop",
                lifecycle=lifecycle,
            )
            if result.exit_code == 0 or result.timed_out or result.cancelled:
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
        iteration.cancelled = result.cancelled
        return iteration

    def _finalize_iteration(
        self,
        claim: IterationClaim,
        iteration: IterationRecord,
    ) -> tuple[LoopState, list[dict[str, Any]], BaseException | None]:
        deferred_error: BaseException | None = None
        events: list[dict[str, Any]] = [
            {
                "at": utc_now(),
                "event": "iteration_completed",
                "iteration": claim.number,
                "exit_code": iteration.exit_code,
                "duration_seconds": iteration.duration_seconds,
            }
        ]
        with self.store.mutate(claim.state.loop_id) as state:
            state.iterations.append(iteration)
            state.completed_iterations += 1
            state.current_iteration = claim.number
            state.last_exit_code = iteration.exit_code
            duration = iteration.duration_seconds or 0.0
            state.total_duration_seconds += duration
            state.average_duration_seconds = (
                state.total_duration_seconds / state.completed_iterations
            )
            state.last_summary = iteration.summary
            state.consecutive_failures = 0 if iteration.success else state.consecutive_failures + 1
            state.pending_single_iteration = False
            if state.queued_follow_up_token == claim.consumed_follow_up_token:
                state.queued_follow_up = None
                state.queued_follow_up_token = None

            if state.control == "stop":
                state.status = "stopped"
                events.append({"at": utc_now(), "event": "stopped"})
            elif state.control == "pause":
                state.status = "paused"
                events.append({"at": utc_now(), "event": "paused"})
            elif iteration.exit_code not in (0, None) and not state.run_config.continue_on_error:
                state.status = "failed"
            else:
                try:
                    continue_running = self.should_continue(state)
                except BaseException as exc:
                    deferred_error = exc
                    state.status = "failed"
                else:
                    if claim.single_iteration_requested and continue_running:
                        state.status = "paused"
                        events.append(
                            {
                                "at": utc_now(),
                                "event": "single_iteration_completed",
                                "iteration": claim.number,
                            }
                        )
                    elif not continue_running:
                        state.status = "completed"
                        if state.run_config.stop_when_tasks_complete and state.run_config.task_file:
                            state.last_summary = state.last_summary or "task file complete"
                        events.append({"at": utc_now(), "event": "completed"})
                    else:
                        state.status = "running"
        return state, events, deferred_error

    def _abort_iteration(
        self,
        claim: IterationClaim,
        lifecycle: RunnerLifecycle,
        error: BaseException,
    ) -> None:
        event_name = "iteration_aborted"
        try:
            with self.store.mutate(claim.state.loop_id) as state:
                if not lifecycle.cleanup_confirmed or not lifecycle.direct_child_reaped:
                    state.status = "cleanup_failed"
                    event_name = "cleanup_failed"
                elif state.control == "stop":
                    state.status = "stopped"
                elif state.control == "pause":
                    state.status = "paused"
                else:
                    state.status = "failed"
        except BaseException as recovery_error:
            error.add_note(f"failed to persist iteration abort state: {recovery_error}")
            return
        try:
            self.store.append_event(
                claim.state.loop_id,
                {
                    "at": utc_now(),
                    "event": event_name,
                    "iteration": claim.number,
                    "error": type(error).__name__,
                },
            )
        except BaseException as event_error:
            error.add_note(f"failed to record {event_name}: {event_error}")

    def _record_finalized_iteration(
        self,
        claim: IterationClaim,
        iteration: IterationRecord,
        state: LoopState,
        events: list[dict[str, Any]],
    ) -> None:
        for event in events:
            self.store.append_event(state.loop_id, event)
        if state.run_config.workspace_history_enabled:
            if claim.consumed_follow_up:
                self.workspace_history.append_follow_up(
                    state.run_config.workspace_root,
                    state.loop_id,
                    claim.consumed_follow_up,
                )
            self.workspace_history.append_result(
                state.run_config.workspace_root,
                state.loop_id,
                iteration,
            )
        if self.emit_output:
            print(render_iteration_summary(state), flush=True)

    def _fail_after_finalized_iteration(
        self,
        claim: IterationClaim,
        error: BaseException,
    ) -> None:
        """Leave finalized accounting intact while clearing an orphaned active status."""
        try:
            with self.store.mutate(claim.state.loop_id) as state:
                if state.completed_iterations == claim.number and state.status in CLAIMED_STATUSES:
                    if state.control == "stop":
                        state.status = "stopped"
                    elif state.control == "pause":
                        state.status = "paused"
                    else:
                        state.status = "failed"
        except BaseException as recovery_error:
            error.add_note(f"failed to persist post-finalization failure: {recovery_error}")
            return
        try:
            self.store.append_event(
                claim.state.loop_id,
                {
                    "at": utc_now(),
                    "event": "post_finalize_failed",
                    "iteration": claim.number,
                    "error": type(error).__name__,
                },
            )
        except BaseException as event_error:
            error.add_note(f"failed to record post-finalization failure: {event_error}")
