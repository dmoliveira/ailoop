import fcntl
import json
from pathlib import Path

import pytest

from ailoop.models import LoopRunConfig
from ailoop.paths import lock_file, raw_loop_dir
from ailoop.runners.local import CAPTURE_TAIL_LINES, LocalRunner
from ailoop.service import LoopService
from ailoop.stats import render_iteration_summary, render_stats, render_status


def make_service_run_config(*, workspace_root: str | None = None) -> LoopRunConfig:
    return LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=2,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
        workspace_root=workspace_root,
    )


def test_create_loop_persists_complete_config_in_initial_state(tmp_path: Path) -> None:
    service = LoopService(tmp_path)

    state = service.create_loop(
        make_service_run_config(),
        "configured-positional",
        dashboard_config={"mode": "scheduled", "autonomy": "level-4"},
        workspace_config={"include": "src/**", "exclude": ".git/**"},
    )

    loaded = service.load_loop(state.loop_id)
    assert loaded.dashboard_config == {"mode": "scheduled", "autonomy": "level-4"}
    assert loaded.workspace_config == {"include": "src/**", "exclude": ".git/**"}


@pytest.mark.parametrize(
    "failure_point",
    ["state_write", "event_exception", "event_false", "workspace_history"],
)
def test_create_loop_rolls_back_all_recoverable_creation_failures(
    tmp_path: Path,
    monkeypatch,
    failure_point: str,
) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = LoopService(state_root)
    run_config = make_service_run_config(
        workspace_root=str(workspace) if failure_point == "workspace_history" else None
    )

    if failure_point == "state_write":
        monkeypatch.setattr(
            service.store,
            "_write_unlocked",
            lambda _state: (_ for _ in ()).throw(OSError("state write failed")),
        )
    elif failure_point == "event_exception":
        monkeypatch.setattr(
            service.store,
            "append_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event write failed")),
        )
    elif failure_point == "event_false":
        monkeypatch.setattr(service.store, "append_event", lambda *_args, **_kwargs: False)
    else:
        monkeypatch.setattr(
            service.workspace_history,
            "append_prompt",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("history write failed")),
        )

    with pytest.raises((OSError, RuntimeError)):
        service.create_loop(
            run_config,
            "creation-rollback",
            dashboard_config={"mode": "scheduled"},
            workspace_config={"include": "src/**"},
        )

    assert not raw_loop_dir(state_root, "creation-rollback").exists()
    assert service.list_loops() == []
    with pytest.raises(FileNotFoundError):
        service.load_loop("creation-rollback")


@pytest.mark.parametrize("event_failure", ["false", "exception"])
@pytest.mark.parametrize(
    "operation",
    [
        "control",
        "resume",
        "follow-up",
        "follow-up-only",
        "clear-follow-up",
        "single-iteration",
    ],
)
def test_committed_intents_survive_event_failures(
    tmp_path: Path,
    monkeypatch,
    operation: str,
    event_failure: str,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_service_run_config(), loop_id=f"intent-{operation}")
    if operation == "clear-follow-up":
        service.queue_follow_up(state.loop_id, "queued")

    original_append = service.store.append_event
    if event_failure == "false":
        monkeypatch.setattr(service.store, "append_event", lambda *_args, **_kwargs: False)
    else:

        def append_then_fail(loop_id: str, event: dict) -> bool:
            original_append(loop_id, event)
            raise OSError("event durability uncertain")

        monkeypatch.setattr(service.store, "append_event", append_then_fail)

    expected_event: str
    if operation == "control":
        result = service.request_control(state.loop_id, "pause")
        assert result.status == "paused"
        expected_event = "control"
    elif operation == "resume":
        result = service.request_resume(state.loop_id)
        assert result.control == "run"
        expected_event = "resume_requested"
    elif operation == "follow-up":
        result = service.queue_follow_up(state.loop_id, "run this", run_next=True)
        assert result.pending_single_iteration is True
        expected_event = "follow_up_queued"
    elif operation == "follow-up-only":
        result = service.queue_follow_up(state.loop_id, "save this")
        assert result.pending_single_iteration is False
        expected_event = "follow_up_queued"
    elif operation == "clear-follow-up":
        result = service.clear_follow_up(state.loop_id)
        assert result.queued_follow_up is None
        expected_event = "follow_up_cleared"
    elif operation == "single-iteration":
        result = service.request_single_iteration(state.loop_id)
        assert result.pending_single_iteration is True
        expected_event = "single_iteration_requested"
    assert service.load_loop(state.loop_id).to_dict() == result.to_dict()
    event_names = [
        json.loads(line)["event"]
        for line in (tmp_path / state.loop_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert event_names.count(expected_event) == (1 if event_failure == "exception" else 0)


@pytest.mark.parametrize(
    "error",
    [RuntimeError("contract error"), TypeError("serialization error"), ValueError("bad event")],
)
def test_postcommit_event_does_not_swallow_programming_errors(
    tmp_path: Path,
    monkeypatch,
    error: Exception,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_service_run_config(), loop_id="event-programming-error")
    monkeypatch.setattr(
        service.store,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(type(error), match=str(error)):
        service.request_control(state.loop_id, "pause")

    assert service.load_loop(state.loop_id).status == "paused"


def test_postcommit_event_does_not_swallow_keyboard_interrupt(tmp_path: Path, monkeypatch) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_service_run_config(), loop_id="event-interrupt")
    monkeypatch.setattr(
        service.store,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        service.request_control(state.loop_id, "pause")

    assert service.load_loop(state.loop_id).status == "paused"


def test_create_loop_rolls_back_after_ambiguous_event_append(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = LoopService(tmp_path)
    original_append = service.store.append_event

    def append_then_fail(loop_id: str, event: dict) -> bool:
        original_append(loop_id, event)
        raise OSError("creation event durability uncertain")

    monkeypatch.setattr(service.store, "append_event", append_then_fail)

    with pytest.raises(OSError, match="creation event durability uncertain"):
        service.create_loop(make_service_run_config(), "ambiguous-creation")

    assert not raw_loop_dir(tmp_path, "ambiguous-creation").exists()
    with pytest.raises(FileNotFoundError):
        service.load_loop("ambiguous-creation")


def test_committed_intent_survives_lock_teardown_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_service_run_config(), loop_id="intent-unlock-failure")
    original_flock = fcntl.flock
    unlock_attempts = 0

    def fail_unlock(descriptor: int, operation: int) -> None:
        nonlocal unlock_attempts
        if operation == fcntl.LOCK_UN:
            unlock_attempts += 1
            raise OSError("unlock failed")
        original_flock(descriptor, operation)

    monkeypatch.setattr("ailoop.state.fcntl.flock", fail_unlock)

    committed = service.request_single_iteration(state.loop_id)

    assert committed.pending_single_iteration is True
    assert service.load_loop(state.loop_id).pending_single_iteration is True
    assert unlock_attempts >= 3


def test_precommit_state_failure_still_raises_without_event_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(make_service_run_config(), loop_id="state-before-event")
    event_attempts: list[dict] = []
    monkeypatch.setattr(
        service.store,
        "_write_unlocked",
        lambda _state: (_ for _ in ()).throw(OSError("state unavailable")),
    )
    monkeypatch.setattr(
        service.store,
        "append_event",
        lambda _loop_id, event: event_attempts.append(event) or True,
    )

    with pytest.raises(OSError, match="state unavailable"):
        service.request_control(state.loop_id, "pause")

    assert event_attempts == []
    assert service.load_loop(state.loop_id).status == "idle"


def test_create_loop_reports_creation_and_rollback_failures(tmp_path: Path, monkeypatch) -> None:
    service = LoopService(tmp_path)
    monkeypatch.setattr(
        service.store,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event write failed")),
    )
    monkeypatch.setattr(
        "ailoop.service.shutil.rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(RuntimeError) as raised:
        service.create_loop(make_service_run_config(), "rollback-report")

    message = str(raised.value)
    assert "event write failed" in message
    assert "rollback failed: cleanup failed" in message


def test_loop_runs_and_persists_state(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=2,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="loop1")
    final_state = service.run_loop(state.loop_id)
    assert final_state.status == "completed"
    assert final_state.completed_iterations == 2
    assert final_state.last_exit_code == 0
    assert "loop1 · completed" in render_status(final_state)
    assert "🔁 iter 2" in render_iteration_summary(final_state)
    assert "🕘 recent:" in render_stats(final_state)
    loaded = service.load_loop("loop1")
    assert loaded.completed_iterations == 2


def test_retry_attempts_keep_distinct_log_evidence(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    marker = tmp_path / "attempt.txt"
    run_config = make_service_run_config()
    run_config.steps = 1
    run_config.retry_count = 1
    run_config.runner_args = [
        "-c",
        (
            "from pathlib import Path; import sys; "
            f"p=Path({str(marker)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
            "p.write_text(str(n)); print(f'out-{{n}}'); print(f'err-{{n}}', file=sys.stderr); "
            "raise SystemExit(1 if n == 1 else 0)"
        ),
    ]
    state = service.create_loop(run_config, loop_id="retry-evidence")

    final = service.run_loop(state.loop_id)

    logs = raw_loop_dir(service.state_root, state.loop_id) / "logs"
    stdout_logs = sorted(logs.glob("iteration-0001-*.attempt-*.stdout.log"))
    stderr_logs = sorted(logs.glob("iteration-0001-*.attempt-*.stderr.log"))
    assert len(stdout_logs) == 2
    assert len(stderr_logs) == 2
    assert [path.read_text().strip() for path in stdout_logs] == ["out-1", "out-2"]
    assert [path.read_text().strip() for path in stderr_logs] == ["err-1", "err-2"]
    assert Path(final.iterations[-1].stdout_log or "") == stdout_logs[-1]
    assert service.loop_paths(state.loop_id)["stdout"] == stdout_logs[-1]


def test_reset_restart_does_not_overwrite_prior_evidence(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    run_config = make_service_run_config()
    run_config.steps = 1
    state = service.create_loop(run_config, loop_id="reset-evidence")
    first = service.run_loop(state.loop_id)
    old_stdout = Path(first.iterations[-1].stdout_log or "")
    old_bytes = old_stdout.read_bytes()

    service.request_restart(
        state.loop_id,
        run_config,
        {},
        {},
        reset=True,
    )
    second = service.run_loop(state.loop_id)
    new_stdout = Path(second.iterations[-1].stdout_log or "")

    assert new_stdout != old_stdout
    assert old_stdout.read_bytes() == old_bytes
    assert new_stdout.is_file()
    assert service.loop_paths(state.loop_id)["stdout"] == new_stdout


def test_loop_paths_rejects_symlinked_logs_directory(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    run_config = make_service_run_config()
    run_config.steps = 1
    state = service.create_loop(run_config, loop_id="unsafe-logs")
    service.run_loop(state.loop_id)
    logs = raw_loop_dir(service.state_root, state.loop_id) / "logs"
    preserved = tmp_path / "preserved-logs"
    logs.rename(preserved)
    external = tmp_path / "external"
    external.mkdir()
    logs.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Unsafe loop logs directory"):
        service.loop_paths(state.loop_id)


def test_run_rejects_symlinked_logs_directory(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    run_config = make_service_run_config()
    run_config.steps = 1
    state = service.create_loop(run_config, loop_id="unsafe-write-logs")
    logs = raw_loop_dir(service.state_root, state.loop_id) / "logs"
    external = tmp_path / "external-write"
    external.mkdir()
    logs.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Unsafe loop logs directory"):
        service.run_loop(state.loop_id)

    assert list(external.iterdir()) == []


def test_pause_request_is_recorded(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="loop2")
    updated = service.request_control(state.loop_id, "pause")
    assert updated.control == "pause"


def test_request_control_rejects_invalid_values(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="loop2b")

    with pytest.raises(ValueError, match="Invalid control: invalid"):
        service.request_control(state.loop_id, "invalid")


def test_request_single_iteration_runs_one_iteration_then_pauses(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="single-step")

    requested = service.request_single_iteration(state.loop_id)
    assert requested.pending_single_iteration is True

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "paused"
    assert final_state.completed_iterations == 1
    assert final_state.pending_single_iteration is False


def test_request_single_iteration_rejects_active_loop(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=2,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="active-step")
    with service.store.mutate(state.loop_id) as persisted:
        persisted.status = "running"

    with pytest.raises(RuntimeError, match="Loop is already active: active-step"):
        service.request_single_iteration(state.loop_id)


def test_request_single_iteration_rejects_completed_loop(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="done-step")
    service.run_loop(state.loop_id)

    with pytest.raises(RuntimeError, match="Loop has no pending iterations: done-step"):
        service.request_single_iteration(state.loop_id)


def test_run_loop_preserves_pre_start_pause_request(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=2,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="pause-before-start")
    service.request_control(state.loop_id, "pause")

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "paused"
    assert final_state.completed_iterations == 0


def test_run_loop_preserves_pre_start_stop_request(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=2,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="stop-before-start")
    service.request_control(state.loop_id, "stop")

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "stopped"
    assert final_state.completed_iterations == 0


def test_resume_loop_resets_control_and_runs(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="resume-run")
    service.request_control(state.loop_id, "pause")

    final_state = service.resume_loop(state.loop_id)

    assert final_state.status == "completed"
    assert final_state.completed_iterations == 1


def test_run_loop_keeps_scheduled_loops_idle(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=3600,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="scheduled-idle")
    with service.store.mutate(state.loop_id) as persisted:
        persisted.dashboard_config = {
            "mode": "scheduled",
            "schedule_type": "hours",
            "schedule_every": "1",
        }

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "idle"
    assert final_state.completed_iterations == 0


def test_scheduled_loop_rejects_inactive_pause_request(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=3600,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="scheduled-pause")
    with service.store.mutate(state.loop_id) as persisted:
        persisted.dashboard_config = {
            "mode": "scheduled",
            "schedule_type": "hours",
            "schedule_every": "1",
        }
    before = service.load_loop(state.loop_id).to_dict()
    with pytest.raises(RuntimeError, match="saved configurations"):
        service.request_control(state.loop_id, "pause")
    assert service.load_loop(state.loop_id).to_dict() == before


def test_scheduled_loop_rejects_inactive_stop_request(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=3600,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="scheduled-stop")
    with service.store.mutate(state.loop_id) as persisted:
        persisted.dashboard_config = {
            "mode": "scheduled",
            "schedule_type": "hours",
            "schedule_every": "1",
        }
    before = service.load_loop(state.loop_id).to_dict()
    with pytest.raises(RuntimeError, match="saved configurations"):
        service.request_control(state.loop_id, "stop")
    assert service.load_loop(state.loop_id).to_dict() == before


def test_scheduled_loop_allows_emergency_stop_for_claimed_process(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=3600,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="scheduled-emergency-stop")
    with service.store.mutate(state.loop_id) as persisted:
        persisted.dashboard_config = {"mode": "scheduled"}
        persisted.status = "running"

    before = service.load_loop(state.loop_id).to_dict()
    with pytest.raises(RuntimeError, match="saved configurations"):
        service.request_control(state.loop_id, "pause")
    assert service.load_loop(state.loop_id).to_dict() == before

    stopped = service.request_control(state.loop_id, "stop")

    assert stopped.status == "stop_requested"
    assert stopped.control == "stop"

    retried = service.request_control(state.loop_id, "stop")
    assert retried.status == "stop_requested"
    assert retried.control == "stop"


@pytest.mark.parametrize(
    "status",
    ["running", "pause_requested", "stop_requested", "cleanup_failed"],
)
def test_scheduled_loop_rejects_follow_up_changes_while_fail_closed(
    tmp_path: Path,
    status: str,
) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(
        make_service_run_config(),
        loop_id=f"scheduled-follow-up-{status.replace('_', '-')}",
        dashboard_config={"mode": "scheduled"},
    )
    with service.store.mutate(state.loop_id) as persisted:
        persisted.status = status

    before = service.load_loop(state.loop_id).to_dict()
    with pytest.raises(RuntimeError, match="while a worker is active"):
        service.queue_follow_up(state.loop_id, "review this")

    assert service.load_loop(state.loop_id).to_dict() == before


def test_saving_scheduled_mode_normalizes_control_and_rejects_pending_work(
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=3600,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="scheduled-normalized")
    with service.store.mutate(state.loop_id) as persisted:
        persisted.status = "stopped"
        persisted.control = "stop"

    saved = service.update_loop_config(
        state.loop_id,
        run_config,
        {"mode": "scheduled"},
        {},
    )
    assert saved.status == "idle"
    assert saved.control == "run"

    with service.store.mutate(state.loop_id) as persisted:
        persisted.pending_single_iteration = True
    before = service.load_loop(state.loop_id).to_dict()
    with pytest.raises(RuntimeError, match="manual iteration is pending"):
        service.update_loop_config(
            state.loop_id,
            run_config,
            {"mode": "scheduled"},
            {},
        )
    assert service.load_loop(state.loop_id).to_dict() == before


def test_list_loops_returns_saved_states(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    for loop_id in ["loop-a", "loop-b"]:
        run_config = LoopRunConfig(
            prompt="hello",
            runner="echo",
            agent=None,
            steps=1,
            pause_seconds=0,
            continue_on_error=True,
            retry_count=0,
            pre_prompt_enabled=False,
            attach_agent_file=False,
            pre_prompt="",
            agent_file=None,
            runner_command="python3",
            runner_args=["-c", "print('ok')"],
        )
        service.create_loop(run_config, loop_id=loop_id)

    states = service.list_loops()
    assert {state.loop_id for state in states} == {"loop-a", "loop-b"}


def test_list_loops_skips_corrupt_state_files(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    service.create_loop(run_config, loop_id="healthy")
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "state.json").write_text("{not-json")

    states = service.list_loops()

    assert [state.loop_id for state in states] == ["healthy"]


def test_create_loop_rejects_duplicate_loop_id(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    service.create_loop(run_config, loop_id="duplicate")
    with pytest.raises(RuntimeError, match="Loop already exists: duplicate"):
        service.create_loop(run_config, loop_id="duplicate")


def test_missing_runner_marks_failure_cleanly(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="missing",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=False,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="definitely-not-a-real-binary",
        runner_args=["--version"],
    )
    state = service.create_loop(run_config, loop_id="loop3")
    final_state = service.run_loop(state.loop_id)
    assert final_state.status == "failed"
    assert final_state.last_exit_code == 127
    assert final_state.completed_iterations == 1


def test_loop_paths_and_remove_loop(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="loop4")
    service.run_loop(state.loop_id)
    paths = service.loop_paths("loop4")
    assert paths["stdout"].exists()
    assert paths["stderr"].exists()
    service.remove_loop("loop4")
    with pytest.raises(FileNotFoundError):
        service.load_loop("loop4")


def test_remove_active_loop_requires_force(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    service.create_loop(run_config, loop_id="loop5")
    with pytest.raises(RuntimeError):
        service.remove_loop("loop5")


def test_remove_locked_loop_is_rejected_even_with_force(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    service.create_loop(run_config, loop_id="loop6")
    with service.store.acquire_lock("loop6"):
        with pytest.raises(RuntimeError, match="already active"):
            service.remove_loop("loop6", force=True)


def test_unlocked_diagnostic_file_does_not_block_remove(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    service.create_loop(run_config, loop_id="loop7")
    lock_path = lock_file(tmp_path, "loop7")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999")
    service.remove_loop("loop7", force=True)
    assert lock_path.exists()


def test_unlocked_diagnostic_file_does_not_block_run(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="loop7b")
    lock_path = lock_file(tmp_path, "loop7b")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999")

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "completed"
    assert final_state.completed_iterations == 1
    assert lock_path.exists()


def test_loop_stops_when_task_file_is_complete(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    task_file = tmp_path / "tasks.md"
    task_file.write_text(
        "# Loop Tasks\n\n## To do\n- [ ] First task\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        task_file=str(task_file),
        stop_when_tasks_complete=True,
        max_doing=1,
        runner_command="python3",
        runner_args=[
            "-c",
            (
                f"from pathlib import Path; Path(r'{task_file}').write_text("
                "'# Loop Tasks\\n\\n## To do\\n- None\\n\\n## Doing\\n- None\\n\\n"
                "## Done\\n- [x] First task\\n')"
            ),
        ],
    )
    state = service.create_loop(run_config, loop_id="loop8")
    final_state = service.run_loop(state.loop_id)
    assert final_state.status == "completed"
    assert final_state.completed_iterations == 1


def test_pause_request_during_iteration_is_preserved(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="loop9")
    original_run = service.runner.run

    def run_and_pause(**kwargs):  # type: ignore[no-untyped-def]
        service.request_control("loop9", "pause")
        return original_run(**kwargs)

    service.runner.run = run_and_pause  # type: ignore[method-assign]
    final_state = service.run_loop(state.loop_id)
    assert final_state.status == "paused"
    assert final_state.control == "pause"


def test_stop_request_during_iteration_stops_cleanly(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=None,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="loop10")
    original_run = service.runner.run

    def run_and_stop(**kwargs):  # type: ignore[no-untyped-def]
        service.request_control("loop10", "stop")
        return original_run(**kwargs)

    service.runner.run = run_and_stop  # type: ignore[method-assign]
    final_state = service.run_loop(state.loop_id)
    assert final_state.status == "stopped"
    assert final_state.control == "stop"


def test_local_runner_writes_output_to_logs_and_returns_tail(tmp_path: Path) -> None:
    runner = LocalRunner()
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    result = runner.run(
        command="python3",
        args=[
            "-c",
            (
                "for i in range(120):\n"
                "    print(f'out-{i}')\n"
                "import sys\n"
                "for i in range(120):\n"
                "    print(f'err-{i}', file=sys.stderr)\n"
            ),
        ],
        env={},
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )

    stdout_lines = stdout_log.read_text().splitlines()
    stderr_lines = stderr_log.read_text().splitlines()

    assert result.exit_code == 0
    assert len(stdout_lines) == 120
    assert len(stderr_lines) == 120
    assert stdout_lines[0] == "out-0"
    assert stdout_lines[-1] == "out-119"
    assert stderr_lines[0] == "err-0"
    assert stderr_lines[-1] == "err-119"
    assert result.stdout.splitlines()[0] == f"out-{120 - CAPTURE_TAIL_LINES}"
    assert result.stdout.splitlines()[-1] == "out-119"
    assert result.stderr.splitlines()[0] == f"err-{120 - CAPTURE_TAIL_LINES}"
    assert result.stderr.splitlines()[-1] == "err-119"


def test_local_runner_preserves_arbitrary_bytes_and_returns_safe_text(tmp_path: Path) -> None:
    runner = LocalRunner()
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    stdout_bytes = b"valid:\xe2\x82\xac:\xff:end\n"
    stderr_bytes = b"error:\xfe:end\n"

    result = runner.run(
        command="python3",
        args=[
            "-c",
            (
                "import os\n"
                "os.write(1, b'valid:')\n"
                "os.write(1, b'\\xe2')\n"
                "os.write(1, b'\\x82')\n"
                "os.write(1, b'\\xac')\n"
                "os.write(1, b':\\xff:end\\n')\n"
                "os.write(2, b'error:\\xfe:end\\n')\n"
            ),
        ],
        env={},
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )

    assert result.exit_code == 0
    assert stdout_log.read_bytes() == stdout_bytes
    assert stderr_log.read_bytes() == stderr_bytes
    assert result.stdout == "valid:€:�:end"
    assert result.stderr == "error:�:end"


def test_local_runner_records_oserror_in_stderr_log(tmp_path: Path) -> None:
    runner = LocalRunner()
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    result = runner.run(
        command="definitely-not-a-real-binary",
        args=[],
        env={},
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )

    assert result.exit_code == 127
    assert stdout_log.read_text() == ""
    assert stderr_log.read_text() == result.stderr


def test_inter_iteration_wait_observes_stop_request(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=2,
        pause_seconds=60,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="wait-stop")
    sleeps: list[float] = []

    def request_stop(seconds: float) -> None:
        sleeps.append(seconds)
        service.request_control(state.loop_id, "stop")

    monkeypatch.setattr("ailoop.service.time.sleep", request_stop)

    assert service._wait_between_iterations(state) is True
    assert sleeps == [0.25]


def test_inter_iteration_wait_observes_pause_request(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent=None,
        steps=2,
        pause_seconds=60,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    state = service.create_loop(run_config, loop_id="wait-pause")

    def request_pause(_seconds: float) -> None:
        service.request_control(state.loop_id, "pause")

    monkeypatch.setattr("ailoop.service.time.sleep", request_pause)

    assert service._wait_between_iterations(state) is True


def test_local_runner_times_out_and_marks_result(tmp_path: Path) -> None:
    runner = LocalRunner()
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    result = runner.run(
        command="python3",
        args=["-c", "import time; time.sleep(30)"],
        env={},
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        timeout_seconds=1,
    )

    assert result.timed_out is True
    assert result.exit_code != 0
    assert "runner timed out after 1 seconds" in stderr_log.read_text()
    assert result.duration_seconds < 7


def test_local_runner_stops_when_control_requests_stop(tmp_path: Path) -> None:
    runner = LocalRunner()
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    result = runner.run(
        command="python3",
        args=["-c", "import time; time.sleep(30)"],
        env={},
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        should_stop=lambda: True,
    )

    assert result.cancelled is True
    assert result.timed_out is False
    assert "runner stopped by loop control" in stderr_log.read_text()
    assert result.duration_seconds < 7
