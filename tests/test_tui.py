import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import DataTable

from ailoop.memory import MemoryStore
from ailoop.models import IterationRecord, LoopRunConfig
from ailoop.service import LoopService
from ailoop.tui import LoopDashboard, launch_in_tmux, read_events, render_progress_text, tail_text


def test_tail_text_reads_last_lines(tmp_path: Path) -> None:
    path = tmp_path / "out.log"
    path.write_text("a\nb\nc\n")
    assert tail_text(path, lines=2) == "b\nc"


def test_read_events_reads_last_rows(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("1\n2\n3\n")
    assert read_events(path, limit=2) == "2\n3"


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


def test_loop_table_uses_iteration_and_mode_columns(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="table-loop")
    state.status = "running"
    state.current_iteration = 2
    state.completed_iterations = 1
    service.store.save(state)

    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
        app.service = service
        async with app.run_test() as pilot:
            app.refresh_data()
            await pilot.pause()
            table = app.query_one(DataTable)
            row = table.get_row("table-loop")
            assert row[0] == "table-loop"
            assert "running" in row[1]
            assert row[2] == "2/5"
            assert row[3] == "fixed"
            assert row[4] == "orchestrator"

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
    text = app._summary_bar_text(0, 0, 0, 0, 0, 0, None)
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
    text = app._summary_bar_text(0, 0, 0, 0, 0, 0, None, width=80)
    assert "all 0 · act 0 · run 0 · pause 0 · sch 0 · fail 0" in text
    assert "f running" in text
    assert f"mem all · lab 0 · sel {entry.id[:8]}" in text


def test_summary_bar_text_compacts_non_memory_mode_at_80_columns() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    text = app._summary_bar_text(0, 0, 0, 0, 0, 0, None, width=80)
    assert (
        text
        == "all 0 · act 0 · run 0 · pause 0 · sch 0 · fail 0 · f running · stdout · sel none"
    )


def test_metrics_today_text_uses_iteration_summaries_for_signal_counts(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
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
    state = service.create_loop(run_config, loop_id="metrics-loop")

    now = datetime.now(UTC)
    today = now.isoformat()
    yesterday = (now - timedelta(days=1)).isoformat()

    state.iterations = [
        IterationRecord(
            number=1,
            started_at=today,
            duration_seconds=120,
            success=True,
            summary="Modified 6 files and prepared commit. Token usage: 1200. Cost usage: $1.25.",
        ),
        IterationRecord(
            number=2,
            started_at=yesterday,
            duration_seconds=240,
            success=False,
            summary="Modified 2 files after validation failure.",
        ),
    ]
    service.store.save(state)

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service

    text = app._metrics_today_text()

    assert "Runs: 1" in text
    assert "Success rate: 100%" in text
    assert "Average runtime: 2m 00s" in text
    assert "Files modified: 6" in text
    assert "Commits created: 1" in text
    assert "Token usage: 1200" in text
    assert "Cost usage: $1.25" in text


def test_render_sidebar_stats_shows_activity_counts() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.loop_query = "review"
    app.selected_loop_id = "reliability-review"

    class FakeState:
        def __init__(self, status: str) -> None:
            self.status = status

            class run_config:
                steps = 1
                pause_seconds = 0

            self.run_config = run_config()

    class FakeStatic:
        def __init__(self) -> None:
            self.value = ""

        def update(self, value: str) -> None:
            self.value = value

    sidebar = FakeStatic()
    app.query_one = lambda *_args, **_kwargs: sidebar  # type: ignore[method-assign]

    app._render_sidebar_stats(
        [FakeState("running"), FakeState("paused"), FakeState("idle"), FakeState("failed")]
    )

    assert "visible 4 · active 3 · running 1 · paused 1 · sched 0 · fail 1" in sidebar.value
    assert "filter running · query review · selected reliability-" in sidebar.value


def test_summary_selected_text_shortens_next_run_for_wide_layout() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeState:
        loop_id = "reliability-review"
        status = "running"
        current_iteration = 2
        completed_iterations = 1

        class run_config:
            agent = "orchestrator"
            steps = 5
            pause_seconds = 1800

    app._schedule_countdown_text = lambda: "in 30 minutes"  # type: ignore[method-assign]

    text = app._summary_selected_text(FakeState(), width=140)

    assert "selected reliability-" in text
    assert "iter 2/5" in text
    assert "mode fixed" in text
    assert "next 30m" in text
    assert f"branch {app.current_branch}" in text
    assert "agent orchestrato" in text


def test_summary_selected_text_compacts_mode_and_next_run() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeState:
        loop_id = "scheduled-review"
        status = "paused"
        current_iteration = 0
        completed_iterations = 2
        dashboard_config = {
            "mode": "scheduled",
            "schedule_type": "hours",
            "schedule_every": "6",
        }

        class run_config:
            agent = "orchestrator"
            steps = 5
            pause_seconds = 3600

    app._schedule_countdown_text = lambda: "in 6 hours"  # type: ignore[method-assign]

    text = app._summary_selected_text(FakeState(), width=80)

    assert "sel scheduled-re" in text
    assert "paused" in text
    assert "iter 2/5" in text
    assert "sched" in text
    assert "next 6h" in text


def test_summary_selected_text_prefers_selected_loop_schedule_over_form_defaults() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeState:
        loop_id = "selected-scheduled"
        status = "paused"
        current_iteration = 0
        completed_iterations = 1
        dashboard_config = {
            "mode": "scheduled",
            "schedule_type": "hours",
            "schedule_every": "6",
            "schedule_start": "09:30",
        }

        class run_config:
            agent = "orchestrator"
            steps = 5
            pause_seconds = 60

    app._schedule_countdown_text = lambda: "in 1 minutes"  # type: ignore[method-assign]

    text = app._summary_selected_text(FakeState(), width=140)

    assert "next 6h" in text
    assert "next 1m" not in text


def test_loop_summary_uses_selected_loop_schedule_over_form_defaults() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeState:
        loop_id = "selected-scheduled"
        status = "idle"
        completed_iterations = 0
        current_iteration = 0
        created_at = "2026-05-16T10:40:01+00:00"
        updated_at = "2026-05-16T10:45:01+00:00"
        average_duration_seconds = 0
        last_summary = None
        dashboard_config = {
            "mode": "scheduled",
            "schedule_type": "hours",
            "schedule_every": "6",
            "schedule_start": "09:30",
        }

        class run_config:
            runner = "echo"
            agent = "orchestrator"
            steps = None
            pause_seconds = 60

    app._schedule_countdown_text = lambda: "in 1 minutes"  # type: ignore[method-assign]
    app.query_one = lambda selector, *_args, **_kwargs: {  # type: ignore[method-assign]
        "#safety-autonomy": type("S", (), {"value": "level-3"})(),
        "#workspace-branch-strategy": type("S", (), {"value": "current"})(),
    }[selector]

    text = app._loop_summary_text(FakeState())

    assert "Mode: Scheduled" in text
    assert "Next run: in 6 hours" in text
    assert "Next run: in 1 minutes" not in text


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


def test_schedule_form_uses_compact_follow_up_fields() -> None:
    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
        async with app.run_test() as pilot:
            await pilot.pause()

            schedule_type_group = app.query_one("#schedule-type").parent
            schedule_every_group = app.query_one("#schedule-every").parent
            schedule_timezone_group = app.query_one("#schedule-timezone").parent
            schedule_start_group = app.query_one("#schedule-start-time").parent

            assert schedule_type_group is not None
            assert schedule_every_group is not None
            assert schedule_timezone_group is not None
            assert schedule_start_group is not None
            assert schedule_type_group.has_class("field-group")
            assert schedule_every_group.has_class("compact-field")
            assert schedule_every_group.has_class("schedule-value-field")
            assert schedule_timezone_group.has_class("compact-field")
            assert schedule_start_group.has_class("compact-field")
            assert schedule_every_group.parent is schedule_type_group.parent
            assert schedule_start_group.parent is not schedule_type_group.parent

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
        "#workspace-branch-strategy": FakeSelect("per-iteration"),
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

    assert text.startswith("Sched: hours")
    assert "every 6" in text
    assert "next 6h" in text
    assert "Schedule type: fixed" not in text


def test_right_rail_previews_are_visible_after_render() -> None:
    async def run_test() -> None:
        app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app.query_one("#schedule-preview").has_class("detail-preview-hidden")
            assert not app.query_one("#safety-preview").has_class("detail-preview-hidden")
            assert not app.query_one("#notifications-preview").has_class("detail-preview-hidden")

    import asyncio

    asyncio.run(run_test())


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


def test_actions_status_text_marks_next_iteration_ready_when_loop_can_step(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="step-loop")
    state.status = "paused"

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service

    text = app._actions_status_text(state)

    assert text.startswith("step-loop · paused")
    assert "continue ready" in text
    assert "next ready" in text


def test_action_next_iteration_requests_single_step_and_resume(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="step-action")
    state.status = "paused"
    service.store.save(state)

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    app.selected_loop_id = state.loop_id

    seen: dict[str, str] = {}
    app._spawn_resume = lambda loop_id: seen.setdefault("loop_id", loop_id)  # type: ignore[method-assign]
    app.notify = lambda message, **_kwargs: seen.setdefault("message", message)  # type: ignore[method-assign]
    app.refresh_data = lambda: seen.setdefault("refreshed", "yes")  # type: ignore[method-assign]

    app.action_next_iteration()

    updated = service.load_loop(state.loop_id)
    assert updated.pending_single_iteration is True
    assert updated.control == "run"
    assert seen == {
        "loop_id": state.loop_id,
        "message": f"next iteration queued: {state.loop_id}",
        "refreshed": "yes",
    }


def test_action_resume_selected_resets_control_to_run(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="resume-action")
    state.status = "paused"
    state.control = "pause"
    service.store.save(state)

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    app.selected_loop_id = state.loop_id

    seen: dict[str, str] = {}
    app._spawn_resume = lambda loop_id: seen.setdefault("loop_id", loop_id)  # type: ignore[method-assign]
    app.notify = lambda message, **_kwargs: seen.setdefault("message", message)  # type: ignore[method-assign]
    app.refresh_data = lambda: seen.setdefault("refreshed", "yes")  # type: ignore[method-assign]

    app.action_resume_selected()

    updated = service.load_loop(state.loop_id)
    assert updated.control == "run"
    assert seen == {
        "loop_id": state.loop_id,
        "message": f"resume sent: {state.loop_id}",
        "refreshed": "yes",
    }


def test_action_resume_selected_blocks_scheduled_loop(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
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
    state = service.create_loop(run_config, loop_id="resume-scheduled")
    state.status = "idle"
    state.dashboard_config = {"mode": "scheduled", "schedule_type": "hours", "schedule_every": "1"}
    service.store.save(state)

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    app.selected_loop_id = state.loop_id

    seen: dict[str, str] = {}
    app._spawn_resume = lambda loop_id: seen.setdefault("loop_id", loop_id)  # type: ignore[method-assign]
    app.notify = lambda message, **_kwargs: seen.setdefault("message", message)  # type: ignore[method-assign]

    app.action_resume_selected()

    updated = service.load_loop(state.loop_id)
    assert updated.control == "run"
    assert seen == {"message": "scheduled loops wait for their configured run window"}


def test_action_run_loop_saves_scheduled_loop_without_spawning(tmp_path: Path) -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = LoopService(tmp_path)

    created: dict[str, str] = {}
    app._spawn_resume = lambda loop_id: created.setdefault("spawned", loop_id)  # type: ignore[method-assign]
    app.notify = lambda message, **_kwargs: created.setdefault("message", message)  # type: ignore[method-assign]
    app.refresh_data = lambda: created.setdefault("refreshed", "yes")  # type: ignore[method-assign]
    app._config_mode_value = lambda: "scheduled"  # type: ignore[method-assign]
    app._form_supports_run = lambda: True  # type: ignore[method-assign]
    app._build_run_config_from_form = lambda state=None: LoopRunConfig(  # type: ignore[method-assign]
        prompt="hello",
        runner="echo",
        agent="orchestrator",
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
    app._dashboard_form_values = lambda: {  # type: ignore[method-assign]
        "mode": "scheduled",
        "schedule_type": "hours",
        "schedule_every": "1",
    }
    app._workspace_form_values = lambda: {  # type: ignore[method-assign]
        "root": str(tmp_path),
        "include": "src/**",
        "exclude": ".git/**",
    }

    app.action_run_loop()

    loops = app.service.list_loops()
    assert len(loops) == 1
    assert loops[0].dashboard_config["mode"] == "scheduled"
    assert created == {
        "message": f"scheduled loop saved: {loops[0].loop_id}",
        "refreshed": "yes",
    }
    assert "spawned" not in created


def test_loop_summary_uses_saved_scheduled_mode_and_scope(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
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
    state = service.create_loop(run_config, loop_id="scheduled-loop")
    state.dashboard_config = {"mode": "scheduled", "schedule_type": "hours", "schedule_every": "6"}
    service.store.save(state)

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    app._schedule_countdown_text = lambda: "in 6 hours"  # type: ignore[method-assign]
    app.query_one = lambda selector, *_args, **_kwargs: {  # type: ignore[method-assign]
        "#safety-autonomy": type("S", (), {"value": "level-3"})(),
        "#workspace-branch-strategy": type("S", (), {"value": "current"})(),
    }[selector]

    text = app._loop_summary_text(state)

    assert "Mode: Scheduled" in text
    assert "Interval: every 6 hours" in text


def test_config_status_uses_saved_scheduled_mode(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
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
    state = service.create_loop(run_config, loop_id="scheduled-config")
    state.dashboard_config = {"mode": "scheduled", "schedule_type": "hours", "schedule_every": "6"}
    service.store.save(state)

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service

    text = app._config_status_text(state)

    assert "mode scheduled" in text
    assert "schedule every 6 hours" in text


def test_saved_scheduled_loop_reloads_scheduled_mode_into_form(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="hello",
        runner="echo",
        agent="orchestrator",
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
    state = service.create_loop(run_config, loop_id="scheduled-form")
    state.dashboard_config = {
        "mode": "scheduled",
        "schedule_type": "hours",
        "schedule_every": "6",
        "schedule_start": "09:30",
        "schedule_timezone": "utc",
    }
    service.store.save(state)

    async def run_test() -> None:
        app = LoopDashboard(
            Path("~/.config/ailoop/config.yaml").expanduser(),
            loop_id=state.loop_id,
        )
        app.service = service
        async with app.run_test() as pilot:
            app.refresh_data()
            await pilot.pause()
            assert app.query_one("#config-mode").value == "scheduled"
            assert app.query_one("#schedule-type").value == "hours"
            assert app.query_one("#schedule-every").value == "6"

    import asyncio

    asyncio.run(run_test())


def test_saved_dashboard_and_workspace_values_reload_into_forms(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    run_config = LoopRunConfig(
        prompt="saved prompt",
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
    state = service.create_loop(run_config, loop_id="saved-forms")
    state.dashboard_config = {
        "quiet_hours": True,
        "quiet_start": "21:00",
        "quiet_end": "06:00",
        "jitter": True,
        "jitter_value": "1-3",
        "schedule_type": "hours",
        "schedule_every": "6",
        "schedule_start": "09:30",
        "schedule_timezone": "utc",
        "autonomy": "level-4",
        "branch_strategy": "per-iteration",
        "notify_slack": True,
    }
    state.workspace_config = {
        "root": str(tmp_path / "workspace"),
        "include": "src/**\ndocs/**",
        "exclude": ".git/**\ndist/**",
    }
    service.store.save(state)

    async def run_test() -> None:
        app = LoopDashboard(
            Path("~/.config/ailoop/config.yaml").expanduser(),
            loop_id=state.loop_id,
        )
        app.service = service
        async with app.run_test() as pilot:
            app.refresh_data()
            await pilot.pause()
            assert app.query_one("#config-quiet-hours").value is True
            assert app.query_one("#config-quiet-start").value == "21:00"
            assert app.query_one("#config-quiet-end").value == "06:00"
            assert app.query_one("#config-jitter").value is True
            assert app.query_one("#config-jitter-value").value == "1-3"
            assert app.query_one("#schedule-type").value == "minutes"
            assert app.query_one("#schedule-every").value == "1"
            assert app.query_one("#schedule-start-time").value == "09:30"
            assert app.query_one("#schedule-timezone").value == "utc"
            assert app.query_one("#safety-autonomy").value == "level-4"
            assert app.query_one("#workspace-branch-strategy").value == "per-iteration"
            assert app.query_one("#notify-slack").value is True
            assert app.query_one("#workspace-root").value == str(tmp_path / "workspace")
            assert str(app.query_one("#workspace-current-branch").render()) == app.current_branch
            assert app.query_one("#workspace-include").text == "src/**\ndocs/**"
            assert app.query_one("#workspace-exclude").text == ".git/**\ndist/**"

    import asyncio

    asyncio.run(run_test())


def test_restart_actions_clear_pending_single_iteration(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="restart-step")
    state.status = "paused"
    state.pending_single_iteration = True
    service.store.save(state)

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app.service = service
    app.selected_loop_id = state.loop_id
    app._build_run_config_from_form = lambda current_state=None: (  # type: ignore[method-assign]
        current_state.run_config if current_state is not None else run_config
    )
    app._dashboard_form_values = lambda: {}  # type: ignore[method-assign]
    app._workspace_form_values = lambda: {}  # type: ignore[method-assign]
    app._spawn_resume = lambda _loop_id: None  # type: ignore[method-assign]
    app.notify = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    app.refresh_data = lambda: None  # type: ignore[method-assign]

    app.action_restart_selected()
    restarted = service.load_loop(state.loop_id)
    assert restarted.pending_single_iteration is False

    restarted.pending_single_iteration = True
    service.store.save(restarted)
    app.action_restart_reset_selected()
    reset = service.load_loop(state.loop_id)
    assert reset.pending_single_iteration is False


def test_safety_card_text_compacts_preview_summary() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeSelect:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeInput:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeCheckbox:
        def __init__(self, value: bool) -> None:
            self.value = value

    widgets = {
        "#safety-autonomy": FakeSelect("level-4"),
        "#workspace-branch-strategy": FakeSelect("per-iteration"),
        "#safety-ask-before-commit": FakeCheckbox(True),
        "#safety-ask-before-push": FakeCheckbox(False),
        "#safety-auto-commit": FakeCheckbox(True),
        "#safety-auto-push": FakeCheckbox(False),
        "#safety-create-backup-branch": FakeCheckbox(True),
        "#safety-auto-stop-on-limit": FakeCheckbox(True),
        "#safety-max-runtime": FakeInput("4h"),
        "#safety-max-files-changed": FakeInput("100"),
        "#safety-max-commits": FakeInput("10"),
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    text = app._safety_card_text(None)

    assert text.startswith("Safety: Level 4 Edit + Commit")
    assert "limits 4h/100/10" in text
    assert "Autonomy level:" not in text


def test_notifications_text_compacts_preview_summary() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeCheckbox:
        def __init__(self, value: bool) -> None:
            self.value = value

    widgets = {
        "#notify-start": FakeCheckbox(True),
        "#notify-success": FakeCheckbox(True),
        "#notify-failure": FakeCheckbox(True),
        "#notify-limit": FakeCheckbox(True),
        "#notify-complete": FakeCheckbox(False),
        "#notify-terminal": FakeCheckbox(True),
        "#notify-slack": FakeCheckbox(False),
        "#notify-email": FakeCheckbox(False),
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    text = app._notifications_text()

    assert text.startswith("Notify: start on")
    assert "chan T on/S off/E off" in text
    assert "Channels:" not in text


def test_ops_snapshot_text_compacts_to_two_summary_lines() -> None:
    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())

    class FakeState:
        pass

    class FakeSelect:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeInput:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeCheckbox:
        def __init__(self, value: bool) -> None:
            self.value = value

    widgets = {
        "#schedule-type": FakeSelect("minutes"),
        "#config-interval": FakeSelect("minutes"),
        "#schedule-every": FakeInput("30"),
        "#schedule-start-time": FakeInput("Now"),
        "#schedule-timezone": FakeSelect("local"),
        "#safety-autonomy": FakeSelect("level-3"),
        "#workspace-branch-strategy": FakeSelect("current"),
        "#safety-ask-before-commit": FakeCheckbox(True),
        "#safety-ask-before-push": FakeCheckbox(True),
        "#safety-auto-commit": FakeCheckbox(False),
        "#safety-auto-push": FakeCheckbox(False),
        "#safety-max-runtime": FakeInput("4h"),
        "#safety-max-files-changed": FakeInput("100"),
        "#safety-max-commits": FakeInput("10"),
        "#notify-start": FakeCheckbox(True),
        "#notify-success": FakeCheckbox(True),
        "#notify-failure": FakeCheckbox(True),
        "#notify-limit": FakeCheckbox(True),
        "#notify-complete": FakeCheckbox(True),
        "#notify-terminal": FakeCheckbox(True),
        "#notify-slack": FakeCheckbox(False),
        "#notify-email": FakeCheckbox(False),
    }
    app.query_one = lambda selector, *_args, **_kwargs: widgets[selector]  # type: ignore[method-assign]

    text = app._ops_snapshot_text(FakeState())
    lines = text.splitlines()

    assert lines[0] == "[b][#4ea3ff]OPS SNAPSHOT[/][/]"
    assert len(lines) == 3
    assert "Sched 30m" in lines[1]
    assert "Safe L3 current" in lines[1]
    assert "C/P on/on" in lines[2]
    assert "ch on/off/off" in lines[2]


def test_loop_summary_text_compacts_metadata_lines(tmp_path: Path) -> None:
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
    state = service.create_loop(run_config, loop_id="summary-loop")
    state.status = "running"
    state.completed_iterations = 2
    state.current_iteration = 2
    state.average_duration_seconds = 483
    state.last_summary = "Modified 6 files and passing tests."

    app = LoopDashboard(Path("~/.config/ailoop/config.yaml").expanduser())
    app._schedule_countdown_text = lambda: "in 30 minutes"  # type: ignore[method-assign]

    text = app._loop_summary_text(state)

    assert "Branch/Autonomy: current branch · Level 3 Edit" in text
    assert "Runner/Agent: echo · orchestrator" in text
    assert "Updated/Avg:" in text
    assert "Branch strategy:" not in text
    assert "\nAutonomy:" not in text
    assert "\nRunner:" not in text
    assert "\nAgent:" not in text
    assert "Avg runtime:" not in text


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
