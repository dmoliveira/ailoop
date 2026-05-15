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
- `runners.<name>.command`
- `runners.<name>.args`

Example lives in `README.md`.
