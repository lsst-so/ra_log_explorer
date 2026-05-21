# ra_log_explorer — Agent Guide

A standalone tool for reconstructing what happened in the **rapid analysis**
distributed pipeline (the LSST observatory's real-time observation
processing backend) for a single exposure. Given a dataId and a t-zero,
it pulls every pod's logs from Loki for a window around that time and
serves an interactive browser timeline.

This is a **separate project** from rapid analysis itself, which lives at
`../rubintv_production/` on disk and is referred to externally as
`rubintv_production` (a misleading historical name — the system is called
"rapid analysis", not "RubinTV").

## Quick orientation

```
ra_log_explorer/
  ra_log_explorer/             ← Python package (runtime is stdlib-only)
    config.py                    cache paths, FetchSpec, defaults
    fetch.py                     logcli wrapper, parallel per-pod fetch, cache
    parse.py                     log line parser + event classifier
    server.py                    stdlib HTTP server + JSON API
    cli.py                       argument parsing + composition
    static/                      vanilla-JS UI (app.js, style.css)
    templates/                   timeline.html (single-page app shell)
  architecture/                Design docs — keep these in sync with code
    architecture.md              component layout, data flow, JSON API
    parsing.md                   log-line parsing + event taxonomy
    caching.md                   cache layout + reuse policy
    testing.md                   test scope, fixtures, dev loop
  tests/                       Unit tests + fixtures (pytest)
    data/                        sample Loki JSONL files
  .claude/skills/              Per-project agent skills
  pyproject.toml, setup.cfg,
  mypy.ini, .pre-commit-config.yaml   ← lint/type-check config
```

Detailed architecture docs are in [architecture/](architecture/):

- [Architecture & data flow](architecture/architecture.md) — components,
  responsibilities, JSON API surface.
- [Log parsing & event taxonomy](architecture/parsing.md) — regex patterns,
  every event kind, pod classification.
- [Caching](architecture/caching.md) — on-disk layout, exact/superset/none
  reuse policy, `.partial` flag.
- [Testing](architecture/testing.md) — unit-test scope, fixtures, dev loop.

When you change anything that shifts how the system is *shaped* — a new
event kind, a cache key change, a new JSON API field, a structural module
move — update the matching architecture doc in the same commit. The
[ra-log-explorer-architecture-sync](.claude/skills/ra-log-explorer-architecture-sync/SKILL.md)
skill describes when this applies in more detail.

## Naming

Call this project **ra_log_explorer** (with underscore) or
**ra-log-explorer** (with hyphen) interchangeably depending on context
(Python package vs CLI banner / repo / branch names). Do **not** call it
"RubinTV log explorer" — RubinTV is the separate frontend repo. The
project explores logs *for* rapid analysis; it isn't part of either
rubintv_production or RubinTV.

## Coding standards

The conventions deliberately mirror rapid analysis upstream — same line
length, same import sorting, same camelCase identifiers — so anyone moving
between the two projects doesn't have to context-switch.

- **Python version**: 3.13. A `.venv` at the repo root (created from
  pyenv 3.13.x) holds the dev toolchain; the runtime itself is
  stdlib-only.
- **Line length**: 110 (black + flake8 + isort all set to this).
- **Identifier style**: camelCase for functions, methods, variables;
  PascalCase for classes; UPPER_CASE for module-level constants.
  Leading-underscore for module-private helpers. Flake8 ignores
  `N802 / N803 / N806 / N812 / N813 / N815 / N816` so this is enforced.
- **Type hints**: required on every def. `mypy.ini` sets
  `disallow_untyped_defs` and `disallow_incomplete_defs`. Use built-in
  generics (`list[int]`, `dict[str, X]`) and `X | None` — never
  `typing.List` / `typing.Optional`.
- **Docstrings**: numpydoc-ish, but lighter-weight than rapid analysis
  upstream. A one-line summary is usually enough; expand only when the
  *why* isn't obvious from the code.
- **Comments**: avoid restating *what* the code does. Add a comment when
  it captures *why* — a non-obvious constraint, a historical reason, a
  subtle ordering requirement.
- **Backwards compatibility**: none required. This is an end-consumer
  application, not a library. No deprecation shims, no re-exports —
  rename across all call sites in the same commit and move on.

The [ra-log-explorer-code-style](.claude/skills/ra-log-explorer-code-style/SKILL.md)
skill encodes these in agent-readable form.

## Validation loop

Neither pre-commit nor any CI runs `mypy`, `mypy-coverage`, or `pytest`
on this repo today. Before declaring work done, run them by hand:

```bash
.venv/bin/pre-commit run --all-files    # black, isort, flake8, whitespace
.venv/bin/mypy                          # configured by mypy.ini
.venv/bin/mypy-coverage                 # aim for 100% body-checked
.venv/bin/pytest -q                     # unit tests
```

End-to-end smoke test: run the CLI against the real cluster for a known
exposure (see [architecture/testing.md](architecture/testing.md)) before
shipping anything UI-visible.

The [ra-log-explorer-validation](.claude/skills/ra-log-explorer-validation/SKILL.md)
skill is the checklist.

## Skills

Project-scoped skills live under `.claude/skills/` and load automatically
when their triggering context matches:

- **ra-log-explorer-validation** — the pre-commit + mypy +
  mypy-coverage + pytest validation loop to run after editing Python
  here, since nothing automates it.
- **ra-log-explorer-code-style** — naming, formatting, type-annotation,
  and docstring conventions when writing or editing Python here.
- **ra-log-explorer-architecture-sync** — keeping `architecture/*.md` in
  step with code changes that touch the system's shape (event kinds,
  cache key, JSON API, module layout).

## Working on the UI

The UI is plain HTML + CSS + JS, no build step. Edit
[ra_log_explorer/static/app.js](ra_log_explorer/static/app.js) and
[ra_log_explorer/templates/timeline.html](ra_log_explorer/templates/timeline.html)
directly; reload the browser. The server serves static files with
`Cache-Control: no-store` so reloads pick up changes immediately.

There's no UI test framework today; hand-verify in a browser. When you
change the JSON API shape, update [architecture/architecture.md](architecture/architecture.md)
and the matching JSON-handling code in `app.js` in the same commit.

## Working on the parser

When the rapid analysis backend introduces a new log message worth
recognising, add the pattern in three places **in the same commit**:

1. [ra_log_explorer/parse.py](ra_log_explorer/parse.py) — the regex,
   the `classify()` branch, the Event kind constant.
2. [architecture/parsing.md](architecture/parsing.md) — the table row.
3. [tests/test_parse.py](tests/test_parse.py) — the smallest fixture
   line that reproduces the match, and an assertion pinning the parsed
   event.

If you can't write a meaningful test (e.g. the pattern depends on a
multi-line state machine), say so in the commit message.

## Git workflow

- Trunk is `main`. Feature work happens on `tickets/DM-NNNNN` branches
  named after the Jira ticket.
- New commits should pass the full validation loop locally before
  push.
- Squash-merge ticket branches to `main` via PR; no fast-forward of
  unsquashed history. (Convention; not yet automated.)

## What this project is not

- Not a library. Nothing imports from `ra_log_explorer` outside this
  repo, so refactor freely.
- Not a service. Each run launches the server on `127.0.0.1` and
  exits when the user hits Ctrl-C.
- Not coupled to a Butler. The Butler-side lookup of `dataId →
  shutter close` is the caller's responsibility for now; the tool
  starts from a pre-supplied t-zero.
