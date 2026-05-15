# Task file mode ✅

Use strict Markdown when you want `ailoop` to stop only after the task list is done.

Create one:

```bash
ailoop init-task-file ./loop_tasks.md
ailoop check-task-file ./loop_tasks.md
```

Run with it:

```bash
ailoop run "Work the task list." --task-file ./loop_tasks.md --until-tasks-complete
```

Valid format:

```md
# Loop Tasks

## To do
- [ ] First task

## Doing
- None

## Done
- None
```

Rules:

- title must be `# Loop Tasks`
- sections must be `To do`, `Doing`, `Done`
- empty section must use `- None`
- max 1 task in `Doing`
- `To do` and `Doing` use `- [ ] task`
- `Done` uses `- [x] task`

Exit codes for `check-task-file --state-exit-code`:

- `0` done
- `10` open
- `1` invalid
