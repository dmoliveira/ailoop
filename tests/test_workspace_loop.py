from pathlib import Path

import pytest

from ailoop.models import LoopRunConfig
from ailoop.service import LoopService


def build_run_config(*, workspace_root: str | None = None) -> LoopRunConfig:
    return LoopRunConfig(
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
        workspace_root=workspace_root,
    )


def test_clear_follow_up_rejects_pending_single_iteration(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(build_run_config(), loop_id="pending-clear-follow-up")
    service.queue_follow_up(state.loop_id, "committed prompt")
    service.request_single_iteration(state.loop_id)
    with pytest.raises(RuntimeError, match="Cannot clear"):
        service.clear_follow_up(state.loop_id)


def test_workspace_root_runs_child_in_selected_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = LoopService(tmp_path / "state")
    config = build_run_config(workspace_root=str(workspace))
    config.steps = 1
    config.runner_args = ["-c", "import os; print(os.getcwd())"]
    state = service.create_loop(config, loop_id="workspace-cwd")
    final_state = service.run_loop(state.loop_id)
    stdout_log = Path(final_state.iterations[-1].stdout_log or "")
    assert stdout_log.read_text().strip() == str(workspace.resolve())


def test_follow_up_is_consumed_once_and_recorded_in_workspace_history(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = LoopService(tmp_path / "state")
    config = build_run_config(workspace_root=str(workspace))
    config.steps = 1
    state = service.create_loop(config, loop_id="follow-up-once")
    service.queue_follow_up(state.loop_id, "focus on the regression")
    final_state = service.run_loop(state.loop_id)
    assert final_state.queued_follow_up is None
    prompt_log = Path(final_state.iterations[-1].prompt_file or "")
    assert "focus on the regression" in prompt_log.read_text()
    entries = service.workspace_history.recent_entries(str(workspace), limit=10, max_chars=5000)
    assert {entry.kind for entry in entries} == {"prompt", "follow_up", "result"}


def test_single_iteration_rejects_locked_loop(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(build_run_config(), loop_id="locked-single-iteration")
    with service.store.acquire_lock(state.loop_id):
        with pytest.raises(RuntimeError, match="Loop is already active"):
            service.request_single_iteration(state.loop_id)


def test_run_without_workspace_root_does_not_create_workspace_history(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    config = build_run_config()
    config.steps = 1
    state = service.create_loop(config, loop_id="isolated-cli-style-run")
    service.run_loop(state.loop_id)
    assert not (tmp_path / "state" / "workspaces").exists()
