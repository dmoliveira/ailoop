import os
from pathlib import Path

import pytest

from ailoop.models import LoopRunConfig
from ailoop.runners.local import CAPTURE_TAIL_LINES, LocalRunner
from ailoop.service import LoopService
from ailoop.stats import render_iteration_summary, render_stats, render_status


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
    state.status = "running"
    service.store.save(state)

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


def test_run_loop_resumes_when_control_is_reset_to_run(tmp_path: Path) -> None:
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
    state.status = "paused"
    state.control = "run"
    service.store.save(state)

    final_state = service.run_loop(state.loop_id)

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
    state.dashboard_config = {"mode": "scheduled", "schedule_type": "hours", "schedule_every": "1"}
    service.store.save(state)

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "idle"
    assert final_state.completed_iterations == 0


def test_scheduled_loop_preserves_pre_start_pause_request(tmp_path: Path) -> None:
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
    state.dashboard_config = {"mode": "scheduled", "schedule_type": "hours", "schedule_every": "1"}
    service.store.save(state)
    service.request_control(state.loop_id, "pause")

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "paused"
    assert final_state.completed_iterations == 0


def test_scheduled_loop_preserves_pre_start_stop_request(tmp_path: Path) -> None:
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
    state.dashboard_config = {"mode": "scheduled", "schedule_type": "hours", "schedule_every": "1"}
    service.store.save(state)
    service.request_control(state.loop_id, "stop")

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "stopped"
    assert final_state.completed_iterations == 0


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
    lock_path = tmp_path / "loop6" / ".lock"
    lock_path.write_text(str(os.getpid()))
    with pytest.raises(RuntimeError):
        service.remove_loop("loop6", force=True)


def test_stale_lock_is_cleaned_and_remove_can_continue(tmp_path: Path) -> None:
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
    lock_path = tmp_path / "loop7" / ".lock"
    lock_path.write_text("999999")
    service.remove_loop("loop7", force=True)
    assert not lock_path.exists()


def test_stale_lock_is_cleaned_and_run_can_continue(tmp_path: Path) -> None:
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
    lock_path = tmp_path / "loop7b" / ".lock"
    lock_path.write_text("999999")

    final_state = service.run_loop(state.loop_id)

    assert final_state.status == "completed"
    assert final_state.completed_iterations == 1
    assert not lock_path.exists()


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
