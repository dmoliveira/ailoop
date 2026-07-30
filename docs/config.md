# Config ⚙️

Main config path:

```text
~/.config/ailoop/config.yaml
```

State path:

```text
~/.config/ailoop/state
```

Priority:

1. CLI flags
2. config file
3. built-in defaults

Core keys:

- `default_runner`
- `default_agent`
- `paths.agent_file`
- `paths.state_dir`
- `prompt.pre_prompt_enabled`
- `prompt.attach_agent_file`
- `prompt.pre_prompt`
- `loop.steps`
- `loop.pause_seconds`
- `loop.continue_on_error`
- `loop.retry_count`
- `tasks.file`
- `tasks.stop_when_complete`
- `tasks.max_doing`
- `runners.<name>.command`
- `runners.<name>.args`

Boolean settings must use YAML booleans such as `true` and `false`. Quoted strings,
numbers, and `null` are rejected instead of being coerced.

Example lives in `README.md`.
