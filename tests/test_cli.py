from __future__ import annotations

import json
from pathlib import Path

from ailoop.cli import main
from ailoop.models import LoopRunConfig, LoopState


def test_task_template_command_prints_template(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["ailoop", "task-template"])
    main()
    out = capsys.readouterr().out
    assert "# Loop Tasks" in out
    assert "Task file rules:" not in out


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
    path.write_text("## To do\n- None\n")
    monkeypatch.setattr("sys.argv", ["ailoop", "check-task-file", str(path)])
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 1
    out = capsys.readouterr().out
    assert "bad task file" in out
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
    (loop_dir / "iteration-0001.stdout.log").write_text("ok\n")
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
    assert data["stdout"]["content"] == "ok\n"
