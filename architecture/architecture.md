# ra_log_explorer — Architecture & Data Flow

A standalone tool for reconstructing what happened in the rapid analysis
distributed pipeline for a given exposure. Given a dataId and a t-zero
(shutter close from the Butler `DimensionRecord`), it pulls every pod's
logs from Loki for a configurable window, parses them into structured
events, and serves an interactive browser timeline.

Sibling docs:

- [Log line parsing & event taxonomy](parsing.md)
- [Caching & cache reuse](caching.md)
- [Testing](testing.md)

## Component Overview

```
       (cluster Loki via logcli)
                 │
                 ▼
    ┌────────────────────────┐
    │   fetch.py             │  parallel per-pod queries, on-disk JSONL cache
    │   (listPods, fetchAll) │
    └─────────┬──────────────┘
              │ pods/<pod>.jsonl
              ▼
    ┌────────────────────────┐
    │   parse.py             │  per-pod summaries with structured events
    │   (summarizeAll)       │
    └─────────┬──────────────┘
              │ list[PodSummary]
              ▼
    ┌────────────────────────┐
    │   cli.py               │  composes fetch + parse + ServerState,
    │                        │  applies TAI→UTC for t-zero, launches server
    └─────────┬──────────────┘
              │
              ▼
    ┌────────────────────────┐         GET /
    │   server.py            │ ◄────── GET /static/*       browser
    │   (stdlib HTTP)        │ ◄────── GET /api/summary
    │                        │ ◄────── GET /api/pod/<pod>
    └────────────────────────┘
                  ▲
                  │ HTML / CSS / JS (vanilla)
                  │
              static/  templates/
```

## Module Responsibilities

| Module       | Responsibility                                                                |
|--------------|--------------------------------------------------------------------------------|
| `config.py`  | Defaults, `FetchSpec` dataclass, cache-path helpers.                          |
| `fetch.py`   | `logcli` subprocess wrapper. Lists pods, fetches per-pod JSONL in parallel, manages the on-disk cache, including superset reuse. |
| `parse.py`   | Parses Loki JSONL → `LogLine` → `Event`. Owns the regex taxonomy in [parsing.md](parsing.md). |
| `server.py`  | Stdlib `ThreadingHTTPServer` with two JSON endpoints + static files. Assembles the per-exposure summary payload, including the task colour palette. |
| `cli.py`     | Argument parsing, TAI→UTC adjustment on `--t-zero`, window calculation, calls into `fetch` + `parse`, hands a `ServerState` to `server.serve()`. |
| `static/`    | Single-page vanilla JS UI (`app.js`, `style.css`) and the HTML template (`templates/timeline.html`). No build step. |

## Key Concepts

- **dataId / expId** — 13-digit `YYYYMMDDSSSSS` integer (e.g. `2026051900722`).
  The tool targets a single exposure at a time.

- **t-zero** — the reference time the timeline's `0s` line corresponds to.
  Conventionally the shutter-close time from `DimensionRecord.timespan.end`,
  which is **TAI**. The CLI subtracts 37 s by default; `--t-zero-utc` opts
  out. `cli.py:TAI_MINUS_UTC_S` holds the constant.

- **Window** — the (UTC) `[from, to]` time range we query Loki for. Defaults
  to `(t-zero - 5 s, t-zero + 5 min)`. The expId should never appear in logs
  before its shutter close; if it does, that's a real anomaly, not a window
  problem.

- **PodSummary** — per-pod parsed-log statistics: line/warn/error/traceback
  counts, the set of exposure IDs seen anywhere in the file, plus the
  classified `Event` list.

- **Event** — a structured fact extracted from one log line: `kind` (string
  enum, e.g. `HEAD_DEFINE_VISIT`, `WORKER_PICKUP`, `QUANTUM_DONE`) plus
  optional `expId`, `detector`, `visit`, `who`, `taskLabel`, `durationS`,
  `flavor`.

- **Pod group** — coarse classification of a pod by name substring
  (`head`, `sfm`, `aos`, `step1b`, `step1b-aos`, `mosaic`, `psf-plot`, …).
  Drives the row grouping in the UI. Owned by `parse.POD_GROUPS`; order
  matters because the match is "first wins".

- **`who`** — pipeline tag (`SFM`, `AOS`, `AOS_DANISH`, `ISR`, …) that the
  rapid analysis system uses to distinguish concurrent pipelines on the
  same exposure. Surfaced on head-node events.

## JSON API

The server exposes two endpoints; both return JSON.

### `GET /api/summary`

```jsonc
{
  "expId": 2026051900722,
  "tZero": "2026-05-20T08:45:39.267000+00:00",
  "cacheDir": "/Users/.../yagan/rapid-analysis/<window-slug>",
  "cacheBytes": 84115620,
  "meta": { ...fetch metadata, including cacheReuse: "exact"|"superset"|"none"... },
  "referencePoints": [
    { "label": "shutter close (caller-supplied, TAI input)", "offsetS": 0.0, ... },
    { "label": "head node first defined visit",              "offsetS": 6.95, ... }
  ],
  "taskColors": { "isr": "#d4801f", "calibrateImage": "#1a9c8c", ... },
  "pods":    [ { "pod": "...", "group": "sfm", "ordinal": 0, "nLines": 541,
                 "nWarn": 39, "nError": 0, "events": [ ... ] }, ... ],
  "podsAll": [ { "pod": "...", "group": "...", "nLines": N, ... }, ... ]
}
```

`pods` is the subset whose `expIdsSeen` contains `expId`; `podsAll` is every
pod that emitted in the window.

### `GET /api/pod/<podName>`

Returns every parsed `LogLine` from that pod's JSONL file:

```jsonc
{
  "pod": "s-lsstcam-run-head-node-...",
  "lines": [
    { "t": "...", "offsetS": -1.234, "level": "info",
      "logger": "lsst.rubintv.production.processControl.HeadProcessController",
      "function": "doDetectorFanout",
      "message": "Fanning ...", "raw": "<full original line>" },
    ...
  ]
}
```

Used by the detail drawer in the UI. `podName` is checked against a
`[A-Za-z0-9._-]+` allowlist so it can't break out of `pods/`.

## Threading model

`server.serve()` uses `ThreadingHTTPServer`. Both endpoints read shared
`ServerState` but never mutate it after the CLI assembles it, so no locks
are needed. Pod detail responses re-read the JSONL files from disk on each
request rather than buffering them in memory — the cache for one exposure
is typically ~40 MiB so this stays cheap, and lets the user delete files
between requests without confusing the running server.

## Non-goals

- Streaming / live tailing of logs. Snapshot-based; one window per run.
- Cross-exposure aggregation or trending. One exposure at a time.
- Authentication. Listens on `127.0.0.1` only.
- A persistent service. Process exits when the user Ctrl-C's, the cache
  persists on disk.
