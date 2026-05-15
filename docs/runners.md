# Runners 🤖

`ailoop` runs external AI terminal tools through simple command templates.

Built-in examples:

- `opencode`
- `codex`
- `claude`

Example:

```yaml
runners:
  opencode:
    command: opencode
    args: ["run", "--agent", "{agent}", "{prompt}"]
```

Template vars:

- `{prompt}`
- `{prompt_file}`
- `{agent}`
- `{loop_id}`
- `{iteration}`

Use CLI to override runner or agent per run:

```bash
ailoop run "Review the repo" --runner opencode --agent orchestrator
```
