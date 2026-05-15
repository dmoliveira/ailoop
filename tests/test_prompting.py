from pathlib import Path

from ailoop.models import LoopRunConfig, LoopState
from ailoop.prompting import build_prompt


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
