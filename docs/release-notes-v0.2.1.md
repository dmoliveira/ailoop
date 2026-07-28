# ailoop v0.2.1

## Fixes

- `ailoop resume` now establishes an explicit run intent for paused and stopped loops.
- Loop state and control transitions are transactional, so newer pause or stop requests win
  before a detached dashboard worker can claim the next iteration.
- State writes use durable atomic replacement, while event appends are flushed and synced.

## Safety

- On POSIX, catchable runner exits clean and reap the original process group before the loop
  becomes resumable.
- Unconfirmed cleanup records `cleanup_failed` and blocks rerun, resume, control, and removal;
  it does not claim that the process was terminated.
- Abrupt parent `SIGKILL` can leave a runner alive and require manual cleanup. The persisted loop
  remains active and fails closed instead of authorizing duplicate work.
- Removed loop IDs are durably retired and cannot be reused by delayed workers or events.
- Scheduled loops remain configuration-only until scheduler execution is implemented.

## Quality

- Added lifecycle-safety coverage for control races, durable claims, cleanup failures,
  process-group teardown, retired IDs, removal races, and scheduled-loop guardrails.
- CI passes on Python 3.11 and 3.13.
- 272 tests pass with 83% coverage.

## Install

Download the wheel attached to this GitHub release, then run:

```bash
pipx install ./ai_loop-0.2.1-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
