# ailoop v0.2.4

## Fixes

- Configured loop creation now writes complete state atomically and removes a newly created loop if
  creation-event or workspace-history persistence fails. (#104)
- Scheduled loops now fail closed while a worker is active: follow-up metadata changes are rejected,
  emergency stop is the only claimed-process control, and cleanup failures are refresh-only. (#104)
- Scheduled draft, branch strategy, safety, and notification surfaces now describe saved planning
  metadata honestly; unsupported controls are disabled and excluded from keyboard focus. (#104)

## Safety

- Recoverable service and worker-spawn failures now report the error, refresh state, and avoid false
  success messages. Unsaved follow-up text is preserved when a worker cannot start. (#104)
- Start, resume, follow-up, and next-iteration side effects occur only after their corresponding
  persistence or spawn step succeeds. (#104)

## Quality

- Added regressions for creation rollback, fail-closed scheduled edits, compact-layout capability
  copy, planning-only focus behavior, recoverable mutations and spawns, and input preservation. (#104)
- Release validation: 319 tests passed with 85% coverage; Ruff, diff, compile, and lock checks
  passed; Python 3.11 and Python 3.13 PR CI passed; wheel and source distributions passed
  `twine check`; and the wheel installed and reported version 0.2.4 in a clean environment.

## Install

Download the wheel attached to this GitHub release, then run:

```bash
pipx install ./ai_loop-0.2.4-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
