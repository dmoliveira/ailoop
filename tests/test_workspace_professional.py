from pathlib import Path

import pytest

from ailoop.models import IterationRecord, LoopRunConfig, LoopState
from ailoop.service import LoopService
from ailoop.workspace_history import WorkspaceHistoryEntry, WorkspaceHistoryStore


def build_run_config() -> LoopRunConfig:
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
    )


def test_loop_state_ignores_unknown_persisted_fields() -> None:
    config = build_run_config().to_dict()
    config["future_setting"] = "ignored"
    state = LoopState.from_dict(
        {
            "loop_id": "future-loop",
            "created_at": "now",
            "updated_at": "now",
            "status": "idle",
            "control": "run",
            "run_config": config,
            "iterations": [{"number": 1, "started_at": "now", "future_field": True}],
            "future_field": "ignored",
        }
    )
    assert state.run_config.prompt == "hello"
    assert state.iterations == [IterationRecord(number=1, started_at="now")]


def test_queue_follow_up_rejects_committed_iteration(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(build_run_config(), loop_id="committed-loop")
    service.request_single_iteration(state.loop_id)
    with pytest.raises(RuntimeError, match="pending iteration"):
        service.queue_follow_up(state.loop_id, "cannot swap this")


def test_workspace_history_reads_only_tail(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    history = WorkspaceHistoryStore(tmp_path / "state")
    for index in range(5):
        config = build_run_config()
        config.workspace_root = str(workspace)
        config.prompt = f"prompt {index}"
        history.append_prompt("loop", config)

    def fail_read_text(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("full history read is not allowed")

    history_path = next((tmp_path / "state" / "workspaces").glob("*/prompt-history.jsonl"))
    monkeypatch.setattr(type(history_path), "read_text", fail_read_text)
    assert [entry.prompt for entry in history.recent_entries(str(workspace), limit=2)] == [
        "prompt 3",
        "prompt 4",
    ]


def test_queue_follow_up_can_atomically_request_next_iteration(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    state = service.create_loop(build_run_config(), loop_id="atomic-follow-up")
    queued = service.queue_follow_up(state.loop_id, "run next", run_next=True)
    assert queued.queued_follow_up == "run next"
    assert queued.pending_single_iteration is True


def test_workspace_history_entry_ignores_unknown_persisted_fields() -> None:
    entry = WorkspaceHistoryEntry.from_dict(
        {
            "recorded_at": "now",
            "workspace_root": "/workspace",
            "workspace_hash": "hash",
            "loop_id": "loop",
            "kind": "prompt",
            "prompt": "hello",
            "future_field": "ignored",
        }
    )
    assert entry.prompt == "hello"
