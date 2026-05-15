# Release + launch 🚀

Use this page to publish `ailoop` as a public repo, push the first release, and ship the launch copy.

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

## 3) PyPI publish

Recommended package name:

- `ailoop`

Build:

```bash
python3 -m venv .venv-publish
. .venv-publish/bin/activate
python -m pip install --upgrade pip build twine
python -m build
twine check dist/*
```

Publish:

```bash
twine upload dist/*
```

Then verify:

```bash
pipx install ailoop
ailoop --help
```

## 4) First git tag + GitHub release

Suggested first tag:

- `v0.1.0`

Commands:

```bash
git tag v0.1.0
git push origin v0.1.0
gh release create v0.1.0 \
  --title "ailoop v0.1.0" \
  --notes-file docs/release-notes-v0.1.0.md
```

## 5) Release notes

Create `docs/release-notes-v0.1.0.md` with:

```md
# ailoop v0.1.0

- Launches `ailoop`, a small CLI for running AI terminal tools in safe, repeatable loops.
- Adds YAML config, CLI overrides, and durable local state for resumable runs.
- Supports OpenCode, Codex, Claude, and other command-template runners.
- Includes strict Markdown task-file mode for structured iterative work.
- Ships pause, resume, stop, logs, stats, tail, and JSON output for automation.
```

## 6) Badge updates after publish

Once the public repo and PyPI package are live, these links should resolve cleanly:

- PyPI version
- Python versions
- License
- release tag
- GitHub Actions test badge if/when workflow is added

Optional future badges:

- `GitHub release`
- `Last commit`
- `Stars`

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
- PyPI package is published
- `v0.1.0` tag exists
- GitHub release is published
- launch post is published
