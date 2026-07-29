# Release + launch 🚀

Use this page to build, publish, and verify an `ailoop` GitHub release.

## 1) Public repo settings

Repo name:

- `ailoop`

Description:

- `Repeatable AI terminal loops with YAML config, durable state, task-file mode, pause/resume controls, and JSON-friendly output.`

Topics:

- `ai`
- `cli`
- `automation`
- `developer-tools`
- `llm`
- `agentic-workflows`
- `terminal`
- `yaml`
- `task-runner`
- `python`

## 2) Create GitHub repo

If the local folder is not yet a git repo:

```bash
git init
git add .
git commit -m "Launch ailoop"
gh repo create dmoliveira/ailoop --public --source=. --remote=origin --push \
  --description "Repeatable AI terminal loops with YAML config, durable state, task-file mode, pause/resume controls, and JSON-friendly output."
```

If the repo already exists locally:

```bash
git add .
git commit -m "Launch public docs and release pack"
git remote add origin git@github.com:dmoliveira/ailoop.git
git push -u origin main
```

Set topics:

```bash
gh repo edit dmoliveira/ailoop \
  --add-topic ai \
  --add-topic cli \
  --add-topic automation \
  --add-topic developer-tools \
  --add-topic llm \
  --add-topic agentic-workflows \
  --add-topic terminal \
  --add-topic yaml \
  --add-topic task-runner \
  --add-topic python
```

## 3) Build GitHub release artifacts

Build the wheel and source distribution from the validated release commit:

```bash
rm -rf dist
uv run coverage erase
uv build
uvx twine check dist/ai_loop-*.whl dist/ai_loop-*.tar.gz
(cd dist && shasum -a 256 ai_loop-*.whl ai_loop-*.tar.gz > SHA256SUMS)
```

Verify the wheel in a clean environment before publishing:

```bash
SMOKE_DIR="$(mktemp -d)"
uv venv --seed --python 3.11 "$SMOKE_DIR"
"$SMOKE_DIR/bin/pip" install dist/ai_loop-0.2.4-py3-none-any.whl
"$SMOKE_DIR/bin/ailoop" --help
```

PyPI publication is not currently configured; release artifacts are hosted on GitHub.

## 4) Git tag + GitHub release

Create and push an annotated tag from the exact merged release-PR commit, then use that
verified tag for the GitHub release:

Before tagging, wait for the `main` CI workflow on that exact commit and verify both the
Python 3.11 and Python 3.13 jobs passed. Also confirm that neither the remote tag nor the
GitHub release already exists.

```bash
RELEASE_SHA=<merged-release-sha>
git tag -a v0.2.4 "$RELEASE_SHA" -m "ailoop v0.2.4"
git push origin v0.2.4
gh release create v0.2.4 \
  dist/ai_loop-0.2.4-py3-none-any.whl \
  dist/ai_loop-0.2.4.tar.gz \
  dist/SHA256SUMS \
  --verify-tag \
  --title "ailoop v0.2.4" \
  --notes-file docs/release-notes-v0.2.4.md
```

Then verify the tag, checksums, assets, and wheel install from the published release.

## 5) Release notes

Keep release-specific notes in `docs/release-notes-vX.Y.Z.md`. Notes should separate additions, changes, fixes, quality, and installation instructions without claiming unpublished distribution channels.

## 6) Badge updates after publish

These links should resolve cleanly:

- latest GitHub release
- supported Python version
- license
- GitHub Actions CI
- release tag

## 7) Short launch post

```text
Launched ailoop 🔁 — a small CLI for running AI terminal tools in repeatable loops instead of fragile while-true scripts.

It ships with YAML config, durable state, task-file mode, pause/resume, logs/stats, and JSON output for OpenCode, Codex, Claude, and more.

Repo: https://github.com/dmoliveira/ailoop
Support: https://buy.stripe.com/8x200i8bSgVe3Vl3g8bfO00
```

## 8) X / short post

```text
Launched ailoop 🔁

Repeatable loops for AI terminal tools.

YAML config, durable state, task files, pause/resume, stats, JSON output.

Works with OpenCode, Codex, Claude, and more.

github.com/dmoliveira/ailoop
```

## 9) LinkedIn / longer post

```text
I launched ailoop 🔁 — a small CLI for running AI terminal tools in repeatable, resumable loops.

The goal is simple: replace fragile one-off shell loops with a cleaner workflow built around YAML config, durable state, strict task files, pause/resume controls, and machine-friendly output.

It works well for OpenCode, Codex, Claude, and similar AI terminal tools.

Repo: https://github.com/dmoliveira/ailoop
Support: https://buy.stripe.com/8x200i8bSgVe3Vl3g8bfO00
```

## 10) Final launch checklist

- repo is public
- repo description is set
- topics are set
- README hero renders
- docs links work
- support link works
- wheel and source distribution are attached
- SHA-256 checksums are attached
- clean-wheel install smoke passes
- `v0.2.4^{}` peels to the exact merged release commit
- GitHub release is published
- GitHub release assets and notes are verified
