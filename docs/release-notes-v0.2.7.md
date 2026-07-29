# ailoop v0.2.7

## Fixes

- Programmatic loop-table rebuilds now suppress `DataTable.RowHighlighted`, so refreshes no longer
  rebind transient rows or clear unsaved loop drafts, selection, cursor position, or workspace
  branch state.

## Quality

- Added Python 3.11 CI regression coverage for the refresh and draft-preservation path.

## Install

```bash
pipx install ./ai_loop-0.2.7-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
