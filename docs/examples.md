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

Watch:

```bash
ailoop ps
ailoop tail <loop-id>
```

JSON:

```bash
ailoop --json ps
ailoop --json status <loop-id>
ailoop --json logs <loop-id>
```
