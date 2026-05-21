# ra_log_explorer

Interactive log exploration tool for the rapid analysis backend (Vera Rubin
Observatory). Given a dataId and a `t=0` reference time (typically shutter
close from the Butler `DimensionRecord`), it pulls every pod's logs from Loki
for a configurable window around that time and presents them in a browser
timeline so you can reconstruct what happened across the distributed pipeline.

## What it does

1. Lists every pod in the `rapid-analysis` namespace that was emitting logs
   during the requested window (via `logcli series`).
2. Downloads each pod's logs in parallel into a local cache. Cache keys are
   the (cluster, namespace, from, to) tuple — re-running the same query hits
   the cache rather than re-querying Loki, as long as the requested window is
   entirely in the past (so the results are stable).
3. Parses each line, extracting structured events for things like:
   - head node: defineVisit, fanout, gather dispatch, mosaic dispatch
   - workers: payload picked up, building quantum graph, quantum start/end,
     binned image written, detector finished
   - any line at WARNING / ERROR level
   - tracebacks (multi-line)
4. Filters to the pods that actually touched the requested dataId.
5. Serves a single-page browser UI showing:
   - a timeline of all relevant pods, with events as markers
   - per-pod log view with deltas from a configurable t=0
   - clickable reference times (shutter close, head node dispatch,
     first ISR start, ...)

## Status

Prototype. Single instrument (LSSTCam). DataId -> shutter-close mapping
must be supplied by the caller for now — the backlog item is to look it
up from the Butler `DimensionRecord` automatically.

## Quick start

```
export LOKI_PASSWORD=...          # set in your shell rc
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267    # shutter-close TAI from DimensionRecord
```

This will fetch the logs (5-minute window after t=0, plus a small pre-shutter
buffer), parse them, and open a browser at `http://localhost:8765`.

`--t-zero` is interpreted as **TAI** by default (the Butler DimensionRecord
convention). Internally we subtract 37 s to land on the real UTC shutter
close before computing the log-fetch window and per-event offsets. Pass
`--t-zero-utc` if your value is already in UTC.

## Cache

Logs live under `~/.cache/ra_log_explorer/`. The CLI prints the cache path
and total size on every run. Flush with:

```
python3 -m ra_log_explorer.cli cache flush
```

or just `rm -rf ~/.cache/ra_log_explorer`.

## Development

Python 3.13. Create a venv and install the dev toolchain:

```
python3.13 -m venv .venv
.venv/bin/pip install pre-commit black isort flake8 flake8-bugbear mypy mypy-coverage pytest
.venv/bin/pre-commit install
```

Validation loop before committing:

```
.venv/bin/pre-commit run --all-files     # black, isort, flake8, whitespace
.venv/bin/mypy                            # configured via mypy.ini (covers ra_log_explorer/ + tests/)
.venv/bin/mypy-coverage                   # annotation coverage; aim for 100%
.venv/bin/pytest -q                       # unit tests against the fixtures under tests/data/
```

See [CLAUDE.md](CLAUDE.md) for the full contributor guide and
[architecture/](architecture/) for design docs.

The runtime itself uses stdlib only (no Flask/FastAPI/etc.); the venv is for
the dev chain. `python3 -m ra_log_explorer.cli ...` will work against any
Python that fromisoformat understands the Loki timestamp format (≥ 3.11).
