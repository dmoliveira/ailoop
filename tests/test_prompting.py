from pathlib import Path

from ailoop.models import LoopRunConfig, LoopState
from ailoop.prompting import build_prompt
from ailoop.workspace_history import WorkspaceHistoryEntry


def test_build_prompt_includes_sections(tmp_path: Path) -> None:
    agent_file = tmp_path / "AGENTS.md"
    agent_file.write_text("Use orchestrator")
    state = LoopState(
        loop_id="abc123",
        created_at="now",
        updated_at="now",
        status="idle",
        control="run",
        run_config=LoopRunConfig(
            prompt="review repo",
            runner="opencode",
            agent="orchestrator",
            steps=1,
            pause_seconds=0,
            continue_on_error=True,
            retry_count=0,
            pre_prompt_enabled=True,
            attach_agent_file=True,
            pre_prompt="Be safe.",
            agent_file=str(agent_file),
            task_file=str(tmp_path / "tasks.md"),
            stop_when_tasks_complete=True,
            max_doing=1,
            runner_command="opencode",
            runner_args=["run", "{prompt}"],
        ),
        last_summary="previous",
    )
    prompt = build_prompt(state, 1)
    assert "Be safe." in prompt
    assert "Use orchestrator" in prompt
    assert "review repo" in prompt
    assert "previous" in prompt
    assert "Task file:" in prompt


def test_build_prompt_includes_workspace_follow_up_and_recent_history(tmp_path: Path) -> None:
    state = LoopState(
        loop_id="abc-history",
        created_at="now",
        updated_at="now",
        status="idle",
        control="run",
        run_config=LoopRunConfig(
            prompt="review repo",
            runner="opencode",
            agent="orchestrator",
            steps=1,
            pause_seconds=0,
            continue_on_error=True,
            retry_count=0,
            pre_prompt_enabled=False,
            attach_agent_file=False,
            pre_prompt="",
            agent_file=None,
            task_file=None,
            stop_when_tasks_complete=False,
            max_doing=1,
            runner_command="opencode",
            runner_args=["run", "{prompt}"],
            workspace_root=str(tmp_path),
        ),
        queued_follow_up="continue from the latest failure",
        last_summary="previous",
    )
    prompt = build_prompt(
        state,
        2,
        recent_workspace_history=[
            WorkspaceHistoryEntry(
                recorded_at="now",
                workspace_root=str(tmp_path),
                workspace_hash="abc",
                loop_id="abc-history",
                kind="follow_up",
                prompt="fix the lint issue",
            )
        ],
    )
    assert f"- root: {tmp_path}" in prompt
    assert "continue from the latest failure" in prompt
    assert "Recent workspace history:" in prompt
    assert "fix the lint issue" in prompt
