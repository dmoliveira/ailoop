# ailoop v0.2.2

## Fixes

- Completed iteration accounting now survives post-run task-file validation failures, so a
  successful runner is not repeated after the task file is repaired. (#97)
- `memory edit` patches only explicitly supplied fields against the latest snapshot instead of
  resetting omitted values to current application defaults. (#98)
- Scheduled loops are presented honestly as saved configurations while scheduler execution is
  unavailable. Unsupported runtime controls are disabled and guarded. (#99)

## Safety

- Finalized iteration events and workspace history are persisted before post-run validation errors
  surface.
- Memory updates use stable per-entry locks and durable atomic replacement; concurrent edits and
  usage updates no longer lose each other.
- Scheduled follow-ups are saved without spawning a process, pending manual work cannot be
  overwritten by a schedule save, and emergency stop remains available for a claimed process.

## Quality

- Added regressions for task-file failure recovery, memory patch/version integrity, concurrent
  memory mutation, atomic-write failures, scheduled keyboard actions, and scheduler-unavailable
  status copy.
- Release validation: 287 tests passed with 83% coverage; Ruff and lock checks passed; wheel
  and source distributions passed `twine check`; and the wheel installed and ran in a clean
  virtual environment.

## Install

Download the wheel attached to this GitHub release, then run:

```bash
pipx install ./ai_loop-0.2.2-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
