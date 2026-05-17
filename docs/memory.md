# Memory: presets, history, and replay

`ailoop` can save reusable run setups as local memory entries.

There are two kinds:

- `preset` — a saved reusable run configuration
- `history` — a saved past run pattern you may want to replay later

Entries are stored under your state root:

```text
~/.config/ailoop/state/memory/
```

## Basic flow

Save a preset:

```bash
ailoop memory save "Quick review" "Review the repo and keep iterating." --runner opencode --agent orchestrator
```

List entries:

```bash
ailoop memory list
ailoop memory list --kind preset
ailoop memory list --favorites
```

Inspect one entry:

```bash
ailoop memory show <memory-id>
```

Replay an entry into a new loop:

```bash
ailoop replay <memory-id>
ailoop replay <memory-id> --loop-id review-replay-01
```

## Update metadata or saved config

Edit a title:

```bash
ailoop memory edit <memory-id> --title "Quick review v2"
```

Edit the saved prompt/config:

```bash
ailoop memory edit <memory-id> \
  --prompt "Review the repo with extra focus on tests." \
  --steps 5 \
  --pause-seconds 10
```

Favorite or delete:

```bash
ailoop memory favorite <memory-id>
ailoop memory favorite <memory-id> --off
ailoop memory delete <memory-id>
```

## Scope behavior

Memory entries are scoped by current user and current folder by default.

That means:

- `memory list` shows entries for the current repo/folder
- single-entry commands like `show`, `edit`, `delete`, and `replay` only work when the entry belongs to the current folder scope
- `memory list --all-folders` shows entries across folders for the current user

This keeps presets/history safer when you use `ailoop` across multiple repos.

## Notes

- `replay` creates a new loop from the saved entry
- replay usage stats increase only after replay starts successfully
- entries keep version snapshots when saved config changes through `memory edit`
