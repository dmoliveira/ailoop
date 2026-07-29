# ailoop v0.2.6

## Fixes

- Lock teardown and file-close failures no longer turn an already committed state mutation into a
  reported operation failure.
- Best-effort postcommit event suppression is limited to storage `OSError` failures; programming
  errors and interrupts still surface.
- Ambiguous append-succeeded-then-raised outcomes do not retry or duplicate events, workers, or
  consumed follow-up intent.

## Quality

- 418 tests pass with 85% coverage on the corrective release line.
- Python 3.11 and Python 3.13 CI, Ruff, package metadata checks, clean-wheel installation, and
  downloaded checksum verification are required before completion.

## Install

```bash
pipx install ./ai_loop-0.2.6-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
