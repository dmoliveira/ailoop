# ailoop v0.2.3

## Fixes

- Post-run task-file validation failures now emit a dedicated truthful event while preserving the
  original validation exception as the primary error. Secondary history/event failures are attached
  as notes instead of masking it. (#101)
- Saved scheduled configurations can transition atomically to fixed or infinite execution, while
  requests that remain scheduled are still rejected from manual restart. (#102)
- TUI restart and worker-spawn failures are reported without escaping the action handler. Scheduled
  help now shows emergency stop only for a claimed process and never advertises deletion while the
  loop is active. (#102)

## Safety

- Deferred validation catches only expected `OSError`/`ValueError` failures; interrupts and process
  exits retain their normal control-flow semantics.
- Completed iteration accounting, output artifacts, and resume numbering remain durable across
  task-file validation failures.
- A failed worker spawn leaves the converted executable configuration persisted and idle, with an
  explicit recoverable error message.

## Quality

- Added validation-primary combined-failure, deleted-task-file, resume-artifact, scheduled-mode
  transition, spawn-failure, emergency-stop-help, and delete-help regressions.
- Release validation: 293 tests passed with 83% coverage; Ruff and lock checks passed; wheel
  and source distributions passed `twine check`; and the wheel installed and reported version
  0.2.3 in a clean virtual environment.

## Install

Download the wheel attached to this GitHub release, then run:

```bash
pipx install ./ai_loop-0.2.3-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
