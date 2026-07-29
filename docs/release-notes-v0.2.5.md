# ailoop v0.2.5

## Reliability

- Memory IDs are collision-safe and publication cannot overwrite an existing entry.
- Arbitrary runner output bytes remain exact on disk and render safely in summaries, CLI, and TUI.
- Committed run intents remain successful when best-effort event journaling fails.
- Retry and reset executions preserve distinct prompt, stdout, and stderr evidence.
- Concurrent workspace-history appends and compaction are serialized and atomic.

## TUI safety

- Hidden or editor-focused memory shortcuts fail closed.
- Loop actions require a visible, stable row identity; hidden query drafts cannot mutate loops.
- Memory archive and delete confirmations are bound to the same entry ID across both keypresses.
- Detached worker and replay feedback reports launch requests and recoverable launch failures honestly.

## Quality

- Release validation includes 405 passing tests with 85% coverage, Ruff, package checks,
  clean-wheel installation, Python 3.11 and Python 3.13 CI, and downloaded artifact checksum
  verification.

## Install

Download the wheel attached to this GitHub release, then run:

```bash
pipx install ./ai_loop-0.2.5-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
