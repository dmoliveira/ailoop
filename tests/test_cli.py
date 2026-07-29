from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ailoop.cli import _read_log_content, _read_log_excerpt, main
from ailoop.memory import MemoryIntegrityError, MemoryStorageError, MemoryStore
from ailoop.models import LoopRunConfig, LoopState
from ailoop.service import LoopService


def write_test_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: {tmp_path / "state"}
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: null
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {{}}
""".strip()
    )
    return config_path


def test_task_template_command_prints_template(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["ailoop", "task-template"])
    main()
    out = capsys.readouterr().out
    assert "# Loop Tasks" in out
    assert "Task file rules:" not in out


def test_key_subcommand_help_includes_examples(capsys, monkeypatch) -> None:
    for argv in (
        ["ailoop", "resume", "--help"],
        ["ailoop", "replay", "--help"],
        ["ailoop", "memory", "save", "--help"],
        ["ailoop", "memory", "list", "--help"],
        ["ailoop", "memory", "show", "--help"],
        ["ailoop", "tui", "--help"],
    ):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Examples:" in out


def test_resume_command_resets_persisted_pause_and_runs(monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    service = LoopService(tmp_path / "state")
    state = service.create_loop(
        LoopRunConfig(
            prompt="resume me",
            runner="test",
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
        ),
        loop_id="cli-resume",
    )
    service.request_control(state.loop_id, "pause")
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--quiet", "--config", str(config_path), "resume", state.loop_id],
    )

    main()

    resumed = service.load_loop(state.loop_id)
    assert resumed.status == "completed"
    assert resumed.control == "run"
    assert resumed.completed_iterations == 1


def test_task_template_with_rules_prints_guide(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["ailoop", "task-template", "--with-rules"])
    main()
    out = capsys.readouterr().out
    assert "# Loop Tasks" in out
    assert "Task file rules:" in out


def test_init_task_file_and_check_task_file(capsys, monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    monkeypatch.setattr("sys.argv", ["ailoop", "init-task-file", str(path), "--force"])
    main()
    out = capsys.readouterr().out
    assert "wrote task file" in out

    monkeypatch.setattr("sys.argv", ["ailoop", "check-task-file", str(path)])
    main()
    out = capsys.readouterr().out
    assert "⏳ open" in out


def test_check_task_file_uses_configured_max_doing(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: {tmp_path / "state"}
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: null
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 2
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {{}}
""".strip()
    )
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n"
        "## To do\n- None\n\n"
        "## Doing\n- [ ] First task\n- [ ] Second task\n\n"
        "## Done\n- None\n"
    )

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "check-task-file", str(path)],
    )
    main()
    out = capsys.readouterr().out
    assert "⏳ open" in out


def test_memory_save_list_show_and_edit(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Quick review",
            "Review the repo",
            "--runner",
            "test",
            "--label",
            "demo",
            "--favorite",
        ],
    )
    main()
    saved = json.loads(capsys.readouterr().out)
    assert saved["title"] == "Quick review"
    assert saved["favorite"] is True
    entry_id = saved["id"]

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "list", "--kind", "preset"],
    )
    main()
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["id"] == entry_id

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "edit",
            entry_id,
            "--title",
            "Quick review v2",
            "--prompt",
            "Review the repo again",
            "--label",
            "updated",
            "--no-favorite",
            "--change-note",
            "refresh",
        ],
    )
    main()
    edited = json.loads(capsys.readouterr().out)
    assert edited["title"] == "Quick review v2"
    assert edited["favorite"] is False
    assert edited["current"]["prompt"] == "Review the repo again"
    assert edited["latest_version"] == 2

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "show", entry_id],
    )
    main()
    shown = capsys.readouterr().out
    assert "Quick review v2" in shown
    assert "prompt: Review the repo again" in shown

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "list", "--kind", "preset"],
    )
    main()
    listed_text = capsys.readouterr().out
    assert "Used" in listed_text
    assert "Labels" in listed_text


def test_memory_edit_patches_latest_snapshot_without_defaults(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = write_test_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    memory = MemoryStore(tmp_path / "state")
    original = memory.create(
        kind="preset",
        title="Patch me",
        folder=tmp_path,
        run_config=LoopRunConfig(
            prompt="original prompt",
            runner="test",
            agent=None,
            steps=7,
            pause_seconds=9,
            continue_on_error=True,
            retry_count=0,
            pre_prompt_enabled=True,
            attach_agent_file=True,
            pre_prompt="",
            agent_file=None,
            runner_command="python3",
            runner_args=["-c", "print('ok')"],
            task_file=None,
            stop_when_tasks_complete=False,
            workspace_root=None,
            workspace_history_enabled=False,
            workspace_history_limit=13,
            workspace_history_chars=2400,
        ),
        token_ref="token-1",
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "edit",
            original.id,
            "--prompt",
            "patched prompt",
        ],
    )
    main()
    first = json.loads(capsys.readouterr().out)

    assert first["latest_version"] == 2
    assert first["versions"][0]["prompt"] == "original prompt"
    assert first["current"] == first["versions"][-1]
    assert first["current"]["prompt"] == "patched prompt"
    assert first["token_ref"] == first["current"]["token_ref"] == "token-1"
    for field in (
        "runner",
        "agent",
        "steps",
        "pause_seconds",
        "task_file",
        "until_tasks_complete",
        "no_pre_prompt",
        "no_agent_file",
        "agent_file",
        "workspace_root",
        "workspace_history_enabled",
        "workspace_history_limit",
        "workspace_history_chars",
        "token_ref",
    ):
        assert first["current"][field] == first["versions"][0][field]

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "edit",
            original.id,
            "--steps",
            "3",
        ],
    )
    main()
    second = json.loads(capsys.readouterr().out)
    assert second["latest_version"] == 3
    assert [item["version"] for item in second["versions"]] == [1, 2, 3]
    assert second["current"]["prompt"] == "patched prompt"
    assert second["current"]["steps"] == 3

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "edit",
            original.id,
            "--title",
            "Metadata only",
        ],
    )
    main()
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["latest_version"] == 3
    assert len(metadata["versions"]) == 3

    path = tmp_path / "state" / "memory" / "presets" / f"{original.id}.json"
    before_invalid_edit = path.read_bytes()
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "edit",
            original.id,
            "--runner",
            "missing",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert path.read_bytes() == before_invalid_edit

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "edit",
            original.id,
        ],
    )
    with pytest.raises(SystemExit) as empty_exc:
        main()
    assert empty_exc.value.code == 1
    assert "No memory changes requested" in capsys.readouterr().out
    assert path.read_bytes() == before_invalid_edit


def test_memory_edit_failure_preserves_original_bytes(monkeypatch, tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    entry = memory.create(
        kind="preset",
        title="Atomic",
        folder=tmp_path,
        run_config=LoopRunConfig(
            prompt="before",
            runner="test",
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
            runner_args=[],
        ),
    )
    path = tmp_path / "memory" / "presets" / f"{entry.id}.json"
    original = path.read_bytes()

    def fail_replace(*_args) -> None:  # type: ignore[no-untyped-def]
        raise OSError("fail")

    monkeypatch.setattr("ailoop.memory.os.replace", fail_replace)

    with pytest.raises(OSError, match="fail"):
        memory.edit(entry.id, snapshot_updates={"prompt": "after"}, folder=tmp_path)

    assert path.read_bytes() == original
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_memory_postcommit_directory_fsync_failure_does_not_invite_retry(
    monkeypatch, tmp_path: Path
) -> None:
    memory = MemoryStore(tmp_path)
    entry = memory.create(
        kind="preset",
        title="Committed",
        folder=tmp_path,
        run_config=LoopRunConfig(
            prompt="before",
            runner="test",
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
            runner_args=[],
        ),
    )
    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory fsync unavailable")
        original_fsync(descriptor)

    monkeypatch.setattr("ailoop.memory.os.fsync", fail_directory_fsync)

    updated = memory.edit(
        entry.id,
        snapshot_updates={"prompt": "after"},
        folder=tmp_path,
    )

    assert updated.latest_version == 2
    assert memory.load(entry.id, folder=tmp_path).current.prompt == "after"


def test_concurrent_memory_usage_updates_are_serialized(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    entry = memory.create(
        kind="preset",
        title="Usage",
        folder=tmp_path,
        run_config=LoopRunConfig(
            prompt="count",
            runner="test",
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
            runner_args=[],
        ),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: memory.mark_used(entry.id, folder=tmp_path), range(20)))

    assert memory.load(entry.id, folder=tmp_path).use_count == 20


def test_concurrent_disjoint_memory_patches_both_survive(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path)
    entry = memory.create(
        kind="preset",
        title="Patches",
        folder=tmp_path,
        run_config=LoopRunConfig(
            prompt="before",
            runner="test",
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
            runner_args=[],
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                memory.edit,
                entry.id,
                snapshot_updates=updates,
                folder=tmp_path,
            )
            for updates in ({"prompt": "after"}, {"steps": 4})
        ]
        for future in futures:
            future.result()

    updated = memory.load(entry.id, folder=tmp_path)
    assert updated.current.prompt == "after"
    assert updated.current.steps == 4
    assert updated.latest_version == 3
    assert [snapshot.version for snapshot in updated.versions] == [1, 2, 3]


def test_memory_list_skips_corrupt_entries(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    memory = MemoryStore(tmp_path / "state")
    good = memory.create(
        kind="preset",
        title="Good entry",
        run_config=LoopRunConfig(
            prompt="hello",
            runner="test",
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
        ),
        folder=Path.cwd(),
    )
    (tmp_path / "state" / "memory" / "presets" / "broken.json").write_text("{not-json")

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "list", "--kind", "preset"],
    )
    main()
    listed = json.loads(capsys.readouterr().out)

    assert [entry["id"] for entry in listed] == [good.id]


def test_invalid_numeric_config_exits_cleanly(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    config_path.write_text(config_path.read_text().replace("pause_seconds: 0", "pause_seconds: -1"))

    monkeypatch.setattr("sys.argv", ["ailoop", "--config", str(config_path), "list"])
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert "Invalid configuration: loop.pause_seconds must be >= 0" in capsys.readouterr().out


def test_memory_favorite_and_delete(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "History entry",
            "Do a run",
            "--kind",
            "history",
        ],
    )
    main()
    saved = json.loads(capsys.readouterr().out)
    entry_id = saved["id"]

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "favorite", entry_id],
    )
    main()
    favorited = json.loads(capsys.readouterr().out)
    assert favorited["favorite"] is True

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "delete", entry_id],
    )
    main()
    deleted = json.loads(capsys.readouterr().out)
    assert deleted == {"ok": True, "deleted": entry_id}

    store = MemoryStore(tmp_path / "state")
    try:
        store.load(entry_id)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("entry should be deleted")


def test_memory_archive_list_and_restore(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Archive me",
            "Keep for later",
        ],
    )
    main()
    saved = json.loads(capsys.readouterr().out)
    entry_id = saved["id"]

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "archive", entry_id],
    )
    main()
    archived = json.loads(capsys.readouterr().out)
    assert archived["archived"] is True

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "list", "--archived"],
    )
    main()
    listed = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in listed] == [entry_id]

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "archive", entry_id, "--off"],
    )
    main()
    restored = json.loads(capsys.readouterr().out)
    assert restored["archived"] is False

    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "list"],
    )
    main()
    default_list = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in default_list] == [entry_id]


def test_memory_list_filters_by_label(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Tagged One",
            "Keep for later",
            "--label",
            "ops",
            "--label",
            "nightly",
        ],
    )
    main()
    tagged = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Tagged Two",
            "Something else",
            "--label",
            "ops",
        ],
    )
    main()
    json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "list",
            "--label",
            "ops",
            "--label",
            "nightly",
        ],
    )
    main()
    listed = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in listed] == [tagged["id"]]


def test_memory_list_filters_by_query(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Nightly Ops",
            "Keep for later",
            "--label",
            "ops",
        ],
    )
    main()
    first = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Docs Sweep",
            "Something else",
            "--label",
            "docs",
        ],
    )
    main()
    json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "list",
            "--query",
            "nightly",
        ],
    )
    main()
    listed = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in listed] == [first["id"]]


def test_memory_list_filters_to_favorites_in_current_folder(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = write_test_config(tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Favorite entry",
            "Keep for later",
            "--favorite",
        ],
    )
    main()
    favorite = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Regular entry",
            "Something else",
        ],
    )
    main()
    json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "list",
            "--favorites",
        ],
    )
    main()
    listed = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in listed] == [favorite["id"]]


def test_memory_list_all_folders_expands_scope(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    first_dir = tmp_path / "project-one"
    second_dir = tmp_path / "project-two"
    first_dir.mkdir()
    second_dir.mkdir()

    monkeypatch.chdir(first_dir)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "First entry",
            "Keep for later",
        ],
    )
    main()
    first = json.loads(capsys.readouterr().out)

    monkeypatch.chdir(second_dir)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Second entry",
            "Something else",
        ],
    )
    main()
    second = json.loads(capsys.readouterr().out)

    monkeypatch.chdir(first_dir)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "list",
        ],
    )
    main()
    local_only = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in local_only] == [first["id"]]

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "list",
            "--all-folders",
        ],
    )
    main()
    expanded = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in expanded] == [second["id"], first["id"]]


def test_memory_list_empty_output_includes_scope_guidance(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = write_test_config(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "list"],
    )
    main()
    out = capsys.readouterr().out
    assert "No memory entries found." in out
    assert "scope: current folder" in out
    assert "ailoop memory list --all-folders" in out
    assert 'ailoop memory save "Quick review" "Review the repo"' in out


@pytest.mark.parametrize("entry_id", ["missing-entry", "../escape"])
def test_memory_show_missing_entry_is_friendly(
    entry_id: str, capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = write_test_config(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "show", entry_id],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"Memory entry not found: {entry_id}" in out
    assert "ailoop memory list --all-folders" in out


def test_memory_storage_error_is_reported_without_traceback(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = write_test_config(tmp_path)
    monkeypatch.setattr(
        "ailoop.memory.MemoryStore.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MemoryStorageError("unsafe storage")),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "show", "a" * 12],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert capsys.readouterr().out == "Storage error: unsafe storage\n"


def test_memory_integrity_error_is_reported_without_traceback(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = write_test_config(tmp_path)
    monkeypatch.setattr(
        "ailoop.memory.MemoryStore.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MemoryIntegrityError("ambiguous entry")),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "show", "a" * 12],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert capsys.readouterr().out == "Memory integrity error: ambiguous entry\n"


def test_unclassified_os_error_is_not_mislabeled_as_storage(monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    monkeypatch.setattr(
        "ailoop.memory.MemoryStore.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unclassified")),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "show", "a" * 12],
    )

    with pytest.raises(OSError, match="unclassified"):
        main()


def test_status_missing_loop_is_friendly(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "status", "missing-loop"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Loop not found: missing-loop" in out
    assert "ailoop list" in out


def test_replay_uses_saved_entry_and_marks_used(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Replay me",
            "Review the repo",
            "--runner",
            "test",
        ],
    )
    main()
    saved = json.loads(capsys.readouterr().out)
    entry_id = saved["id"]

    seen: dict[str, object] = {}

    def fake_create_loop(self, run_config, loop_id=None):  # type: ignore[no-untyped-def]
        seen["run_config"] = run_config
        return LoopState(
            loop_id=loop_id or "replayed-loop",
            created_at="now",
            updated_at="now",
            status="idle",
            control="run",
            run_config=run_config,
        )

    def fake_run_loop(self, loop_id):  # type: ignore[no-untyped-def]
        run_config = seen["run_config"]
        assert isinstance(run_config, LoopRunConfig)
        return LoopState(
            loop_id=loop_id,
            created_at="now",
            updated_at="now",
            status="completed",
            control="run",
            run_config=run_config,
        )

    monkeypatch.setattr("ailoop.service.LoopService.create_loop", fake_create_loop)
    monkeypatch.setattr("ailoop.service.LoopService.run_loop", fake_run_loop)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "replay",
            entry_id,
            "--loop-id",
            "replay-1",
        ],
    )
    main()
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["loop_id"] == "replay-1"
    run_config = seen["run_config"]
    assert isinstance(run_config, LoopRunConfig)
    assert run_config.prompt == "Review the repo"

    store = MemoryStore(tmp_path / "state")
    entry = store.load(entry_id)
    assert entry.use_count == 1


def test_memory_show_rejects_out_of_scope_entry(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    source = tmp_path / "source"
    other = tmp_path / "other"
    source.mkdir()
    other.mkdir()

    monkeypatch.chdir(source)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "memory", "save", "Scoped", "Only here"],
    )
    main()
    saved = json.loads(capsys.readouterr().out)

    monkeypatch.chdir(other)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "memory", "show", saved["id"]],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"Memory entry not found: {saved['id']}" in out
    assert "ailoop memory list --all-folders" in out


def test_replay_failure_does_not_mark_used(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "memory",
            "save",
            "Replay me",
            "Review the repo",
        ],
    )
    main()
    saved = json.loads(capsys.readouterr().out)
    entry_id = saved["id"]

    def fake_create_loop(self, run_config, loop_id=None):  # type: ignore[no-untyped-def]
        return LoopState(
            loop_id=loop_id or "replay-fail",
            created_at="now",
            updated_at="now",
            status="idle",
            control="run",
            run_config=run_config,
        )

    def fake_run_loop(self, loop_id):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"boom: {loop_id}")

    monkeypatch.setattr("ailoop.service.LoopService.create_loop", fake_create_loop)
    monkeypatch.setattr("ailoop.service.LoopService.run_loop", fake_run_loop)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--config", str(config_path), "replay", entry_id],
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "boom" in capsys.readouterr().out

    store = MemoryStore(tmp_path / "state")
    entry = store.load(entry_id)
    assert entry.use_count == 0


def test_check_task_file_verbose(capsys, monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- [ ] First task\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    monkeypatch.setattr("sys.argv", ["ailoop", "--verbose", "check-task-file", str(path)])
    main()
    out = capsys.readouterr().out
    assert "To do:" in out
    assert "- First task" in out


def test_check_task_file_quiet(capsys, monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- [ ] First task\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    monkeypatch.setattr("sys.argv", ["ailoop", "--quiet", "check-task-file", str(path)])
    main()
    out = capsys.readouterr().out
    assert out == ""


def test_check_task_file_bad_file_is_friendly(capsys, monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("# Loop Tasks\n\n## To do\n- bad\n\n## Doing\n- None\n\n## Done\n- None\n")
    monkeypatch.setattr("sys.argv", ["ailoop", "check-task-file", str(path)])
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 1
    out = capsys.readouterr().out
    assert "bad task file" in out
    assert "line 4" in out
    assert "content: - bad" in out
    assert "task-template --with-rules" in out


def test_check_task_file_json(capsys, monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- [ ] First task\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    monkeypatch.setattr("sys.argv", ["ailoop", "--json", "check-task-file", str(path)])
    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True
    assert data["todo_count"] == 1


def test_check_task_file_json_error(capsys, monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("## To do\n- None\n")
    monkeypatch.setattr("sys.argv", ["ailoop", "--json", "check-task-file", str(path)])
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 1
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is False
    assert "error" in data


def test_check_task_file_bad_checkbox_spacing_is_friendly(
    capsys,
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad.md"
    path.write_text("# Loop Tasks\n\n## To do\n- [ ]Task\n\n## Doing\n- None\n\n## Done\n- None\n")
    monkeypatch.setattr("sys.argv", ["ailoop", "check-task-file", str(path)])
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 1
    out = capsys.readouterr().out
    assert "line 4" in out
    assert "Invalid task line in To do" in out


def test_tail_command_reads_last_lines(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    loop_dir = tmp_path / "state" / "loop-tail" / "logs"
    loop_dir.mkdir(parents=True)
    (tmp_path / "state" / "loop-tail" / "state.json").write_text(
        json.dumps(
            {
                "loop_id": "loop-tail",
                "created_at": "now",
                "updated_at": "now",
                "status": "completed",
                "control": "run",
                "run_config": {
                    "prompt": "x",
                    "runner": "test",
                    "agent": "orchestrator",
                    "steps": 1,
                    "pause_seconds": 0,
                    "continue_on_error": True,
                    "retry_count": 0,
                    "pre_prompt_enabled": False,
                    "attach_agent_file": False,
                    "pre_prompt": "",
                    "agent_file": None,
                    "runner_command": "python3",
                    "runner_args": ["-c", "print('ok')"],
                    "runner_env": {},
                    "task_file": None,
                    "stop_when_tasks_complete": False,
                    "max_doing": 1,
                },
                "current_iteration": 1,
                "completed_iterations": 1,
                "last_exit_code": 0,
                "consecutive_failures": 0,
                "total_duration_seconds": 0.0,
                "average_duration_seconds": 0.0,
                "last_summary": "ok",
                "iterations": [
                    {
                        "number": 1,
                        "started_at": "now",
                        "finished_at": "now",
                        "duration_seconds": 0.0,
                        "exit_code": 0,
                        "success": True,
                        "stdout_log": str(loop_dir / "iteration-0001.stdout.log"),
                        "stderr_log": str(loop_dir / "iteration-0001.stderr.log"),
                        "prompt_file": str(loop_dir / "iteration-0001.prompt.txt"),
                        "summary": "ok",
                    }
                ],
            }
        )
    )
    (loop_dir / "iteration-0001.stdout.log").write_bytes(b"a\xff\nb\xfe\nc\n")

    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--config",
            str(config_path),
            "tail",
            "loop-tail",
            "--kind",
            "stdout",
            "-n",
            "2",
        ],
    )

    from ailoop.service import LoopService as RealLoopService

    real_init = RealLoopService.__init__

    def fake_init(self, state_root, emit_output=True):  # type: ignore[no-untyped-def]
        return real_init(self, tmp_path / "state", emit_output=emit_output)

    monkeypatch.setattr("ailoop.service.LoopService.__init__", fake_init)
    main()

    assert capsys.readouterr().out == "b�\nc\n"


def test_log_readers_keep_prompt_decoding_strict(tmp_path: Path) -> None:
    path = tmp_path / "prompt.txt"
    path.write_bytes(b"prompt\xff\n")

    with pytest.raises(UnicodeDecodeError):
        _read_log_excerpt(path, 1, kind="prompt")
    with pytest.raises(UnicodeDecodeError):
        _read_log_content(path, kind="prompt")


def test_check_task_file_state_exit_codes(capsys, monkeypatch, tmp_path: Path) -> None:
    done_path = tmp_path / "done.md"
    done_path.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- None\n\n## Done\n- [x] Done\n"
    )
    monkeypatch.setattr(
        "sys.argv", ["ailoop", "check-task-file", str(done_path), "--state-exit-code"]
    )
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0

    open_path = tmp_path / "open.md"
    open_path.write_text(
        "# Loop Tasks\n\n## To do\n- [ ] First task\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    monkeypatch.setattr(
        "sys.argv", ["ailoop", "check-task-file", str(open_path), "--state-exit-code"]
    )
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 10


def test_task_file_cli_path_is_expanded(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: ~/.config/ailoop/state-test-cli
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: 1
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
""".strip()
    )
    task_file = "~/tmp-ailoop-task-file.md"
    from ailoop.config import load_app_config, resolve_run_config

    app_config = load_app_config(config_path)
    run_config = resolve_run_config(
        app_config,
        prompt="x",
        task_file=task_file,
        stop_when_tasks_complete=True,
    )
    assert run_config.task_file == str(Path(task_file).expanduser().resolve())


def test_run_cli_passes_task_file_flags(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: ~/.config/ailoop/state-test-cli
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: null
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
""".strip()
    )
    task_file = tmp_path / "tasks.md"
    task_file.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    seen: dict[str, object] = {}

    def fake_create_loop(self, run_config, loop_id=None):  # type: ignore[no-untyped-def]
        seen["run_config"] = run_config
        return LoopState(
            loop_id="cli-loop",
            created_at="now",
            updated_at="now",
            status="idle",
            control="run",
            run_config=run_config,
        )

    def fake_run_loop(self, loop_id):  # type: ignore[no-untyped-def]
        run_config = seen["run_config"]
        assert isinstance(run_config, LoopRunConfig)
        return LoopState(
            loop_id=loop_id,
            created_at="now",
            updated_at="now",
            status="completed",
            control="run",
            run_config=run_config,
        )

    monkeypatch.setattr("ailoop.service.LoopService.create_loop", fake_create_loop)
    monkeypatch.setattr("ailoop.service.LoopService.run_loop", fake_run_loop)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--config",
            str(config_path),
            "run",
            "Work tasks.",
            "--task-file",
            str(task_file),
            "--until-tasks-complete",
        ],
    )

    main()
    out = capsys.readouterr().out
    assert "cli-loop" in out
    run_config = seen["run_config"]
    assert isinstance(run_config, LoopRunConfig)
    assert run_config.task_file == str(task_file.resolve())
    assert run_config.stop_when_tasks_complete is True


def test_run_json_output_has_no_banner(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: ~/.config/ailoop/state-test-cli
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: 1
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
""".strip()
    )
    seen: dict[str, object] = {}

    def fake_create_loop(self, run_config, loop_id=None):  # type: ignore[no-untyped-def]
        seen["run_config"] = run_config
        return LoopState(
            loop_id="json-run-loop",
            created_at="now",
            updated_at="now",
            status="idle",
            control="run",
            run_config=run_config,
        )

    def fake_run_loop(self, loop_id):  # type: ignore[no-untyped-def]
        run_config = seen["run_config"]
        assert isinstance(run_config, LoopRunConfig)
        return LoopState(
            loop_id=loop_id,
            created_at="now",
            updated_at="now",
            status="completed",
            control="run",
            run_config=run_config,
        )

    monkeypatch.setattr("ailoop.service.LoopService.create_loop", fake_create_loop)
    monkeypatch.setattr("ailoop.service.LoopService.run_loop", fake_run_loop)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "run", "Work json."],
    )
    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["loop_id"] == "json-run-loop"


def test_run_json_disables_iteration_output(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: ~/.config/ailoop/state-test-cli
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: 1
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
""".strip()
    )
    seen: dict[str, object] = {}
    from ailoop.service import LoopService as RealLoopService

    real_init = RealLoopService.__init__

    def fake_init(self, state_root, emit_output=True):  # type: ignore[no-untyped-def]
        seen["emit_output"] = emit_output
        return real_init(self, state_root, emit_output=emit_output)

    monkeypatch.setattr("ailoop.service.LoopService.__init__", fake_init)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "run", "Work json."],
    )
    try:
        main()
    except Exception:
        pass
    assert seen["emit_output"] is False


def test_status_json_output(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: ~/.config/ailoop/state-test-cli
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: null
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
""".strip()
    )
    state = LoopState(
        loop_id="json-loop",
        created_at="now",
        updated_at="now",
        status="completed",
        control="run",
        run_config=LoopRunConfig(
            prompt="x",
            runner="test",
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
        ),
        completed_iterations=1,
    )

    def fake_load_loop(self, loop_id):  # type: ignore[no-untyped-def]
        return state

    monkeypatch.setattr("ailoop.service.LoopService.load_loop", fake_load_loop)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--json", "--config", str(config_path), "status", "json-loop"],
    )
    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["loop_id"] == "json-loop"


def test_color_mode_always_adds_ansi(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: ~/.config/ailoop/state-test-cli
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: null
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
""".strip()
    )
    state = LoopState(
        loop_id="color-loop",
        created_at="now",
        updated_at="now",
        status="completed",
        control="run",
        run_config=LoopRunConfig(
            prompt="x",
            runner="test",
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
        ),
        completed_iterations=1,
    )

    def fake_load_loop(self, loop_id):  # type: ignore[no-untyped-def]
        return state

    monkeypatch.setattr("ailoop.service.LoopService.load_loop", fake_load_loop)
    monkeypatch.setattr(
        "sys.argv",
        ["ailoop", "--color", "always", "--config", str(config_path), "status", "color-loop"],
    )
    main()
    out = capsys.readouterr().out
    assert "\u001b[" in out or "\x1b[" in out


def test_logs_json_output(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: ~/.config/ailoop/state-test-cli
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: null
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
""".strip()
    )
    loop_dir = tmp_path / "state" / "loop-json" / "logs"
    loop_dir.mkdir(parents=True)
    (tmp_path / "state" / "loop-json" / "state.json").write_text(
        json.dumps(
            {
                "loop_id": "loop-json",
                "created_at": "now",
                "updated_at": "now",
                "status": "completed",
                "control": "run",
                "run_config": {
                    "prompt": "x",
                    "runner": "test",
                    "agent": "orchestrator",
                    "steps": 1,
                    "pause_seconds": 0,
                    "continue_on_error": True,
                    "retry_count": 0,
                    "pre_prompt_enabled": False,
                    "attach_agent_file": False,
                    "pre_prompt": "",
                    "agent_file": None,
                    "runner_command": "python3",
                    "runner_args": ["-c", "print('ok')"],
                    "runner_env": {},
                    "task_file": None,
                    "stop_when_tasks_complete": False,
                    "max_doing": 1,
                },
                "current_iteration": 1,
                "completed_iterations": 1,
                "last_exit_code": 0,
                "consecutive_failures": 0,
                "total_duration_seconds": 0.0,
                "average_duration_seconds": 0.0,
                "last_summary": "ok",
                "iterations": [
                    {
                        "number": 1,
                        "started_at": "now",
                        "finished_at": "now",
                        "duration_seconds": 0.0,
                        "exit_code": 0,
                        "success": True,
                        "stdout_log": str(loop_dir / "iteration-0001.stdout.log"),
                        "stderr_log": str(loop_dir / "iteration-0001.stderr.log"),
                        "prompt_file": str(loop_dir / "iteration-0001.prompt.txt"),
                        "summary": "ok",
                    }
                ],
            }
        )
    )
    (loop_dir / "iteration-0001.stdout.log").write_bytes(b"ok\xff\n")
    (loop_dir / "iteration-0001.stderr.log").write_text("")
    (loop_dir / "iteration-0001.prompt.txt").write_text("prompt\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "logs",
            "loop-json",
            "--print",
        ],
    )
    from ailoop.service import LoopService as RealLoopService

    real_init = RealLoopService.__init__

    def fake_init(self, state_root, emit_output=True):  # type: ignore[no-untyped-def]
        return real_init(self, tmp_path / "state", emit_output=emit_output)

    monkeypatch.setattr("ailoop.service.LoopService.__init__", fake_init)
    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["stdout"]["content"] == "ok�\n"


def test_logs_json_output_can_tail_printed_content(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    loop_dir = tmp_path / "state" / "loop-json-tail" / "logs"
    loop_dir.mkdir(parents=True)
    (tmp_path / "state" / "loop-json-tail" / "state.json").write_text(
        json.dumps(
            {
                "loop_id": "loop-json-tail",
                "created_at": "now",
                "updated_at": "now",
                "status": "completed",
                "control": "run",
                "run_config": {
                    "prompt": "x",
                    "runner": "test",
                    "agent": "orchestrator",
                    "steps": 1,
                    "pause_seconds": 0,
                    "continue_on_error": True,
                    "retry_count": 0,
                    "pre_prompt_enabled": False,
                    "attach_agent_file": False,
                    "pre_prompt": "",
                    "agent_file": None,
                    "runner_command": "python3",
                    "runner_args": ["-c", "print('ok')"],
                    "runner_env": {},
                    "task_file": None,
                    "stop_when_tasks_complete": False,
                    "max_doing": 1,
                },
                "current_iteration": 1,
                "completed_iterations": 1,
                "last_exit_code": 0,
                "consecutive_failures": 0,
                "total_duration_seconds": 0.0,
                "average_duration_seconds": 0.0,
                "last_summary": "ok",
                "iterations": [
                    {
                        "number": 1,
                        "started_at": "now",
                        "finished_at": "now",
                        "duration_seconds": 0.0,
                        "exit_code": 0,
                        "success": True,
                        "stdout_log": str(loop_dir / "iteration-0001.stdout.log"),
                        "stderr_log": str(loop_dir / "iteration-0001.stderr.log"),
                        "prompt_file": str(loop_dir / "iteration-0001.prompt.txt"),
                        "summary": "ok",
                    }
                ],
            }
        )
    )
    (loop_dir / "iteration-0001.stdout.log").write_text("a\nb\nc\n")
    (loop_dir / "iteration-0001.stderr.log").write_text("")
    (loop_dir / "iteration-0001.prompt.txt").write_text("prompt\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--json",
            "--config",
            str(config_path),
            "logs",
            "loop-json-tail",
            "--print",
            "--tail-lines",
            "2",
        ],
    )
    from ailoop.service import LoopService as RealLoopService

    real_init = RealLoopService.__init__

    def fake_init(self, state_root, emit_output=True):  # type: ignore[no-untyped-def]
        return real_init(self, tmp_path / "state", emit_output=emit_output)

    monkeypatch.setattr("ailoop.service.LoopService.__init__", fake_init)
    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["stdout"]["content"] == "b\nc"


def test_logs_print_can_tail_selected_content(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = write_test_config(tmp_path)
    loop_dir = tmp_path / "state" / "loop-print-tail" / "logs"
    loop_dir.mkdir(parents=True)
    (tmp_path / "state" / "loop-print-tail" / "state.json").write_text(
        json.dumps(
            {
                "loop_id": "loop-print-tail",
                "created_at": "now",
                "updated_at": "now",
                "status": "completed",
                "control": "run",
                "run_config": {
                    "prompt": "x",
                    "runner": "test",
                    "agent": "orchestrator",
                    "steps": 1,
                    "pause_seconds": 0,
                    "continue_on_error": True,
                    "retry_count": 0,
                    "pre_prompt_enabled": False,
                    "attach_agent_file": False,
                    "pre_prompt": "",
                    "agent_file": None,
                    "runner_command": "python3",
                    "runner_args": ["-c", "print('ok')"],
                    "runner_env": {},
                    "task_file": None,
                    "stop_when_tasks_complete": False,
                    "max_doing": 1,
                },
                "current_iteration": 1,
                "completed_iterations": 1,
                "last_exit_code": 0,
                "consecutive_failures": 0,
                "total_duration_seconds": 0.0,
                "average_duration_seconds": 0.0,
                "last_summary": "ok",
                "iterations": [
                    {
                        "number": 1,
                        "started_at": "now",
                        "finished_at": "now",
                        "duration_seconds": 0.0,
                        "exit_code": 0,
                        "success": True,
                        "stdout_log": str(loop_dir / "iteration-0001.stdout.log"),
                        "stderr_log": str(loop_dir / "iteration-0001.stderr.log"),
                        "prompt_file": str(loop_dir / "iteration-0001.prompt.txt"),
                        "summary": "ok",
                    }
                ],
            }
        )
    )
    (loop_dir / "iteration-0001.stdout.log").write_text("a\nb\nc\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "ailoop",
            "--config",
            str(config_path),
            "logs",
            "loop-print-tail",
            "--kind",
            "stdout",
            "--print",
            "--tail-lines",
            "2",
        ],
    )
    from ailoop.service import LoopService as RealLoopService

    real_init = RealLoopService.__init__

    def fake_init(self, state_root, emit_output=True):  # type: ignore[no-untyped-def]
        return real_init(self, tmp_path / "state", emit_output=emit_output)

    monkeypatch.setattr("ailoop.service.LoopService.__init__", fake_init)
    main()
    out = capsys.readouterr().out
    assert out == f"[stdout] {loop_dir / 'iteration-0001.stdout.log'}\nb\nc\n"
