import subprocess
from pathlib import Path

import pytest

from ailoop.memory import MemoryStore
from ailoop.models import LoopRunConfig
from ailoop.service import LoopService
from ailoop.tui import LoopDashboard, launch_in_tmux, render_progress_text, tail_text


def test_tail_text_reads_last_lines(tmp_path: Path) -> None:
    path = tmp_path / "out.log"
    path.write_text("a\nb\nc\n")
    assert tail_text(path, lines=2) == "b\nc"


def test_render_progress_text_uses_bar_for_finite_targets() -> None:
    assert render_progress_text(1, 5) == "█░░░ 1/5"
    assert render_progress_text(5, 5) == "████ 5/5"
    assert render_progress_text(2, None) == "∞ 2"


def test_memory_mode_tolerates_missing_launch_cwd(monkeypatch, tmp_path: Path) -> None:
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
        title="Fallback entry",
        run_config=run_config,
        folder=tmp_path / "missing-cwd-source",
        favorite=False,
    )

    def missing_getcwd() -> str:
        raise FileNotFoundError

    monkeypatch.setattr("os.getcwd", missing_getcwd)
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    assert app.launch_cwd is None
    assert app.memory_all_folders is True
    assert app._can_toggle_memory_scope() is False
    assert app._memory_scope_text() == "all-folders (cwd unavailable)"
    assert app._memory_scope_text(compact=True) == "all(no-cwd)"
    assert entry.id in app._memory_log_text()


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


def test_empty_loop_message_for_current_filter_has_recovery_hint(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    app.filter_mode = "all"
    app._summary_counts = lambda: (1, 0, 0)  # type: ignore[method-assign]
    text = app._empty_loop_message()
    assert "No loops in the current filter." in text
    assert "Press l for all loops, g for running, or a for active." in text


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
    assert "labels: b n c" in text


def test_summary_selected_text_compacts_in_memory_mode_at_80_columns(tmp_path: Path) -> None:
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
    assert app._summary_selected_text(None, width=80) == f"mem all · lab 1 · sel {entry.id[:8]}"


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
    assert "current branch" not in text


def test_summary_bar_text_compacts_at_80_columns(tmp_path: Path) -> None:
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
    text = app._summary_bar_text(0, 0, 0, 0, 0, None, width=80)
    assert "all 0 · act 0 · run 0 · pause 0 · fail 0" in text
    assert "f running" in text
    assert f"mem all · lab 0 · sel {entry.id[:8]}" in text


def test_summary_bar_text_compacts_non_memory_mode_at_80_columns() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    text = app._summary_bar_text(0, 0, 0, 0, 0, None, width=80)
    assert text == "all 0 · act 0 · run 0 · pause 0 · fail 0 · f running · stdout · sel none"


def test_summary_selected_text_shortens_next_run_for_wide_layout() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeState:
        loop_id = "reliability-review"
        status = "running"
        current_iteration = 2
        completed_iterations = 1

        class run_config:
            steps = 5

    app._schedule_card_text = lambda _state: "x\ny\nz\nNext run countdown: in 30 minutes"  # type: ignore[method-assign]

    text = app._summary_selected_text(FakeState(), width=140)

    assert "selected reliability-" in text
    assert "iter 2/5" in text
    assert "next 30m" in text
    assert "current branch" not in text


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
    text = app._memory_help_text(width=120)
    assert "logs 1-7/m/0" in text
    assert "all" in text
    assert "1" in text
    assert "0/0" in text
    assert "b" in text
    assert "n" in text
    assert "c" in text
    assert "8" in text
    assert "no loop selected" not in text


def test_memory_help_text_uses_compact_footer_at_80_columns(tmp_path: Path) -> None:
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
    text = app._memory_help_text(width=80)
    assert "↑↓ filt g/a/l · 1-7/m/0 · r/q" in text
    assert "mem:all" in text
    assert "cwd" in text
    assert "ent:1" in text
    assert "act:[ ] b/n/c o / 8/9/z/x" in text


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


def test_set_log_memory_rerenders_summary_bar(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    seen: list[str] = []
    app._sync_button_state = lambda: None  # type: ignore[method-assign]
    app._render_selected = lambda: None  # type: ignore[method-assign]
    app._render_summary_bar = lambda: seen.append("summary")  # type: ignore[method-assign]
    app.action_set_log_memory()
    assert app.log_kind == "memory"
    assert app.memory_filter == "all"
    assert seen == ["summary"]


def test_memory_toolbar_buttons_route_to_memory_actions() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    seen: list[str] = []
    app.action_set_log_memory = lambda: seen.append("memory")  # type: ignore[method-assign]
    app.action_set_log_memory_favorites = lambda: seen.append("favorites")  # type: ignore[method-assign]
    app.action_set_log_memory_history = lambda: seen.append("history")  # type: ignore[method-assign]
    app.action_set_log_memory_archived = lambda: seen.append("archived")  # type: ignore[method-assign]

    class FakeButton:
        def __init__(self, button_id: str) -> None:
            self.id = button_id

    class FakeEvent:
        def __init__(self, button_id: str) -> None:
            self.button = FakeButton(button_id)

    app.on_button_pressed(FakeEvent("log-memory"))
    app.on_button_pressed(FakeEvent("log-memory-favorites"))
    app.on_button_pressed(FakeEvent("log-memory-history"))
    app.on_button_pressed(FakeEvent("log-memory-archived"))

    assert seen == ["memory", "favorites", "history", "archived"]


def test_memory_toolbar_buttons_use_clearer_labels() -> None:
    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#memory-label-prev").label == "b prev label"
            assert app.query_one("#memory-label-next").label == "n next label"
            assert app.query_one("#memory-label-clear").label == "c clear label"
            assert app.query_one("#memory-scope-toggle").label == "o folders"

    import asyncio

    asyncio.run(run_test())


def test_memory_controls_only_show_in_memory_mode() -> None:
    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.log_kind != "memory"
            assert not app.query_one("#memory-filter-toolbar").has_class("memory-ui-hidden")
            assert app.query_one("#memory-action-toolbar").has_class("memory-ui-hidden")
            assert app.query_one("#memory-query").has_class("memory-ui-hidden")

            app.action_set_log_memory()
            await pilot.pause()
            assert app.log_kind == "memory"
            assert not app.query_one("#memory-filter-toolbar").has_class("memory-ui-hidden")
            assert not app.query_one("#memory-action-toolbar").has_class("memory-ui-hidden")
            assert not app.query_one("#memory-query").has_class("memory-ui-hidden")

            app.action_set_log_stdout()
            await pilot.pause()
            assert app.log_kind == "stdout"
            assert not app.query_one("#memory-filter-toolbar").has_class("memory-ui-hidden")
            assert app.query_one("#memory-action-toolbar").has_class("memory-ui-hidden")
            assert app.query_one("#memory-query").has_class("memory-ui-hidden")

    import asyncio

    asyncio.run(run_test())


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
    assert "scope/query: o / esc" in text


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

    def fake_popen(command, cwd, stdout, stderr, start_new_session):  # type: ignore[no-untyped-def]
        seen["command"] = command
        seen["cwd"] = cwd
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
    assert seen["cwd"] == app.launch_cwd


def test_memory_replay_uses_safe_fallback_cwd_when_launch_cwd_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
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
        title="Replay me safely",
        run_config=run_config,
        folder=tmp_path / "missing-cwd-source",
        favorite=False,
    )
    seen = {}

    def fake_popen(command, cwd, stdout, stderr, start_new_session):  # type: ignore[no-untyped-def]
        seen["command"] = command
        seen["cwd"] = cwd
        seen["stdout"] = stdout
        seen["stderr"] = stderr
        seen["start_new_session"] = start_new_session
        return subprocess.Popen  # type: ignore[return-value]

    def missing_getcwd() -> str:
        raise FileNotFoundError

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.getcwd", missing_getcwd)
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_filter = "history"
    app.refresh_data = lambda: None  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app.action_memory_replay()
    assert seen["command"][-2:] == ["replay", entry.id]
    assert seen["cwd"] == Path.home()


def test_resume_uses_safe_fallback_cwd_when_launch_cwd_is_missing(monkeypatch) -> None:
    seen = {}

    def fake_popen(command, cwd, stdout, stderr, start_new_session):  # type: ignore[no-untyped-def]
        seen["command"] = command
        seen["cwd"] = cwd
        seen["stdout"] = stdout
        seen["stderr"] = stderr
        seen["start_new_session"] = start_new_session
        return subprocess.Popen  # type: ignore[return-value]

    def missing_getcwd() -> str:
        raise FileNotFoundError

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.getcwd", missing_getcwd)
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app._spawn_resume("loop-123")
    assert seen["command"][-2:] == ["resume", "loop-123"]
    assert seen["cwd"] == Path.home()


def test_missing_cwd_tui_flow_keeps_safe_cwd_for_replay_and_resume(
    monkeypatch, tmp_path: Path
) -> None:
    service = LoopService(tmp_path / "state")
    memory = MemoryStore(tmp_path / "memory")
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
    service.create_loop(run_config, loop_id="loop-123")
    entry = memory.create(
        kind="history",
        title="Replay safely",
        run_config=run_config,
        folder=tmp_path / "missing-cwd-source",
        favorite=False,
    )
    calls = []

    def fake_popen(command, cwd, stdout, stderr, start_new_session):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "command": command,
                "cwd": cwd,
                "stdout": stdout,
                "stderr": stderr,
                "start_new_session": start_new_session,
            }
        )
        return subprocess.Popen  # type: ignore[return-value]

    def missing_getcwd() -> str:
        raise FileNotFoundError

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("os.getcwd", missing_getcwd)

    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
        app.service = service
        app.memory = memory
        app.log_kind = "memory"
        app.memory_filter = "history"
        app.selected_loop_id = "loop-123"
        async with app.run_test() as pilot:
            app.refresh_data()
            await pilot.pause()
            assert app.memory_all_folders is True
            assert app._memory_scope_text() == "all-folders (cwd unavailable)"
            app.action_memory_replay()
            app.action_resume_selected()
            await pilot.pause()

    import asyncio

    asyncio.run(run_test())
    assert [call["command"][-2:] for call in calls] == [
        ["replay", entry.id],
        ["resume", "loop-123"],
    ]
    assert all(call["cwd"] == Path.home() for call in calls)


def test_selected_loop_shows_friendly_task_file_error(tmp_path: Path) -> None:
    bad_task_file = tmp_path / "bad-tasks.md"
    bad_task_file.write_text("# Loop Tasks\n\n## To do\n- None\n")
    service = LoopService(tmp_path / "state")
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
        task_file=str(bad_task_file),
    )
    state = service.create_loop(run_config, loop_id="task-loop")

    class FakeStatic:
        def __init__(self) -> None:
            self.text = ""

        def update(self, text: str) -> None:
            self.text = text

    detail = FakeStatic()
    meta = FakeStatic()
    view = FakeStatic()

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    app.selected_loop_id = state.loop_id
    app._render_summary_bar = lambda: None  # type: ignore[method-assign]
    widgets = {
        "#detail_view": detail,
        "#log_meta": meta,
        "#log_view": view,
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    app._render_selected()

    assert "❌ bad task file:" in detail.text
    assert "task-template --with-rules" in detail.text
    assert "Missing task sections: Doing, Done" in detail.text


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


def test_memory_detail_text_uses_memory_specific_empty_state(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = MemoryStore(tmp_path)
    app.log_kind = "memory"
    text = app._memory_detail_text()
    assert "memory overview" in text
    assert "no memory entry is selected" in text
    assert "press 5 to switch this view" in text.lower()


def test_memory_detail_text_uses_archived_empty_state(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = MemoryStore(tmp_path)
    app.log_kind = "memory"
    app.memory_filter = "archived"
    text = app._memory_detail_text()
    assert "memory overview" in text
    assert "no archived entries match this view" in text
    assert "press 5 to return to all entries" in text.lower()


def test_memory_query_placeholder_is_descriptive() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    assert app._memory_query_placeholder() == "memory query: title/id/label"


def test_memory_empty_state_prefers_query_clear_hint(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = MemoryStore(tmp_path)
    app.log_kind = "memory"
    app.memory_query = "nightly"
    text = app._memory_log_text().lower()
    assert "press esc to clear the query" in text


def test_memory_empty_state_prefers_label_clear_hint(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = MemoryStore(tmp_path)
    app.log_kind = "memory"
    app.memory_label = "ops"
    text = app._memory_detail_text().lower()
    assert "press c to clear the label" in text


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


def test_sync_button_state_uses_explicit_confirm_labels(tmp_path: Path) -> None:
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
        title="Confirm me",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_archive_armed = True
    app.memory_delete_armed = True
    app.delete_armed = True
    app._render_help_bar = lambda state: None  # type: ignore[method-assign]

    class FakeButton:
        def __init__(self) -> None:
            self.disabled = False
            self.label = ""

        def set_class(self, active: bool, class_name: str) -> None:
            return None

    class FakeWidget(FakeButton):
        pass

    buttons = {
        selector: FakeButton()
        for selector in [
            "#filter-running",
            "#filter-active",
            "#filter-all",
            "#log-stdout",
            "#log-stderr",
            "#log-prompt",
            "#log-events",
            "#log-memory",
            "#log-memory-favorites",
            "#log-memory-history",
            "#log-memory-presets",
            "#log-memory-archived",
            "#memory-filter-toolbar",
            "#memory-action-toolbar",
            "#memory-scope-toggle",
            "#pause",
            "#resume",
            "#stop",
            "#remove",
            "#memory-replay",
            "#memory-favorite",
            "#memory-restore",
            "#memory-archive",
            "#memory-delete",
            "#memory-query",
        ]
    }

    app.query_one = lambda selector, *_args, **_kwargs: buttons[selector]  # type: ignore[method-assign]
    app._sync_button_state()
    assert buttons["#memory-archive"].label == "z confirm archive"
    assert buttons["#memory-delete"].label == "x confirm delete"
    assert buttons["#remove"].label == "✖ Confirm delete"


def test_memory_help_text_switches_to_confirm_actions_when_armed(tmp_path: Path) -> None:
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
        title="Confirm me",
        run_config=run_config,
        folder=Path.cwd(),
        favorite=False,
    )
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = memory
    app.log_kind = "memory"
    app.memory_archive_armed = True
    app.memory_delete_armed = True
    text = app._memory_help_text(width=120)
    assert "z confirm archive" in text
    assert "x confirm delete" in text
    assert "z archive" not in text
    assert "x delete" not in text


def test_loop_help_bar_switches_to_confirm_delete_when_armed() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.delete_armed = True

    class FakeBar:
        def __init__(self) -> None:
            self.text = ""

        def update(self, text: str) -> None:
            self.text = text

    bar = FakeBar()
    app.query_one = lambda selector, *_args, **_kwargs: bar  # type: ignore[method-assign]
    state = type("State", (), {"status": "paused"})()
    app._render_help_bar(state)
    assert "d confirm delete" in bar.text
    assert "d delete" not in bar.text


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


def test_memory_archived_empty_state_mentions_archive_flow(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.memory = MemoryStore(tmp_path)
    app.memory_filter = "archived"
    text = app._memory_log_text()
    assert "No archived memory entries found." in text
    assert "z twice" in text
    assert "press 5 to return to all entries" in text.lower()


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


def test_sync_schedule_with_config_mirrors_non_scheduled_values() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeSelect:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeInput:
        def __init__(self, value: str) -> None:
            self.value = value

    widgets = {
        "#config-mode": FakeSelect("fixed"),
        "#config-interval": FakeSelect("hours"),
        "#config-interval-value": FakeInput("3"),
        "#schedule-type": FakeSelect("continuous"),
        "#schedule-every": FakeInput("0"),
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    app._sync_schedule_with_config()

    assert widgets["#schedule-type"].value == "hours"
    assert widgets["#schedule-every"].value == "3"


def test_config_status_text_distinguishes_draft_from_selected_loop(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
        steps=5,
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
    state = service.create_loop(run_config, loop_id="cfg-loop")
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service

    class FakeSelect:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeInput:
        def __init__(self, value: str) -> None:
            self.value = value

    widgets = {
        "#config-mode": FakeSelect("fixed"),
        "#config-interval": FakeSelect("minutes"),
        "#schedule-type": FakeSelect("minutes"),
        "#schedule-every": FakeInput("30"),
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    assert "Draft config" in app._config_status_text(None)
    selected_text = app._config_status_text(state)
    assert "Editing loop cfg-loop" in selected_text
    assert "schedule every 30 minutes" in selected_text


def test_workspace_scope_text_uses_editable_workspace_fields() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeSelect:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeInput:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeTextArea:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeCheckbox:
        def __init__(self, value: bool) -> None:
            self.value = value

    widgets = {
        "#workspace-root": FakeInput("/tmp/workspace"),
        "#workspace-include": FakeTextArea("src/**\ntests/**"),
        "#workspace-exclude": FakeTextArea(".git/**\nnode_modules/**\ndist/**"),
        "#safety-branch-strategy": FakeSelect("per-iteration"),
        "#schedule-type": FakeSelect("minutes"),
        "#config-interval": FakeSelect("minutes"),
        "#schedule-every": FakeInput("45"),
        "#config-quiet-hours": FakeCheckbox(True),
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    text = app._workspace_scope_text(None)

    assert "root: /tmp/workspace" in text
    assert "include: 2 patterns" in text
    assert "exclude: 3 patterns" in text
    assert "strategy: branch per iteration" in text
    assert "schedule: every 45 minutes" in text
    assert "quiet-hours: on" in text


def test_iteration_progress_text_uses_current_iteration_while_running(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
        steps=5,
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
    state = service.create_loop(run_config, loop_id="progress-loop")
    state.status = "running"
    state.current_iteration = 2
    state.completed_iterations = 1

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    text = app._iteration_progress_text(state)

    assert "Current iteration: 2 / 5" in text
    assert "Progress bar: █████░░░░░░░ 2/5" in text
    assert "1/5" not in text


def test_schedule_card_text_uses_selected_schedule_type_not_loop_mode() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeSelect:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeInput:
        def __init__(self, value: str) -> None:
            self.value = value

    widgets = {
        "#config-mode": FakeSelect("fixed"),
        "#schedule-type": FakeSelect("hours"),
        "#config-interval": FakeSelect("minutes"),
        "#schedule-every": FakeInput("6"),
        "#schedule-start-time": FakeInput("09:30"),
        "#schedule-timezone": FakeSelect("local"),
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    text = app._schedule_card_text(None)

    assert "Schedule type: every X hours" in text
    assert "Every: every 6 hours" in text
    assert "Schedule type: fixed" not in text


def test_actions_status_text_summarizes_available_controls(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
        steps=5,
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
    state = service.create_loop(run_config, loop_id="controls-loop")
    state.status = "running"

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    text = app._actions_status_text(state)

    assert text.startswith("controls-loo · running")
    assert "pause ready" in text
    assert "stop ready" in text
    assert "next blocked" in text


def test_iteration_history_text_treats_unfinished_iteration_as_running(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
        steps=5,
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
    state = service.create_loop(run_config, loop_id="history-loop")
    state.status = "running"
    state.current_iteration = 2
    state.completed_iterations = 1

    from ailoop.models import IterationRecord

    state.iterations = [
        IterationRecord(
            number=1,
            started_at="2026-05-16T10:40:01+00:00",
            finished_at="2026-05-16T10:48:13+00:00",
            duration_seconds=492,
            exit_code=0,
            success=True,
        ),
        IterationRecord(
            number=2,
            started_at="2026-05-16T11:40:01+00:00",
            duration_seconds=480,
            success=None,
        ),
    ]

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    text = app._iteration_history_card_text(state)

    assert "#2 Running" in text
    assert "#2 Failed" not in text
    assert "#2 Running · " in text
    assert ":40 · 8m 00s" in text
    assert "2026-05-16" not in text
