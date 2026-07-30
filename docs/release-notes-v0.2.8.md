# ailoop v0.2.8

## Fixes

- Retry admission now serializes with durable stop mutations. A stop committed before admission
  suppresses the retry, while a later stop still cancels admitted work through the normal active-run
  path.
- Stop intent now remains dominant over later pause requests while explicit resume continues to run
  remaining work.

## Security

- State lock handling now pins operations to owner-controlled, non-group/world-writable state
  namespaces; rejects unsafe path types, ownership, hardlinks, and changed lock identities; normalizes
  `.locks` and lock files to modes `0700` and `0600`; and writes diagnostic PIDs only after successful
  lock validation.
- The configured state-root ancestry, processes sharing the effective user ID, and non-local
  filesystem lock semantics remain trusted boundaries rather than claimed isolation guarantees.

## Compatibility

- `prompt.pre_prompt_enabled`, `prompt.attach_agent_file`, `loop.continue_on_error`, and
  `tasks.stop_when_complete` now require values that PyYAML parses as booleans. Quoted strings,
  numbers, `null`, and collections are rejected with field-specific configuration errors.
- Use unquoted YAML `true` and `false` for portable, explicit configuration. PyYAML legacy spellings
  that already parse as booleans remain accepted.

## Quality

- The release-preparation suite passes 506 tests with 85% coverage, including adversarial lock-path,
  retry-control, explicit-resume, and strict-config regressions.

## Install

```bash
pipx install ./ai_loop-0.2.8-py3-none-any.whl
ailoop --help
```

PyPI publication is not part of this release.
