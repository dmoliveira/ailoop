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

## 3) Validate and build from the merged release commit

Merge the release-preparation PR first. Capture its GitHub-reported merge commit once and verify the
exact `main` push CI run before building anything that will be published:

```bash
PR=<release-pr-number>
RELEASE_SHA="$(gh pr view "$PR" --json mergeCommit --jq .mergeCommit.oid)"
git fetch --prune origin
git merge-base --is-ancestor "$RELEASE_SHA" origin/main

RUN_ID="$(gh run list --branch main --workflow CI --limit 20 \
  --json databaseId,headSha \
  --jq ".[] | select(.headSha == \"$RELEASE_SHA\") | .databaseId" | head -1)"
test -n "$RUN_ID"
gh run watch "$RUN_ID" --exit-status --interval 10
gh run view "$RUN_ID" --json headSha,conclusion,jobs \
  --jq '{headSha,conclusion,jobs:[.jobs[]|{name,conclusion}]}'
```

Require successful `Python 3.11` and `Python 3.13` jobs. A canceled, superseded, or differently
addressed run is not sufficient.

Create a fresh detached worktree at that exact SHA. Build and validate the only artifacts eligible
for publication there; pre-merge or earlier local artifacts are disposable:

```bash
RELEASE_WORKTREE="$(mktemp -d)"
git worktree add --detach "$RELEASE_WORKTREE" "$RELEASE_SHA"
test "$(git -C "$RELEASE_WORKTREE" rev-parse HEAD)" = "$RELEASE_SHA"

(
  cd "$RELEASE_WORKTREE"
  python3 -c 'import shutil; shutil.rmtree("dist", ignore_errors=True)'
  uv sync --frozen --all-groups
  uv build --out-dir dist
  uvx twine check \
    dist/ai_loop-0.2.7-py3-none-any.whl \
    dist/ai_loop-0.2.7.tar.gz
  python3 -c 'from pathlib import Path; Path("dist/.gitignore").unlink(missing_ok=True)'
  python3 - <<'PY'
from pathlib import Path
expected = {
    "ai_loop-0.2.7-py3-none-any.whl",
    "ai_loop-0.2.7.tar.gz",
}
actual = {path.name for path in Path("dist").iterdir()}
assert actual == expected, (actual, expected)
PY
  (cd dist && shasum -a 256 \
    ai_loop-0.2.7-py3-none-any.whl \
    ai_loop-0.2.7.tar.gz > SHA256SUMS)
)

TRUSTED_MANIFEST="$(mktemp)"
cp "$RELEASE_WORKTREE/dist/SHA256SUMS" "$TRUSTED_MANIFEST"
```

Smoke-test the wheel and source distribution separately in clean Python 3.11 environments:

```bash
WHEEL_SMOKE="$(mktemp -d)"
SDIST_SMOKE="$(mktemp -d)"
uv venv --seed --python 3.11 "$WHEEL_SMOKE"
uv venv --seed --python 3.11 "$SDIST_SMOKE"
"$WHEEL_SMOKE/bin/pip" install --no-input \
  "$RELEASE_WORKTREE/dist/ai_loop-0.2.7-py3-none-any.whl"
"$SDIST_SMOKE/bin/pip" install --no-input \
  "$RELEASE_WORKTREE/dist/ai_loop-0.2.7.tar.gz"

for ENVIRONMENT in "$WHEEL_SMOKE" "$SDIST_SMOKE"; do
  "$ENVIRONMENT/bin/python" -c \
    'import ailoop; from importlib.metadata import version; assert version("ai-loop") == ailoop.__version__ == "0.2.7"'
  "$ENVIRONMENT/bin/ailoop" --help
done
```

PyPI publication is not currently configured; release artifacts are hosted on GitHub.

## 4) Tag, publish, and independently verify

Immediately before tagging, check every release namespace. Any unexpected collision stops the
release; never force, move, delete, or recreate a published tag:

```bash
test -z "$(git tag --list v0.2.7)"
test -z "$(git ls-remote --tags origin refs/tags/v0.2.7)"
if gh release view v0.2.7 >/dev/null 2>&1; then
  echo "v0.2.7 GitHub release already exists" >&2
  exit 1
fi
```

Create and push an annotated tag from the recorded merged SHA, then publish exactly the validated
assets from the detached worktree:

```bash
git tag -a v0.2.7 "$RELEASE_SHA" -m "ailoop v0.2.7"
test "$(git rev-parse 'v0.2.7^{}')" = "$RELEASE_SHA"
git push origin v0.2.7

gh release create v0.2.7 \
  "$RELEASE_WORKTREE/dist/ai_loop-0.2.7-py3-none-any.whl" \
  "$RELEASE_WORKTREE/dist/ai_loop-0.2.7.tar.gz" \
  "$RELEASE_WORKTREE/dist/SHA256SUMS" \
  --verify-tag \
  --title "ailoop v0.2.7" \
  --notes-file "$RELEASE_WORKTREE/docs/release-notes-v0.2.7.md"
```

If the tag push succeeds but release creation fails, resume only when the existing annotated tag
peels to `RELEASE_SHA`. If it targets anything else, stop and issue a new version; never retag.

Download into a new directory, require the exact three assets, compare the downloaded manifest to
the preserved trusted copy, and smoke both downloaded distributions:

```bash
VERIFY_DIR="$(mktemp -d)"
gh release download v0.2.7 --dir "$VERIFY_DIR"
python3 - "$VERIFY_DIR" <<'PY'
from pathlib import Path
import sys
expected = {
    "ai_loop-0.2.7-py3-none-any.whl",
    "ai_loop-0.2.7.tar.gz",
    "SHA256SUMS",
}
actual = {path.name for path in Path(sys.argv[1]).iterdir()}
assert actual == expected, (actual, expected)
PY
cmp "$TRUSTED_MANIFEST" "$VERIFY_DIR/SHA256SUMS"
(cd "$VERIFY_DIR" && shasum -a 256 -c SHA256SUMS)

for PACKAGE in \
  "$VERIFY_DIR/ai_loop-0.2.7-py3-none-any.whl" \
  "$VERIFY_DIR/ai_loop-0.2.7.tar.gz"; do
  INSTALL_ENV="$(mktemp -d)"
  uv venv --seed --python 3.11 "$INSTALL_ENV"
  "$INSTALL_ENV/bin/pip" install --no-input "$PACKAGE"
  "$INSTALL_ENV/bin/python" -c \
    'import ailoop; from importlib.metadata import version; assert version("ai-loop") == ailoop.__version__ == "0.2.7"'
  "$INSTALL_ENV/bin/ailoop" --help
done

REMOTE_RELEASE_SHA="$(git ls-remote origin 'refs/tags/v0.2.7^{}' | awk '{print $1}')"
test "$REMOTE_RELEASE_SHA" = "$RELEASE_SHA"
gh release view v0.2.7 --json url,tagName,isDraft,isPrerelease,publishedAt,assets
```

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
- clean wheel and source-distribution install smokes pass
- exact merged-SHA Python 3.11 and Python 3.13 CI passes
- `v0.2.7^{}` peels to the exact merged release commit
- GitHub release is published
- GitHub release assets and notes are verified
