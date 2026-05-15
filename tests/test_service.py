import os
from pathlib import Path

import pytest

from ailoop.models import LoopRunConfig
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
