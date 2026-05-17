import subprocess
from pathlib import Path

from ailoop.models import LoopRunConfig
from ailoop.service import LoopService
from ailoop.tui import LoopDashboard, launch_in_tmux, tail_text


def test_tail_text_reads_last_lines(tmp_path: Path) -> None:
    path = tmp_path / "out.log"
    path.write_text("a\nb\nc\n")
    assert tail_text(path, lines=2) == "b\nc"


def test_tui_mounts_and_loads_loop(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="tui1")
    service.run_loop(state.loop_id)

    async def run_test() -> None:
        app = LoopDashboard(
            config_path=Path("~/.config/ailoop/config.yaml").expanduser(),
            loop_id="tui1",
        )
        app.service = service
        app.filter_mode = "all"
        async with app.run_test() as pilot:
            app.refresh_data()
            await pilot.pause()
            assert app.selected_loop_id == "tui1"

    import asyncio

    asyncio.run(run_test())


def test_launch_in_tmux_uses_tmux(monkeypatch) -> None:
    seen = {}

    def fake_run(command, check):  # type: ignore[no-untyped-def]
        seen["command"] = command
        seen["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    launch_in_tmux(Path("/tmp/config.yaml"), loop_id="loop1")
    command = seen["command"]
    assert command[0] == "tmux"
    assert command[4] == "ailoop-tui"
    assert "--tmux-session" in command[-1]
    assert "--loop-id loop1" in command[-1]


def test_tui_preselect_switches_to_all_for_completed_loop(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="done-loop")
    service.run_loop(state.loop_id)
    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser(), loop_id="done-loop")
        app.service = service
        async with app.run_test() as pilot:
            app.refresh_data()
            await pilot.pause()
            assert app.filter_mode == "all"
            assert app.selected_loop_id == "done-loop"

    import asyncio

    asyncio.run(run_test())


def test_tui_remove_uses_force_for_paused_loop(tmp_path: Path) -> None:
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
    service.create_loop(run_config, loop_id="paused-loop")
    seen = {}

    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
        app.service = service
        app.selected_loop_id = "paused-loop"

        def fake_remove(loop_id, force=False):  # type: ignore[no-untyped-def]
            seen["loop_id"] = loop_id
            seen["force"] = force

        app.service.remove_loop = fake_remove  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app.action_remove_selected()
            await pilot.pause()
            app.action_remove_selected()
            await pilot.pause()

    import asyncio

    asyncio.run(run_test())
    assert seen == {"loop_id": "paused-loop", "force": True}


def test_empty_loop_message_for_no_loops(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    text = app._empty_loop_message()
    assert "No loops yet." in text
    assert 'ailoop run "Review the repo"' in text


def test_unselected_detail_message_includes_counts(tmp_path: Path) -> None:
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
    service.create_loop(run_config, loop_id="loop-count")
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    text = app._unselected_detail_message()
    assert "loops: 1" in text
    assert "choose a loop" in text


def test_summary_counts_reflect_state_buckets(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
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
    service.create_loop(run_config, loop_id="one")
    state = service.create_loop(run_config, loop_id="two")
    service.request_control(state.loop_id, "pause")
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    assert app._summary_counts() == (2, 2, 1)
