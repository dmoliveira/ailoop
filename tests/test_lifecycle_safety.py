import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ailoop.models import LoopRunConfig
from ailoop.runners import LocalRunner, ProcessCleanupError, RunnerLifecycle
from ailoop.service import LoopService


def make_config(
    *,
    steps: int | None = 1,
    continue_on_error: bool = True,
    runner_args: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> LoopRunConfig:
    return LoopRunConfig(
        prompt="hello",
        runner="local",
        agent=None,
        steps=steps,
        pause_seconds=0,
        continue_on_error=continue_on_error,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command=sys.executable,
        runner_args=runner_args or ["-c", "print('ok')"],
        iteration_timeout_seconds=timeout_seconds,
    )


def wait_until(predicate, timeout: float = 5) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def test_control_commit_then_removal_cannot_recreate_ghost_loop(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    service.create_loop(make_config(), loop_id="event-race")
    original_append = service.store.append_event
    append_started = threading.Event()
    release_append = threading.Event()

    def delayed_append(loop_id: str, event: dict) -> bool:
        append_started.set()
        assert release_append.wait(timeout=5)
        return original_append(loop_id, event)

    service.store.append_event = delayed_append  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.request_control, "event-race", "pause")
        assert append_started.wait(timeout=5)
        service.remove_loop("event-race", force=True)
        release_append.set()
        future.result(timeout=5)

    assert not (tmp_path / "event-race").exists()
    assert service.list_loops() == []


@pytest.mark.parametrize(
    ("control", "expected_status"),
    [("pause", "paused"), ("stop", "stopped")],
)
def test_newer_control_wins_before_detached_worker_claim(
    monkeypatch,
    tmp_path: Path,
    control: str,
    expected_status: str,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(steps=None), loop_id=f"intent-{control}")
    service.request_single_iteration(state.loop_id)
    service.request_control(state.loop_id, control)
    monkeypatch.setattr(
        service.runner,
        "run",
        lambda **_kwargs: pytest.fail("runner must not start after a newer control"),
    )

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == expected_status
    assert final_state.completed_iterations == 0
    assert final_state.pending_single_iteration is True


def test_iteration_claim_is_durable_and_finalization_preserves_new_follow_up(
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(), loop_id="durable-claim")
    original_run = service.runner.run
    observed: dict[str, object] = {}

    def run_and_queue(**kwargs):  # type: ignore[no-untyped-def]
        claimed = service.load_loop(state.loop_id)
        observed["status"] = claimed.status
        observed["current"] = claimed.current_iteration
        service.queue_follow_up(state.loop_id, "queued while running")
        return original_run(**kwargs)

    service.runner.run = run_and_queue  # type: ignore[method-assign]
    final_state = service.run_loop(state.loop_id)

    assert observed == {"status": "running", "current": 1}
    assert final_state.status == "completed"
    assert final_state.queued_follow_up == "queued while running"


def test_delayed_duplicate_worker_cannot_run_after_completion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(), loop_id="duplicate-worker")
    assert service.run_loop(state.loop_id).status == "completed"
    monkeypatch.setattr(
        service.runner,
        "run",
        lambda **_kwargs: pytest.fail("duplicate worker executed"),
    )

    with pytest.raises(RuntimeError, match="committed run intent"):
        service.run_loop(state.loop_id)


def test_delayed_duplicate_worker_cannot_run_after_single_step_pause(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(steps=None), loop_id="duplicate-single")
    service.request_single_iteration(state.loop_id)
    assert service.run_loop(state.loop_id).status == "paused"
    monkeypatch.setattr(
        service.runner,
        "run",
        lambda **_kwargs: pytest.fail("duplicate worker executed"),
    )

    with pytest.raises(RuntimeError, match="committed run intent"):
        service.run_loop(state.loop_id)


def test_explicit_resume_retries_failed_iteration(tmp_path: Path) -> None:
    script = "import os, sys; sys.exit(1 if os.environ['AILOOP_ITERATION'] == '1' else 0)"
    service = LoopService(tmp_path)
    state = service.create_loop(
        make_config(steps=2, continue_on_error=False, runner_args=["-c", script]),
        loop_id="resume-failed",
    )

    failed = service.run_loop(state.loop_id)
    resumed = service.resume_loop(state.loop_id)

    assert failed.status == "failed"
    assert failed.completed_iterations == 1
    assert resumed.status == "completed"
    assert resumed.completed_iterations == 2


def test_pre_final_exception_is_recoverable_and_keeps_single_step_intent(
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(steps=None), loop_id="abort-before-final")
    service.request_single_iteration(state.loop_id)

    def fail_runner(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("callback failed")

    service.runner.run = fail_runner  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="callback failed"):
        service.run_loop(state.loop_id)

    aborted = service.load_loop(state.loop_id)
    assert aborted.status == "failed"
    assert aborted.current_iteration == 1
    assert aborted.completed_iterations == 0
    assert aborted.pending_single_iteration is True

    service.runner = LocalRunner()
    resumed = service.resume_loop(state.loop_id)
    assert resumed.status == "paused"
    assert resumed.completed_iterations == 1
    assert resumed.pending_single_iteration is False


def test_completed_iteration_survives_task_file_failure(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.md"
    valid_tasks = (
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- [ ] Durable task\n\n## Done\n- None\n"
    )
    task_file.write_text(valid_tasks, encoding="utf-8")
    script = (
        "import os; from pathlib import Path; "
        f"p = Path(r'{task_file}'); "
        "p.write_text('# broken\\n', encoding='utf-8') "
        "if os.environ['AILOOP_ITERATION'] == '1' else None"
    )
    config = make_config(steps=2, runner_args=["-c", script])
    (tmp_path / "workspace").mkdir()
    config.task_file = str(task_file)
    config.stop_when_tasks_complete = True
    config.workspace_root = str(tmp_path / "workspace")
    service = LoopService(tmp_path)
    state = service.create_loop(config, loop_id="durable-task-accounting")
    service.queue_follow_up(state.loop_id, "consume once")
    service.request_single_iteration(state.loop_id)

    with pytest.raises(ValueError, match="Unexpected content outside task sections"):
        service.run_loop(state.loop_id)

    failed = service.load_loop(state.loop_id)
    assert failed.status == "failed"
    assert failed.completed_iterations == 1
    assert [item.number for item in failed.iterations] == [1]
    assert failed.pending_single_iteration is False
    assert failed.queued_follow_up is None
    events = (tmp_path / state.loop_id / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "iteration_aborted"' not in events
    assert events.count('"event": "iteration_completed"') == 1
    assert events.count('"event": "post_finalize_failed"') == 1
    history = service.workspace_history.recent_entries(
        config.workspace_root,
        limit=10,
        max_chars=10_000,
    )
    assert [entry.kind for entry in history] == ["prompt", "follow_up", "result"]
    assert history[-1].iteration == 1

    task_file.write_text(valid_tasks, encoding="utf-8")
    resumed = service.resume_loop(state.loop_id)

    assert resumed.status == "completed"
    assert resumed.completed_iterations == 2
    assert [item.number for item in resumed.iterations] == [1, 2]
    assert resumed.pending_single_iteration is False
    assert resumed.queued_follow_up is None


@pytest.mark.parametrize("control", ["stop", "pause"])
def test_control_precedes_post_run_task_file_failure(
    control: str,
    tmp_path: Path,
) -> None:
    task_file = tmp_path / "tasks.md"
    task_file.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- [ ] Durable task\n\n## Done\n- None\n",
        encoding="utf-8",
    )
    config = make_config(steps=None)
    config.task_file = str(task_file)
    config.stop_when_tasks_complete = True
    service = LoopService(tmp_path)
    state = service.create_loop(config, loop_id=f"task-file-{control}")
    original_run = service.runner.run

    def run_and_control(**kwargs):  # type: ignore[no-untyped-def]
        result = original_run(**kwargs)
        task_file.write_text("# broken\n", encoding="utf-8")
        service.request_control(state.loop_id, control)
        return result

    service.runner.run = run_and_control  # type: ignore[method-assign]
    final = service.run_loop(state.loop_id)

    assert final.status == ("stopped" if control == "stop" else "paused")
    assert final.completed_iterations == 1
    assert [item.number for item in final.iterations] == [1]


def test_noncontinuable_failure_precedes_post_run_task_file_failure(
    tmp_path: Path,
) -> None:
    task_file = tmp_path / "tasks.md"
    task_file.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- [ ] Durable task\n\n## Done\n- None\n",
        encoding="utf-8",
    )
    script = (
        "import sys; from pathlib import Path; "
        f"Path(r'{task_file}').write_text('# broken\\n', encoding='utf-8'); "
        "sys.exit(1)"
    )
    config = make_config(
        steps=2,
        continue_on_error=False,
        runner_args=["-c", script],
    )
    config.task_file = str(task_file)
    config.stop_when_tasks_complete = True
    service = LoopService(tmp_path)
    state = service.create_loop(config, loop_id="task-file-runner-failure")

    final = service.run_loop(state.loop_id)

    assert final.status == "failed"
    assert final.completed_iterations == 1
    assert [item.number for item in final.iterations] == [1]


def test_unconfirmed_cleanup_is_fail_closed(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(steps=None), loop_id="cleanup-blocked")
    service.request_single_iteration(state.loop_id)

    def fail_cleanup(*, lifecycle: RunnerLifecycle, **_kwargs):  # type: ignore[no-untyped-def]
        lifecycle.process_group_id = 12345
        lifecycle.direct_child_reaped = False
        lifecycle.cleanup_confirmed = False
        raise ProcessCleanupError("cleanup not confirmed")

    service.runner.run = fail_cleanup  # type: ignore[method-assign]
    with pytest.raises(ProcessCleanupError, match="not confirmed"):
        service.run_loop(state.loop_id)

    blocked = service.load_loop(state.loop_id)
    assert blocked.status == "cleanup_failed"
    assert blocked.pending_single_iteration is True
    with pytest.raises(RuntimeError, match="cleanup"):
        service.request_control(state.loop_id, "stop")
    with pytest.raises(RuntimeError, match="active"):
        service.resume_loop(state.loop_id)
    with pytest.raises(RuntimeError, match="marked active"):
        service.remove_loop(state.loop_id, force=True)


def test_post_final_event_failure_does_not_rollback_iteration(
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(), loop_id="post-final")
    service.queue_follow_up(state.loop_id, "consume once")
    original_append = service.store.append_event

    def fail_completed_event(loop_id: str, event: dict) -> bool:
        if event.get("event") == "iteration_completed":
            raise RuntimeError("event sink failed")
        return original_append(loop_id, event)

    service.store.append_event = fail_completed_event  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="event sink failed"):
        service.run_loop(state.loop_id)

    finalized = service.load_loop(state.loop_id)
    assert finalized.status == "completed"
    assert finalized.completed_iterations == 1
    assert len(finalized.iterations) == 1
    assert finalized.pending_single_iteration is False
    assert finalized.queued_follow_up is None


def test_post_final_failure_does_not_strand_continuing_loop_as_running(
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(steps=2), loop_id="post-final-continue")
    original_append = service.store.append_event

    def fail_completed_event(loop_id: str, event: dict) -> bool:
        if event.get("event") == "iteration_completed":
            raise RuntimeError("event sink failed")
        return original_append(loop_id, event)

    service.store.append_event = fail_completed_event  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="event sink failed"):
        service.run_loop(state.loop_id)

    finalized = service.load_loop(state.loop_id)
    assert finalized.status == "failed"
    assert finalized.completed_iterations == 1
    assert len(finalized.iterations) == 1

    service.store.append_event = original_append  # type: ignore[method-assign]
    resumed = service.resume_loop(state.loop_id)
    assert resumed.status == "completed"
    assert resumed.completed_iterations == 2


def test_event_append_flushes_and_fsyncs(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(), loop_id="durable-event")
    original_fsync = os.fsync
    fsync_calls: list[int] = []

    def record_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        original_fsync(fd)

    monkeypatch.setattr("ailoop.state.os.fsync", record_fsync)
    assert service.store.append_event(state.loop_id, {"event": "durable"}) is True
    assert len(fsync_calls) == 2


def test_scheduled_loops_are_configuration_only(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_config(steps=None), loop_id="scheduled-safe")
    scheduled = {"mode": "scheduled", "schedule_type": "hours", "schedule_every": "1"}
    service.update_loop_config(state.loop_id, state.run_config, scheduled, {})

    assert service.run_loop(state.loop_id).status == "idle"
    with pytest.raises(RuntimeError, match="Scheduled"):
        service.resume_loop(state.loop_id)
    with pytest.raises(RuntimeError, match="Scheduled"):
        service.request_resume(state.loop_id)
    with pytest.raises(RuntimeError, match="Scheduled"):
        service.request_single_iteration(state.loop_id)
    with pytest.raises(RuntimeError, match="Scheduled"):
        service.queue_follow_up(state.loop_id, "run now", run_next=True)
    with pytest.raises(RuntimeError, match="Scheduled"):
        service.request_restart(state.loop_id, state.run_config, scheduled, {})

    queued = service.queue_follow_up(state.loop_id, "save for scheduler")
    assert queued.queued_follow_up == "save for scheduler"
    assert queued.pending_single_iteration is False


@pytest.mark.parametrize("error_type", [RuntimeError, OSError, KeyboardInterrupt])
def test_local_runner_cleans_group_and_preserves_callback_exception(
    tmp_path: Path,
    error_type: type[BaseException],
) -> None:
    pid_file = tmp_path / f"{error_type.__name__}.pid"
    runner = LocalRunner()
    lifecycle = RunnerLifecycle()
    script = (
        "import os, time; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )

    def fail_callback() -> bool:
        raise error_type("callback exploded")

    with pytest.raises(error_type, match="callback exploded"):
        runner.run(
            command=sys.executable,
            args=["-c", script],
            env={},
            stdout_log=tmp_path / f"{error_type.__name__}.stdout",
            stderr_log=tmp_path / f"{error_type.__name__}.stderr",
            should_stop=fail_callback,
            lifecycle=lifecycle,
        )

    assert lifecycle.cleanup_confirmed is True
    assert lifecycle.direct_child_reaped is True
    assert lifecycle.process_group_id is not None
    assert process_group_exists(lifecycle.process_group_id) is False


def test_local_runner_cleans_descendant_after_leader_exits(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys; from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"Path({str(child_pid_file)!r}).write_text(str(child.pid))"
    )
    lifecycle = RunnerLifecycle()

    result = LocalRunner().run(
        command=sys.executable,
        args=["-c", script],
        env={},
        stdout_log=tmp_path / "leader.stdout",
        stderr_log=tmp_path / "leader.stderr",
        lifecycle=lifecycle,
    )

    assert result.exit_code == 0
    assert child_pid_file.exists()
    assert lifecycle.cleanup_confirmed is True
    assert lifecycle.process_group_id is not None
    assert process_group_exists(lifecycle.process_group_id) is False


def test_term_resistant_process_tree_is_killed_before_timeout_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("ailoop.runners.local.TERMINATION_GRACE_SECONDS", 0.2)
    monkeypatch.setattr("ailoop.runners.local.FINAL_KILL_GRACE_SECONDS", 1)
    script = """
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.Popen([
    sys.executable,
    "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
])
time.sleep(30)
"""
    lifecycle = RunnerLifecycle()

    result = LocalRunner().run(
        command=sys.executable,
        args=["-c", script],
        env={},
        stdout_log=tmp_path / "resistant.stdout",
        stderr_log=tmp_path / "resistant.stderr",
        timeout_seconds=1,
        lifecycle=lifecycle,
    )

    assert result.timed_out is True
    assert lifecycle.cleanup_confirmed is True
    assert lifecycle.process_group_id is not None
    assert process_group_exists(lifecycle.process_group_id) is False


def test_parent_sigkill_leaves_loop_fail_closed_while_runner_survives(tmp_path: Path) -> None:
    runner_pid_file = tmp_path / "orphan-runner.pid"
    script = (
        "import os, time; from pathlib import Path; "
        f"Path({str(runner_pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    service = LoopService(tmp_path)
    state = service.create_loop(
        make_config(runner_args=["-c", script]),
        loop_id="parent-killed",
    )
    parent_code = (
        "import sys; from pathlib import Path; from ailoop.service import LoopService; "
        "LoopService(Path(sys.argv[1])).run_loop(sys.argv[2])"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(tmp_path), state.loop_id])
    runner_pid: int | None = None
    try:
        wait_until(
            lambda: (
                runner_pid_file.exists() and service.load_loop(state.loop_id).status == "running"
            ),
            timeout=5,
        )
        runner_pid = int(runner_pid_file.read_text())
        parent.kill()
        parent.wait(timeout=5)
        wait_until(lambda: not service.store.is_locked(state.loop_id), timeout=5)

        with pytest.raises(RuntimeError, match="marked active"):
            service.run_loop(state.loop_id)
        with pytest.raises(RuntimeError, match="active"):
            service.resume_loop(state.loop_id)
        with pytest.raises(RuntimeError, match="marked active"):
            service.remove_loop(state.loop_id, force=True)
        assert process_group_exists(runner_pid) is True
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if runner_pid is not None and process_group_exists(runner_pid):
            os.killpg(runner_pid, signal.SIGKILL)
            wait_until(lambda: not process_group_exists(runner_pid), timeout=10)
