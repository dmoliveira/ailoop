# ailoop v0.2.0

## Adds

- Searchable TUI loop navigation across loop IDs, state, mode, runner, agent, prompt, workspace, and task-file metadata.
- A recent-workspace picker plus workspace-aware follow-up and history tracking.
- Runner iteration timeouts and more responsive stop behavior.

## Changes

- TUI controls preserve unsaved editor input while searching and switching loops.
- Workspace-root validation and status are surfaced before spawning a runner.
- Loop refreshes reuse one state snapshot instead of repeatedly scanning state files.
- The Textual dependency now targets the tested 8.2 release line instead of an incompatible legacy floor.

## Fixes

- Stop TUI refresh callbacks safely during shutdown.
- Observe pause and stop controls during loop delays.
- Interrupt active runners when a stop is requested.
- Validate workspace roots before spawning.
- Clarify queued follow-up and reset/restart behavior.

## Quality

- Expanded TUI, service, prompting, and workspace-loop regression coverage.
- Added CI for Python 3.11 and 3.13.
- Enforced an 80% branch-coverage floor.

## Install

Download the wheel attached to this GitHub release, then run:

```bash
pipx install ./ai_loop-0.2.0-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
