from pathlib import Path

import yaml

from ailoop.config import build_app_config, deep_merge, load_app_config, resolve_run_config


def test_deep_merge_overrides_nested_values() -> None:
    merged = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}, "d": 4})
    assert merged == {"a": {"b": 1, "c": 3}, "d": 4}


def test_load_app_config_applies_yaml_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "default_runner": "codex",
                "loop": {"pause_seconds": 5},
            }
        )
    )
    app_config = load_app_config(config_path)
    assert app_config.default_runner == "codex"
    assert app_config.loop.pause_seconds == 5


def test_resolve_run_config_prefers_cli_values() -> None:
    app_config = build_app_config(
        {
            "default_runner": "opencode",
            "default_agent": "orchestrator",
            "paths": {"agent_file": "~/agent.md", "state_dir": "~/.config/ailoop/state"},
            "prompt": {
                "pre_prompt_enabled": True,
                "attach_agent_file": True,
                "pre_prompt": "hello",
            },
            "loop": {
                "steps": None,
                "pause_seconds": 30,
                "continue_on_error": True,
                "retry_count": 0,
            },
            "tasks": {"file": "~/tasks.md", "stop_when_complete": True, "max_doing": 1},
            "runners": {
                "opencode": {"command": "opencode", "args": ["run", "{prompt}"]},
            },
        }
    )
    run_config = resolve_run_config(
        app_config,
        prompt="do work",
        agent="build",
        steps=2,
        pause_seconds=1,
        pre_prompt_enabled=False,
    )
    assert run_config.agent == "build"
    assert run_config.steps == 2
    assert run_config.pause_seconds == 1
    assert run_config.pre_prompt_enabled is False
    assert run_config.task_file.endswith("tasks.md")
    assert run_config.stop_when_tasks_complete is True


def test_resolve_run_config_expands_cli_task_file() -> None:
    app_config = build_app_config(
        {
            "default_runner": "opencode",
            "default_agent": "orchestrator",
            "paths": {"agent_file": None, "state_dir": "~/.config/ailoop/state"},
            "prompt": {
                "pre_prompt_enabled": True,
                "attach_agent_file": True,
                "pre_prompt": "hello",
            },
            "loop": {
                "steps": None,
                "pause_seconds": 30,
                "continue_on_error": True,
                "retry_count": 0,
            },
            "tasks": {"file": None, "stop_when_complete": False, "max_doing": 1},
            "runners": {
                "opencode": {"command": "opencode", "args": ["run", "{prompt}"]},
            },
        }
    )
    run_config = resolve_run_config(app_config, prompt="do work", task_file="~/tasks.md")
    assert run_config.task_file == str(Path("~/tasks.md").expanduser().resolve())
