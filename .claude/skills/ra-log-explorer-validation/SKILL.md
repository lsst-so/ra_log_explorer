---
name: ra-log-explorer-validation
description: Validate Python or UI changes in the ra_log_explorer tool before declaring a task done. Nothing automates pre-commit, mypy, mypy-coverage, or pytest in this repo, so the checks must be run manually. Use this skill whenever you finish editing anything under `ra_log_explorer/`, `tests/`, or `architecture/` and are about to hand the task back to the user; when the user asks to "run the tests", "type check", "validate", or "check my changes"; or when you are about to stage a commit. The validation loop is the same whether the change is to Python, JS, CSS, or HTML — every commit is expected to pass it.
---

# ra_log_explorer: Validating changes

There is no CI on this repo today. None of `pre-commit`, `mypy`,
`mypy-coverage`, or `pytest` runs automatically — type errors and broken
tests land silently on `main` unless someone runs them by hand. **You**
are that someone for every change here.

## The validation loop

From the repo root, with the dev venv created (`python3.13 -m venv .venv
&& .venv/bin/pip install pre-commit black isort flake8 flake8-bugbear mypy
mypy-coverage pytest`):

```bash
.venv/bin/pre-commit run --all-files
.venv/bin/mypy
.venv/bin/mypy-coverage
.venv/bin/pytest -q
```

All four must pass before you commit. The four are independent — pre-commit
fixes formatting, mypy verifies types, mypy-coverage verifies annotation
coverage, pytest verifies behaviour. **Don't claim "tests pass" if you
only ran mypy, or vice versa.**

### 1. `pre-commit run --all-files`

Pinned versions of `pre-commit-hooks`, `isort`, `black`, and `flake8`
configured to match rapid analysis upstream — line length 110, camelCase
allowed (N802/N803/N806 ignored). Pre-commit's whitespace + EOF hooks
will auto-fix; re-run if anything changes. flake8 errors must be fixed
by hand.

### 2. `mypy`

`mypy.ini` sets `files = ra_log_explorer/`, `disallow_untyped_defs`, and
`disallow_incomplete_defs`. Pass with no arguments — passing a path
overrides the config's `files` setting and would skip the rest. Expected
clean output: `Success: no issues found in N source files`.

### 3. `mypy-coverage`

Reports the proportion of defs that have body-checked and fully-annotated
types. The project's current bar is **100% / 100%**. If your change drops
coverage, either annotate the missing piece or justify the gap in the
commit message.

### 4. `pytest`

Unit tests live under [tests/](../../../tests/) and run in seconds.
They use small JSONL fixtures under `tests/data/` — no network, no Loki,
no real cache state. See [architecture/testing.md](../../../architecture/testing.md)
for the test scope.

If you added a new event kind, parser branch, or cache helper, add a
test for it in the same commit. The
[ra-log-explorer-architecture-sync](../ra-log-explorer-architecture-sync/SKILL.md)
skill enforces the same idea for the architecture docs.

## End-to-end smoke test (separate from the unit suite)

For UI-visible or fetch-path changes, run the real CLI once against a
known exposure before shipping:

```bash
export LOKI_PASSWORD=...
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267 \
    --no-browser
```

Then `curl http://127.0.0.1:8765/api/summary` and spot-check the
`referencePoints`, `taskColors`, and pod counts. This needs network +
`logcli` + `LOKI_PASSWORD`, so it is **not** part of the standard
validation loop — only do it when the change affects fetching or
serving, not for pure parser/test edits.

## Reporting results

When you report back to the user:

- Say which of the four checks you actually ran. "Validation clean" is
  ambiguous; "pre-commit + mypy + mypy-coverage + pytest all clean" is
  not.
- If you skipped the smoke test because the change didn't touch fetch
  or the UI, say so explicitly.
- If a check failed and you couldn't fix it, leave the relevant todo as
  `in_progress` and flag the blocker rather than declaring done.
