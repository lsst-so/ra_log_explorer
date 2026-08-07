---
name: ra-log-explorer-code-style
description: Apply the ra_log_explorer code style — camelCase identifiers, PascalCase classes, built-in type hints with "| None" (never typing.Optional/List/Dict), light numpydoc-ish docstrings, and black/isort formatting at line-length 110. Use this skill whenever writing or editing Python in this repo (adding a function/method/class, refactoring, renaming, writing a test). The conventions deliberately mirror rapid analysis upstream (`rubintv_production`); do not "fix" them to PEP-8 even if they look unusual — the divergences are intentional house style.
---

# ra_log_explorer: Code style

These are the same conventions as the rapid analysis backend, so that
moving between this tool and `rubintv_production` is friction-free. They
deliberately diverge from PEP-8 in places; do not normalise them.

## Identifiers

- **Functions / methods / variables**: `camelCase` —
  `summarizePod`, `cacheBytes`, `findSupersetCache`.
- **Classes**: `PascalCase` — `LogLine`, `PodSummary`, `ServerState`.
- **Module-level constants**: `UPPER_SNAKE_CASE` —
  `DEFAULT_WINDOW_BEFORE_S`, `TAI_MINUS_UTC_S`. Constants pinned to
  internal data structures (palettes, fixtures) get a leading
  underscore: `_TASK_PALETTE`.
- **Module-private helpers**: leading underscore —
  `_parseIso`, `_assignTaskColors`, `_eventToDict`.
- **Acronyms**: keep them all-caps in `PascalCase` (`HTTPHandler`,
  `JSONAPI`) but lower-case the first letter when starting a camelCase
  identifier: `httpServer`, `jsonPayload`. The `flake8` config ignores
  N802/N803/N806/N812/N813/N815/N816 to make this work.

## Type hints

- Required on every `def`. `mypy.ini` enforces `disallow_untyped_defs`
  and `disallow_incomplete_defs`.
- Use **built-in generics**: `list[int]`, `dict[str, Path]`,
  `tuple[str, int]`. Never `typing.List`, `typing.Dict`, `typing.Tuple`.
- Use **PEP-604 unions**: `int | None`, `str | bytes`. Never
  `typing.Optional` or `typing.Union`.
- `from __future__ import annotations` is fine and used throughout —
  it keeps annotations lazy and side-steps forward-reference issues.

## Formatting

- **Line length**: 110.
- **Imports**: isort with the `black` profile, line length 110,
  `known_first_party = ["ra_log_explorer"]`. Configured in
  `pyproject.toml`.
- **Quotes**: black's default (double quotes for strings, single only
  when the string itself contains a double quote).
- **Trailing commas**: black handles them; don't fight it.

## Docstrings

Lighter than rapid analysis upstream — a one-line summary on most
functions; expand only when something subtle deserves explanation.

- For private helpers: omit the docstring if the name is descriptive.
- For public/exported functions: one-line summary, plus a short
  `Returns:` / `Raises:` paragraph if non-obvious.
- For dataclasses: a one-line module-level intent, inline field
  comments where the meaning isn't obvious from the name.

Reserve numpydoc-style `Parameters` / `Returns` blocks for cases where
the function genuinely has parameter semantics that need explaining.
Avoid templating boilerplate that just restates the type hints.

## Comments

Write a comment when the **why** is non-obvious and the reader can't
recover it from the code alone:

- A historical/empirical reason ("Python's built-in hash is salted between
  processes so we use SHA-256 here").
- A subtle ordering requirement ("Run before the per-who gather dispatches
  so we still see the exposure in the active set").
- A workaround for an upstream quirk.

Don't write a comment that restates **what** the code does, name the
variables, or paraphrases the next line.

## Errors

Use `raise X from y` to chain exceptions (`from None` to suppress). The
`fetch.FetchError` class wraps any underlying `subprocess.*` failure so
callers see one exception type from this layer.

## Functions over classes

Prefer plain functions for stateless work. Reach for a class when there
is genuinely shared mutable state, an iterator, or a context manager. The
existing dataclasses (`FetchSpec`, `LogLine`, `Event`, `PodSummary`,
`ServerState`) are all frozen-ish data carriers, not OO behaviour
containers.

## Configuration is read from the environment, once

The tool is a deployed service and one process serves every user, so
anything that varies between deployments is an environment variable read
at import in `config.py` — never a request field, never a UI control,
never a file the app writes to itself. If you catch yourself adding a
form input for a setting, it belongs in the environment instead (and
therefore in the Helm chart — see the
[architecture-sync](../ra-log-explorer-architecture-sync/SKILL.md) skill).

The house pattern:

```python
DEFAULT_WORKERS = _envInt("RA_LOG_EXPLORER_WORKERS", 8)
```

- Name the variable `RA_LOG_EXPLORER_*`, except where an external tool
  already owns the name (`LOKI_PASSWORD`, `LOKI_USERNAME`).
- Read it through `_envInt` / `_envFloat`, which **raise `ConfigError` on
  a malformed value** rather than falling back to the default. That is
  the point: a container that refuses to start is noticed immediately,
  whereas a setting that silently never took effect is not noticed for
  months.
- Treat empty or whitespace-only as unset — that's what an unset Helm
  value renders as.
- Keep the default sensible for a laptop, so a local run needs no
  environment at all.

Request handlers must not read configuration out of the request body.
`_buildSpecFromRequest` and friends take the exposure, the window and
nothing else; a body carrying `site`, `username`, `password` or `workers`
is ignored, and there are tests pinning that.

## When in doubt, match the existing files

`parse.py`, `fetch.py`, and `server.py` are the canonical style examples.
If a new file you're adding looks visually different from those, ask
why before committing.
