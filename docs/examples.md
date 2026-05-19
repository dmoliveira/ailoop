# Examples ⚡

Basic:

```bash
ailoop init-config
ailoop run "Review the repo and keep iterating." --runner opencode --agent orchestrator
```

Bounded loop:

```bash
ailoop run "Do exactly 5 iterations." --steps 5
```

Task-file loop:

```bash
ailoop init-task-file ./loop_tasks.md
ailoop run "Work the task list." --task-file ./loop_tasks.md --until-tasks-complete
```

Preset + replay:

```bash
ailoop memory save "Quick review" "Review the repo and keep iterating." --runner opencode --agent orchestrator
ailoop memory list --kind preset
ailoop memory show <memory-id>
ailoop replay <memory-id>
```

History entry:

```bash
ailoop memory save "Recent bugfix pass" "Re-run the bugfix workflow." --kind history --steps 5
ailoop memory favorite <memory-id>
ailoop memory edit <memory-id> --title "Recent bugfix pass v2"
```

Watch:

```bash
ailoop ps
ailoop tail <loop-id>
```

TUI memory smoke:

```bash
bash ./scripts/tui_memory_smoke.sh
tmux attach -t ailoop-tui-smoke
```

Inside the TUI:
- press `5`, `6`, `7`, or `0`
- use `[` and `]` to move between memory entries
- use `8`, `9`, `v`, `z`, and `x` to exercise replay/favorite/restore/archive/delete

GitHub PR helper:

```bash
python3 ./scripts/github_pr_create.py \
  --repo dmoliveira/ailoop \
  --title "Polish TUI fallback" \
  --head dmoliveira:feat/example-branch \
  --base main \
  --body "## Summary\n- describe the change"
```

- uses `GITHUB_TOKEN` / `GH_TOKEN` when present
- otherwise falls back to `gh auth token`
- prints the raw GitHub API response JSON on success

JSON:

```bash
ailoop --json ps
ailoop --json status <loop-id>
ailoop --json logs <loop-id>
```
