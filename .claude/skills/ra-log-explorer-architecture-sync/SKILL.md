---
name: ra-log-explorer-architecture-sync
description: Keep the architecture/ docs in sync with code changes in ra_log_explorer. Use this skill whenever a change touches the *shape* of the system — a new event kind or regex pattern in parse.py, a cache-key change in config.py or fetch.py, a new field on the /api/summary JSON, a new pod-group classification, a new task-palette policy, a new CLI flag that affects behaviour, or a module move. The architecture/ files are the canonical source of truth for the system's design; letting them drift from the code is the single biggest way this project would accumulate onboarding debt. Update the relevant doc in the same commit as the code change — not "later". Also apply when the user asks to "document this change", "update the architecture", or reviews a commit that obviously needs doc updates.
---

# ra_log_explorer: Keeping architecture docs in sync

The `architecture/` directory is the source of truth for *how* this
system is shaped — what the pieces are, what they're responsible for,
what's on the wire, and what's on disk. The Python source is the source
of truth for *current behaviour*; the architecture docs explain it.

When the two drift, anyone new to the project learns the wrong thing and
the docs eventually get discarded. **Update the relevant doc in the same
commit as the code change** — don't leave it for later.

## What lives where

| File                                           | Source-of-truth for                                                 |
|------------------------------------------------|----------------------------------------------------------------------|
| [README.md](../../../README.md)                                       | End-user install/run instructions, CLI flag table, common workflows, troubleshooting |
| [architecture/architecture.md](../../../architecture/architecture.md) | Module responsibilities, top-level data flow, JSON API surface |
| [architecture/parsing.md](../../../architecture/parsing.md)           | The complete event-kind taxonomy, the `_PYLOG_RE` shape, pod classification |
| [architecture/caching.md](../../../architecture/caching.md)           | On-disk layout, cache-hit policy (exact/superset/none), `.partial` flag |
| [architecture/testing.md](../../../architecture/testing.md)           | Unit-test scope, fixture inventory, end-to-end smoke procedure |

## Which doc to update for which change

| If you change…                                                  | Update…                                                            |
|------------------------------------------------------------------|---------------------------------------------------------------------|
| A regex in `parse.py`                                           | `architecture/parsing.md` (the table)                              |
| The Event dataclass shape                                       | `architecture/parsing.md`                                           |
| `POD_GROUPS` / `podOrdinal` / `podInstrument`                   | `architecture/parsing.md`                                           |
| Cache directory layout, `.partial`, hit policy                  | `architecture/caching.md`                                           |
| `FetchSpec`, `cache_root`, the env-override variable             | `architecture/caching.md`                                           |
| Anything in `/api/summary`, `/api/pod/<>`, `/api/cache`, `/api/fetch*` | `architecture/architecture.md` (the JSON API section)         |
| The home-mode ⇄ explore-mode flow, or how `ServerContext` works | `architecture/architecture.md` ("Threading model", "Two startup modes") |
| `jobs.py` event-kind names, `JobStatus`, or the SSE event schema | `architecture/architecture.md` (the SSE section)                  |
| Module split / rename / new file under `ra_log_explorer/`         | `architecture/architecture.md` (the table + tree)                  |
| Default window size, TAI/UTC handling                           | `architecture/architecture.md` ("Key Concepts") **and** README     |
| New unit-test category or new fixture                           | `architecture/testing.md`                                           |
| New CLI flag, renamed flag, or changed default                  | **README** (CLI options + workflow examples)                       |
| New UI feature, browser-visible behaviour, or shipped style change | **README** ("What the UI shows" / "Common workflows")           |
| New troubleshooting failure mode or new env-var dependency      | **README** ("Troubleshooting" / "What you need")                   |

## What doesn't need a doc update

- Internal helper renames that don't change behaviour.
- Type-annotation tightening, comment edits, log message wording.
- Style fixes that pre-commit / black / isort do for you.
- Bug fixes whose only visible effect is "the existing documented
  behaviour now actually works".

When in doubt: if a fresh reader of the architecture docs would walk away
with the wrong mental model after your change, the docs need updating.

## Spotting drift

Before you commit, ask yourself:

1. Did I add or change a *kind* of log line we recognise? → `parsing.md`
2. Did I change *what* gets cached or *when* we reuse a cache? → `caching.md`
3. Did I add or change a JSON field a client reads? → `architecture.md`
4. Did I add a new module or move code between modules? → `architecture.md`
5. Did I add a new test category? → `testing.md`
6. Did I add a CLI flag, rename one, change a default, or alter
   user-visible UI behaviour? → `README.md`
7. Did I introduce a new failure mode an end-user might hit
   (missing binary, new env var, new error)? → `README.md`
   (Troubleshooting section)

If you answered "yes" to any of those and your commit doesn't touch the
matching file, you almost certainly have drift to fix.

## How to flag a deliberate gap

If a code change *should* trigger a doc update but you can't make a
sensible one yet (e.g. the design is still in flux), say so in the commit
message:

> Doc update deferred: the new `cacheTouch` field is experimental and may
> be removed before this lands. Will document under `caching.md` once it
> stabilises.

Don't silently skip; an explicit deferral is far easier to chase than a
missing one.

## When updating prose

- Match the existing tone — terse, technical, second-person when
  addressing the reader, third-person when describing the code.
- Code examples in fenced blocks; use `jsonc` for JSON-with-comments.
- Cross-link with relative paths: `[parsing.md](parsing.md)`,
  `[../tests/test_parse.py](../tests/test_parse.py)`.
- Keep tables to columns that fit in ~120 characters of fixed-width
  text.
