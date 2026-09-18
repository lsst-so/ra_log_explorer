---
name: ra-log-explorer-validation
description: Validate Python or UI changes in the ra_log_explorer tool before declaring a task done. Nothing runs pre-commit, mypy, mypy-coverage, or pytest on a local commit, so the checks must be run by hand. UI changes are covered by browser tests (tests/ui/) that run as part of pytest — they need `pip install -e '.[ui-test]'` + `playwright install chromium` once, and they fail rather than skip without it. Use this skill whenever you finish editing anything under `ra_log_explorer/`, `tests/`, or `architecture/` and are about to hand the task back to the user; when the user asks to "run the tests", "type check", "validate", or "check my changes"; or when you are about to stage a commit. The validation loop is the same whether the change is to Python, JS, CSS, or HTML — every commit is expected to pass it. For anything touching the Dockerfile, the configuration surface, the base path or the served HTML, the container smoke test is part of validation too: the tool is a deployed service and the unit suite does not cover the shape it runs in.
---

# ra_log_explorer: Validating changes

CI does run pytest and mypy — `ci.yaml` on PRs and pushes to `main`, and
`build.yaml` gates the image push on the same two. But **nothing runs on
a local commit**: pre-commit only does formatting and flake8, so a type
error or a broken test gets committed happily and you find out minutes
later in Actions, or not at all on a branch nobody opens a PR for. Run
them yourself.

`mypy-coverage` runs in CI as an annotation-only job with no threshold,
so it can never fail a build. If you let coverage slip, the only thing
that catches it is you.

## The validation loop

From the repo root, with the dev venv created (`python3.13 -m venv .venv
&& .venv/bin/pip install pre-commit black isort flake8 flake8-bugbear mypy
mypy-coverage pytest` and `.venv/bin/pip install -e '.[ui-test]' &&
.venv/bin/playwright install chromium` for the browser tests):

```bash
.venv/bin/pre-commit run --all-files
.venv/bin/mypy
.venv/bin/mypy-coverage
.venv/bin/pytest -q -n auto
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

### 4. `pytest -n auto`

Two kinds of test, one command. Unit tests under
[tests/](../../../tests/) run in seconds against small JSONL fixtures in
`tests/data/` — no network, no Loki, no real cache state. **Browser
tests** under [tests/ui/](../../../tests/ui/) drive Chromium against the
real server over a real cut-down night of logs; they cover the UI and
the integration behind it. See
[architecture/testing.md](../../../architecture/testing.md) for the
scope of both.

`-n auto` is worth typing. Measured on 8 cores: the unit tests take 75 s
serially and 31 s in parallel, and adding all the browser tests to the
parallel run costs **0.1 s** — they parallelise far better than the unit
suite. Serially they double the wall clock.

The browser tests **fail rather than skip** when Playwright or its
Chromium build is missing, on purpose: a UI suite that skips itself
reads exactly like one that passes. If you see the install message, run
the two commands it names — do not reach for `-m "not ui"` to make it
quiet.

If you added a new event kind, parser branch, or cache helper, add a
test for it in the same commit. **If you changed the UI — a new control,
a new panel, a changed selector — add or update a browser test in the
same commit too.** Hand-verification still has a job (layout, colour,
whether a thing reads well) but it is no longer the evidence that
something works. The
[ra-log-explorer-architecture-sync](../ra-log-explorer-architecture-sync/SKILL.md)
skill enforces the same idea for the architecture docs.

## Container smoke test (the mode that actually ships)

**Required** for any change touching the Dockerfile, the configuration
surface (a new or renamed environment variable), the base path, the
served HTML, or anything a read-only root filesystem could break.

The tool is a deployed service — the Phalanx `log-explorer` application
on BTS and the summit — and a laptop run is a development convenience.
The unit suite runs the Python; it does not run the *image*, under a path
prefix, with a read-only root, configured only from the environment. A
change that passes every test here and breaks there has broken the only
mode anyone uses.

The procedure and the specific checks are in
[architecture/testing.md](../../../architecture/testing.md#container-smoke-test).
The short version:

```bash
docker build -t ra-log-explorer:local .
docker run --rm --read-only --tmpfs /tmp --tmpfs /var/cache/ra-log-explorer \
  -e RA_LOG_EXPLORER_BASE_PATH=/log-explorer ... ra-log-explorer:local
curl localhost:8080/log-explorer/healthz     # the readiness probe path
curl localhost:8080/                          # must 404 — outside the prefix
```

If you added an environment variable, **also add it to the Helm chart**
in the [Phalanx](https://github.com/lsst-sqre/phalanx) repo under
`applications/log-explorer/`. A variable this code reads that the chart
never sets means production silently runs on the default — a setting that
appears to do nothing, which is the sort of thing nobody notices for
months.

## End-to-end smoke test (separate from the unit suite)

For fetch-path changes, run the real CLI once against a known exposure
before shipping:

```bash
export LOKI_PASSWORD=...
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267 \
    --no-browser
```

Then `curl http://127.0.0.1:8780/api/summary` and spot-check the
`referencePoints`, `taskColors`, and pod counts. This needs network +
`logcli` + `LOKI_PASSWORD`, so it is **not** part of the standard
validation loop — only do it when the change affects fetching or
serving, not for pure parser/test edits.

## Reporting results

When you report back to the user:

- Say which of the four checks you actually ran. "Validation clean" is
  ambiguous; "pre-commit + mypy + mypy-coverage + pytest all clean" is
  not.
- If you touched the UI, say whether the browser tests covered the
  change or whether you added one. "The UI tests pass" is weak evidence
  for a control none of them touch.
- Say whether you ran the **container** smoke test, and if not, why the
  change couldn't affect the deployed shape. This is the one most easily
  skipped and the most costly to skip.
- If you skipped the end-to-end smoke test because the change didn't
  touch the fetch path, say so explicitly.
- If a check failed and you couldn't fix it, leave the relevant todo as
  `in_progress` and flag the blocker rather than declaring done.
