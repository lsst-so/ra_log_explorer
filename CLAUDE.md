# ra_log_explorer — Agent Guide

A tool for reconstructing what happened in the **rapid analysis**
distributed pipeline (the LSST observatory's real-time observation
processing backend) for a single exposure. Given a dataId and a t-zero,
it pulls every pod's logs from Loki for a window around that time and
serves an interactive browser timeline.

**It is a deployed service.** It runs as the Phalanx application
`log-explorer` on the two clusters whose pipelines it explains — the Base
Test Stand (`manke`) and the summit (`yagan`) — at
`https://<fqdn>/log-explorer`, behind Gafaelfawr, from the container
image this repo builds. That is how everyone who uses it uses it, and it
is the code path that must keep working.

Running it on a laptop (`python3 -m ra_log_explorer.cli`) also works and
is how this repo is developed, but treat it as a development
convenience: it binds `127.0.0.1`, has no authentication, serves at the
root rather than under a path prefix, and reads a ConsDB bearer token
from the developer's home directory because it is outside the cluster.
When the two disagree, the deployed mode is the one that is right.

Two consequences worth holding on to while you work here:

- **Configuration is environment variables, not UI.** One process serves
  every user, so nothing a visitor can set is allowed to change how the
  service behaves for anyone else. If you find yourself adding a form
  field for a setting, it belongs in the environment (and therefore in
  the Helm chart) instead.
- **The Helm chart is in another repo.** It lives in
  [Phalanx](https://github.com/lsst-sqre/phalanx) under
  `applications/log-explorer/`. A change to the configuration surface
  here needs a matching change there, or the deployment silently runs on
  defaults.

This is a **separate project** from rapid analysis itself, which lives at
`../rubintv_production/` on disk and is referred to externally as
`rubintv_production` (a misleading historical name — the system is called
"rapid analysis", not "RubinTV").

## Quick orientation

```
ra_log_explorer/
  Dockerfile                   ← the deployed artefact; logcli pinned, see below
  .dockerignore
  .github/workflows/
    build.yaml                   ← builds + pushes ghcr.io/lsst-so/ra_log_explorer
    ci.yaml, mypy-coverage.yaml
  ra_log_explorer/             ← Python package (runtime is stdlib-only)
    config.py                    cache paths, FetchSpec, env-read defaults
    fetch.py                     logcli wrapper, parallel per-pod fetch, cache
                                 + live-night slicing (timestamp bisect)
    parse.py                     log line parser + event classifier
    night.py                     dayObs-wide rollups over PodSummary (no I/O)
    exposureTimes.py             dataId → ConsDB exposure record (t-zero + the
                                 info-box properties), per-site on-disk cache
    sites.py, sites.toml         the site catalog: Loki cluster ⇄ its ConsDB
    live.py                      live-mode poller: keeps the current night
                                 fetched so views are served from disk
    jobs.py                      FetchJob + JobManager: a thread per fetch,
                                 the SSE event log, the shared stateLock
    server.py                    stdlib HTTP server + JSON API
    cli.py                       argument parsing + composition; cache subcmds
    static/                      ← vanilla-JS UI, no build step: app.js
                                 (bootstrap/routing + history), home.js,
                                 explore.js, night.js, range.js, style.css,
                                 favicon.png (tab icon), logo.png (the topbar
                                 mark; both derived from assets/)
    templates/                   timeline.html (single-page app shell)
  architecture/                Design docs — keep these in sync with code
    architecture.md              component layout, data flow, JSON API
    parsing.md                   log-line parsing + event taxonomy
    caching.md                   cache layout + reuse policy
    testing.md                   test scope, fixtures, dev loop
  tests/                       Unit tests + fixtures (pytest)
    data/                        sample Loki JSONL files
    data/ui/july11.tar.gz        a real cut-down night; the browser tests' corpus
    ui/                          Playwright browser tests (see testing.md)
  assets/                      Full-resolution source art for the two images
                               in static/; not packaged, not in the build context
  tools/                       Dev scripts that hit the real cluster (not shipped)
    captureNight.py              fetch whole nights into master corpora
    stageNight.py                clone masters into a cache root to serve
  .claude/skills/              Per-project agent skills
  pyproject.toml, setup.cfg,
  mypy.ini, .pre-commit-config.yaml   ← lint/type-check config
```

The top-level [README.md](README.md) is the **end-user-facing** doc:
install, run, CLI flags, common workflows, troubleshooting. Keep that
in sync when your change is user-visible — the
[ra-log-explorer-architecture-sync](.claude/skills/ra-log-explorer-architecture-sync/SKILL.md)
skill has the rules.

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
- **Backwards compatibility**: none, ever — and this is a permanent
  policy, not a phase. This is an end-consumer application, not a
  library, and its caches are a temporary convenience, not a data store
  anyone supports. No deprecation shims, no re-exports, no tolerant
  readers for old on-disk formats, no migration code: rename across all
  call sites in the same commit and move on, and when an on-disk shape
  changes (`_meta.json`, `_live.json`, `_range.txt`,
  `_exposure_ids.txt`, the exposure-time records), bump
  `CACHE_SCHEMA_VERSION` in the same commit and delete the old reader
  outright. Deploys invalidate everything intentionally;
  the schema flush is the upgrade path. See *No backwards
  compatibility* in [architecture/caching.md](architecture/caching.md).

The [ra-log-explorer-code-style](.claude/skills/ra-log-explorer-code-style/SKILL.md)
skill encodes these in agent-readable form.

## Validation loop

CI (`ci.yaml`) runs pytest and mypy on PRs and pushes to `main`, and
`build.yaml` re-runs both before it will push an image — but pre-commit
itself runs none of them, and neither fires on a local commit. Run them
by hand before declaring work done:

```bash
.venv/bin/pre-commit run --all-files    # black, isort, flake8, whitespace
.venv/bin/mypy                          # configured by mypy.ini
.venv/bin/mypy-coverage                 # aim for 100% body-checked
.venv/bin/pytest -q                     # unit tests
```

Two smoke tests sit outside that loop, both described in
[architecture/testing.md](architecture/testing.md):

- **Container smoke test** — build the image and exercise it under the
  deployment's conditions (read-only root filesystem, base path,
  configuration only from the environment). Do this for anything
  touching the Dockerfile, the config surface, the base path, or the
  served HTML. Nothing in the unit suite substitutes for it, and it is
  the mode everyone actually runs.
- **End-to-end smoke test** — run against the real cluster for a known
  exposure. Do this for fetch-path work.

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
- **ra-log-explorer-architecture-sync** — keeping `architecture/*.md`
  **and `README.md`** in step with code changes that touch the system's
  shape (event kinds, cache key, JSON API, module layout, CLI flags,
  user-visible UI behaviour, troubleshooting failure modes).
- **ra-log-explorer-loki** — Loki / `logcli` conventions and gotchas
  when editing the fetch path in `fetch.py` or writing one-off scripts
  that hit the cluster's Loki.
- **ra-log-explorer-test-data** — capturing whole nights from the
  cluster into master corpora and staging them locally, when you need
  real data at real scale to develop or demo against.

## Working on the UI

The UI is plain HTML + CSS + JS, no build step. Edit
[ra_log_explorer/static/app.js](ra_log_explorer/static/app.js) and
[ra_log_explorer/templates/timeline.html](ra_log_explorer/templates/timeline.html)
directly; reload the browser. The server serves static files with
`Cache-Control: no-store` so reloads pick up changes immediately.

**The UI has browser tests** — [tests/ui/](tests/ui/), Playwright
driving Chromium against the real server and a real (cut-down) night of
captured logs. They run as part of `pytest`; they are not optional and
they never skip. Add to them when you add UI behaviour: the whole point
is that "I clicked through it once" stops being the only evidence.

```bash
pip install -e '.[ui-test]' && playwright install chromium   # once
.venv/bin/pytest -n auto                                     # 32 s for everything
```

Hand-verification still has a job — layout, colour, whether a thing
*reads* well — but not for "does this still work".

When you change the JSON API shape, update
[architecture/architecture.md](architecture/architecture.md) and the
matching JSON-handling code in `app.js` in the same commit.

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
- Not a general-purpose log browser. It answers "what happened to this
  exposure / this night", not arbitrary log search — see the non-goals
  in [architecture/architecture.md](architecture/architecture.md).
- Not coupled to a Butler. The Butler-side lookup of `dataId →
  shutter close` is the caller's responsibility for now; the tool
  starts from a pre-supplied t-zero.
