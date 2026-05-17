import subprocess
from pathlib import Path

import pytest

from ailoop.memory import MemoryStore
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


def test_memory_log_text_lists_entries_with_kind_and_favorite(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Quick review",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    text = app._memory_log_text()
    assert "Quick review" in text
    assert "preset" in text
    assert "★" in text
    assert "Used" in text
    assert "Labels" in text


def test_memory_log_meta_reports_entry_and_favorite_counts(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="One",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    memory.create(
        kind="history",
        title="Two",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    assert (
        app._memory_log_meta()
        == "source memory · filter all · label - · query - · selected 1/2 · favorites 1 · scope cwd"
    )


def test_memory_log_meta_reports_all_folder_scope(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="One",
        run_config=run_config,
        folder=tmp_path / "other",
        favorite=True,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_all_folders = True
    assert "scope all-folders" in app._memory_log_meta()


def test_summary_selected_text_uses_memory_entry_when_memory_mode_active(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Quick review",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
        labels=["ops"],
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    assert app._summary_selected_text(None) == f"memory all · labels 1 · selected {entry.id}"
    app.memory_label = "ops"
    text = app._memory_detail_text()
    assert "active label: ops" in text
    assert "available labels: 1" in text
    assert "b previous label" in text
    assert "n next label" in text
    assert "c clear label" in text


def test_summary_bar_text_omits_redundant_memory_log_prefix(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Quick review",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    text = app._summary_bar_text(0, 0, 0, 0, 0, None)
    assert f"memory all · labels 0 · selected {entry.id}" in text
    assert "log memory" not in text


def test_memory_help_text_does_not_require_selected_loop(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="history",
        title="History One",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    text = app._memory_help_text()
    assert "memory all" in text
    assert "entries 1" in text
    assert "labels 0/0" in text
    assert "b label<" in text
    assert "n label>" in text
    assert "c labelx" in text
    assert "8 replay" in text
    assert "no loop selected" not in text


def test_memory_log_text_filters_to_favorites(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Fav",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    memory.create(
        kind="history",
        title="Plain",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_filter = "favorites"
    text = app._memory_log_text()
    assert "Fav" in text
    assert "Plain" not in text


def test_memory_log_text_filters_to_history(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Preset",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    memory.create(
        kind="history",
        title="History",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_filter = "history"
    text = app._memory_log_text()
    assert "History" in text
    assert "Preset" not in text


def test_memory_log_text_filters_to_presets(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Preset One",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    memory.create(
        kind="history",
        title="History One",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_filter = "presets"
    text = app._memory_log_text()
    assert "Preset One" in text
    assert "History One" not in text


def test_set_log_memory_presets_preserves_search_context(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory_filter = "history"
    app.memory_label = "ops"
    app.memory_query = "night"
    app.memory_all_folders = True
    app.memory_index = 3
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    app.action_set_log_memory_presets()
    assert app.log_kind == "memory"
    assert app.memory_filter == "presets"
    assert app.memory_label == "ops"
    assert app.memory_query == "night"
    assert app.memory_all_folders is True
    assert app.memory_index == 0


def test_memory_log_text_filters_to_selected_label(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Ops entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["ops", "nightly"],
    )
    memory.create(
        kind="preset",
        title="Docs entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["docs"],
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_label = "ops"
    text = app._memory_log_text()
    assert "Ops entry" in text
    assert "Docs entry" not in text


def test_memory_log_text_filters_to_query(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Ops entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["ops", "nightly"],
    )
    memory.create(
        kind="preset",
        title="Docs entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["docs"],
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_query = "night"
    text = app._memory_log_text()
    assert "Ops entry" in text
    assert "Docs entry" not in text


def test_memory_label_next_cycles_labels(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Ops entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["ops"],
    )
    memory.create(
        kind="preset",
        title="Docs entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["docs"],
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    app.action_memory_label_next()
    assert app.memory_label == "docs"
    app.action_memory_label_next()
    assert app.memory_label == "ops"


def test_memory_label_clear_resets_filter(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Ops entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["ops"],
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_label = "ops"
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    app.action_memory_label_clear()
    assert app.memory_label is None


def test_memory_detail_text_lists_query_controls(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Ops entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["ops"],
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    text = app._memory_detail_text()
    assert "scope: cwd" in text
    assert "o toggle scope" in text
    assert "/ focus query" in text
    assert "esc clear query" in text


def test_memory_scope_toggle_shows_entries_from_other_folders(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Local entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["ops"],
    )
    other_folder = tmp_path / "other"
    other_folder.mkdir()
    memory.create(
        kind="preset",
        title="Global entry",
        run_config=run_config,
        folder=other_folder,
        labels=["docs"],
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    assert "Local entry" in app._memory_log_text()
    assert "Global entry" not in app._memory_log_text()
    app.action_memory_scope_toggle()
    assert app.memory_all_folders is True
    assert "Local entry" in app._memory_log_text()
    assert "Global entry" in app._memory_log_text()


def test_memory_empty_state_mentions_scope_toggle(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = MemoryStore(tmp_path)
    text = app._memory_log_text()
    assert "scope: cwd" in text
    assert "Press o to show all folders." in text


def test_memory_query_clear_resets_filter_and_widget(monkeypatch, tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="Ops entry",
        run_config=run_config,
        folder=Path.cwd(),
        labels=["ops", "nightly"],
    )

    class FakeInput:
        def __init__(self) -> None:
            self.value = "night"

        def focus(self) -> None:
            return None

    widget = FakeInput()
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_query = "night"
    app.memory_index = 1
    app.memory_archive_armed = True
    app.memory_delete_armed = True
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: widget)
    app.action_memory_query_clear()
    assert app.memory_query == ""
    assert app.memory_index == 0
    assert app.memory_archive_armed is False
    assert app.memory_delete_armed is False
    assert widget.value == ""


def test_memory_replay_uses_top_filtered_entry(monkeypatch, tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="history",
        title="Replay me",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    seen = {}

    def fake_popen(command, stdout, stderr, start_new_session):  # type: ignore[no-untyped-def]
        seen["command"] = command
        seen["stdout"] = stdout
        seen["stderr"] = stderr
        seen["start_new_session"] = start_new_session
        return subprocess.Popen  # type: ignore[return-value]

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_filter = "history"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app.action_memory_replay()
    command = seen["command"]
    assert command[-2:] == ["replay", entry.id]


def test_memory_selection_moves_to_next_entry(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    first = memory.create(
        kind="preset",
        title="First",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    second = memory.create(
        kind="preset",
        title="Second",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    assert app._primary_memory_entry().id == second.id
    app.action_memory_next()
    assert app._primary_memory_entry().id == first.id


def test_memory_log_text_marks_selected_entry(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    memory.create(
        kind="preset",
        title="First",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    selected = memory.create(
        kind="preset",
        title="Second",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=True,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    text = app._memory_log_text()
    assert f">   {selected.id}" in text


def test_memory_favorite_toggles_top_filtered_entry(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Fav me",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_filter = "all"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app.action_memory_favorite()
    updated = memory.load(entry.id, folder=Path.cwd())
    assert updated.favorite is True


def test_memory_favorite_toggles_selected_entry(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    first = memory.create(
        kind="preset",
        title="First",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    second = memory.create(
        kind="preset",
        title="Second",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    app.action_memory_next()
    app.action_memory_favorite()
    assert memory.load(first.id, folder=Path.cwd()).favorite is True
    assert memory.load(second.id, folder=Path.cwd()).favorite is False


def test_memory_detail_text_includes_show_and_edit_commands(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Quick review",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    text = app._memory_detail_text()
    assert f"ailoop memory show {entry.id}" in text
    assert f"ailoop memory edit {entry.id} --title 'Quick review'" in text
    assert f"ailoop memory favorite {entry.id}" in text
    assert f"ailoop memory archive {entry.id}" in text


def test_memory_detail_text_uses_restore_command_for_archived_entry(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Archived",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    memory.edit(entry.id, archived=True, folder=Path.cwd())
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_filter = "archived"
    text = app._memory_detail_text()
    assert f"ailoop memory archive {entry.id} --off" in text


def test_memory_delete_requires_confirmation(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Delete me",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app.action_memory_delete()
    assert app.memory_delete_armed is True
    assert memory.load(entry.id, folder=Path.cwd()).id == entry.id


def test_memory_delete_removes_selected_entry(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    first = memory.create(
        kind="preset",
        title="First",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    second = memory.create(
        kind="preset",
        title="Second",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    app.action_memory_next()
    app.action_memory_delete()
    app.action_memory_delete()
    with pytest.raises(FileNotFoundError):
        memory.load(first.id, folder=Path.cwd())
    assert memory.load(second.id, folder=Path.cwd()).id == second.id


def test_memory_archive_requires_confirmation(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Archive me",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app.action_memory_archive()
    assert app.memory_archive_armed is True
    assert memory.load(entry.id, folder=Path.cwd()).id == entry.id


def test_memory_archive_hides_selected_entry_from_default_list(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    first = memory.create(
        kind="preset",
        title="First",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    second = memory.create(
        kind="preset",
        title="Second",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    app.action_memory_next()
    app.action_memory_archive()
    app.action_memory_archive()
    entries = memory.list_entries(folder=Path.cwd())
    assert [entry.id for entry in entries] == [second.id]
    archived = memory.load(first.id, folder=Path.cwd())
    assert archived.archived is True


def test_memory_archived_filter_lists_only_archived_entries(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    visible = memory.create(
        kind="preset",
        title="Visible",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    archived = memory.create(
        kind="preset",
        title="Archived",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    memory.edit(archived.id, archived=True, folder=Path.cwd())
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.memory_filter = "archived"
    text = app._memory_log_text()
    assert archived.id in text
    assert visible.id not in text


def test_memory_restore_unarchives_selected_entry(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    run_config = LoopRunConfig(
        prompt="Review the repo",
        runner="opencode",
        agent="orchestrator",
        steps=5,
        pause_seconds=10,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )
    entry = memory.create(
        kind="preset",
        title="Archived",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    memory.edit(entry.id, archived=True, folder=Path.cwd())
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_filter = "archived"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app.action_memory_restore()
    restored = memory.load(entry.id, folder=Path.cwd())
    assert restored.archived is False
