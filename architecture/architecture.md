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
    │   jobs.py              │  FetchJob + JobManager; one daemon thread per
    │   (in-process worker)  │  fetch, append-only event log with condvar
    └─────────┬──────────────┘
              │ ServerState (via stateLock)
              ▼
    ┌────────────────────────┐         GET /                 (home or explore)
    │   server.py            │ ◄────── GET /static/*
    │   (stdlib HTTP + SSE)  │ ◄────── GET /api/summary
    │                        │ ◄────── GET /api/pod/<pod>
    │                        │ ◄────── GET /api/cache
    │                        │ ◄────── POST /api/fetch       (browser side)
    │                        │ ◄────── GET /api/fetch/<id>/status
    │                        │ ◄────── GET /api/fetch/<id>/progress (SSE)
    └────────────────────────┘
                  ▲
                  │ HTML / CSS / JS (vanilla)
                  │
       static/    │           templates/
       ├─ app.js (bootstrap)  └─ timeline.html (home + explore SPA)
       ├─ home.js
       └─ explore.js

       cli.py    optional "eager mode" — fetch + parse on the CLI before
                 the server starts; populates ServerState ahead of time.
                 Home mode just constructs an empty ServerContext.
```

## Module Responsibilities

| Module       | Responsibility                                                                |
|--------------|--------------------------------------------------------------------------------|
| `config.py`  | Defaults, `FetchSpec` dataclass, cache-path helpers.                          |
| `fetch.py`   | `logcli` subprocess wrapper. Lists pods, fetches per-pod JSONL in parallel, manages the on-disk cache, including superset reuse. |
| `parse.py`   | Parses Loki JSONL → `LogLine` → `Event`. Owns the regex taxonomy in [parsing.md](parsing.md). |
| `jobs.py`    | `FetchJob` + `JobManager` — the in-process worker pool the browser uses to kick off and watch fetches. One daemon thread per job, an append-only event log per job (guarded by a `threading.Condition`), and the single `stateLock` that guards the shared `ServerState` handover. |
| `server.py`  | Stdlib `ThreadingHTTPServer` with the JSON / SSE endpoints + static files. Holds a long-lived `ServerContext` containing `{jobs, state}`. Assembles the per-exposure summary payload (events, task colour palette, reference points) when `state` is set; returns `{loaded: false}` otherwise. |
| `cli.py`     | Argument parsing + the optional "eager fetch" path. Builds a `ServerContext` and hands it to `server.serve()`. When `--exposure-id`/`--t-zero` are omitted, hands over an empty context and lets the browser drive. |
| `static/`    | Single-page vanilla JS UI split for clarity: `app.js` (bootstrap, view switching), `home.js` (landing page form + cache list + progress), `explore.js` (timeline + detail drawer). One HTML template (`templates/timeline.html`) holds both `#home-view` and `#explore-view` sections; the bootstrap shows whichever matches the server's `loaded` state. No build step. |

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

### `GET /api/summary`

When no exposure is loaded (home mode):

```jsonc
{
  "loaded": false,
  "cache": { "path": "/Users/.../ra_log_explorer", "totalBytes": 42330276 }
}
```

When an exposure is loaded:

```jsonc
{
  "loaded": true,
  "expId": 2026051900722,
  "tZero": "2026-05-20T08:45:39.267000+00:00",
  "cacheDir": "/Users/.../yagan/rapid-analysis/<window-slug>",
  "cacheBytes": 84115620,
  "meta": { ...fetch metadata, including cacheReuse: "exact"|"superset"|"none"... },
  "referencePoints": [
    { "label": "shutter close (caller-supplied)", "offsetS": 0.0, ... },
    { "label": "head node first defined visit",   "offsetS": 6.95, ... }
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
`[A-Za-z0-9._-]+` allowlist so it can't break out of `pods/`. Returns
`404` when no exposure is loaded.

### `GET /api/cache`

```jsonc
{
  "root":    { "path": ".../ra_log_explorer", "totalBytes": 42330276 },
  "windows": [
    {
      "cluster": "yagan", "namespace": "rapid-analysis",
      "windowDir": "2026-05-20T084534_267000Z__2026-05-20T085039_267000Z",
      "fromIso":   "2026-05-20T08:45:34.267000Z",
      "toIso":     "2026-05-20T08:50:39.267000Z",
      "fetchedAt": "2026-05-21T15:00:00+00:00",
      "podCount":  432, "totalBytes": 42289444, "sizeOnDisk": 42330276
    }, ...
  ]
}
```

Sorted most-recently-fetched-first. Skips directories with a `.partial`
flag or a missing `_meta.json`.

### `POST /api/fetch`

Request body (all defaults match the CLI; the password is consumed by the
fetch worker and never echoed back in any response):

```jsonc
{
  "exposureId": 2026051900722,         // required, integer
  "tZero":      "2026-05-20T08:46:16.267",  // required, ISO-8601
  "tZeroUtc":   false,                 // optional; default false (treat as TAI)
  "username":   "merlin", "password": "...",
  "cluster":    "yagan", "namespace": "rapid-analysis",
  "lokiAddr":   "https://loki-query.ls.lsst.org",
  "workers":    8,
  "windowBefore": 5.0, "windowAfter": 300.0
}
```

Response: `202 Accepted`, `{"jobId": "<12-char hex>"}`. Validation errors
return `400` with `{"error": "..."}`.

### `GET /api/fetch/<jobId>/status`

JSON snapshot of one job:

```jsonc
{
  "jobId": "8970db79c0a6",
  "status": "running" | "parsing" | "done" | "error",
  "expId": 2026051900722, "tZero": "...",
  "fromIso": "...", "toIso": "...",
  "startedAt": "...", "finishedAt": "...",
  "cacheDir": "...", "cacheReuse": "exact"|"superset"|"none"|null,
  "error": null | "...",
  "eventCount": 5
}
```

### `GET /api/fetch/<jobId>/progress`  (Server-Sent Events)

`text/event-stream` of one job's progress events. Each `data:` line is
one JSON event (`{"type": ...}`):

- `start`: `{ fromIso, toIso }` — once at job kick-off.
- `pod-done`: `{ pod, i, total }` — once per fetched pod.
- `parsing`: `{ cacheReuse, podCount, totalBytes }` — after the fetch
  finishes, before `summarizeAll` runs.
- `done`: `{ expId, tZero, cacheDir, cacheReuse, podCount, totalBytes,
  elapsedS }` — after the server's `ServerState` has been swapped in.
- `error`: `{ error }` — terminal; the job failed.

History is replayable: the SSE handler emits every event already in the
job's log on connection, then waits for new ones. Multiple concurrent
readers are fine — each iterates the log independently.

Keepalive comments (`: keepalive\n\n`) are emitted every 15 s so
intermediate proxies don't time the stream out.

## Threading model

`server.serve()` runs a `ThreadingHTTPServer`; one thread per request.
The shared `ServerContext` is mutated only while holding `ctx.jobs.stateLock`,
which guards:

- replacing `ctx.state` after a fetch completes (worker thread → ✓ swap-in);
- reading `ctx.state` to render `/api/summary` or `/api/pod/<>` (request
  threads → read-only snapshot).

Each fetch runs on its own daemon thread spawned by `JobManager.startJob`.
SSE handlers block on `FetchJob.condition.wait()` to be notified when
new events arrive in the per-job event log.

Pod detail responses re-read the JSONL files from disk on each request
rather than buffering them in memory — the cache for one exposure is
typically ~40 MiB so this stays cheap, and lets the user delete files
between requests without confusing the running server.

## Two startup modes

1. **Home mode** — `python3 -m ra_log_explorer.cli` with no
   `--exposure-id`/`--t-zero`. CLI just spins up a fresh `JobManager`
   and an empty `ServerContext`, hands it to `server.serve()`, and the
   user picks an exposure in the browser. Every fetch from then on
   goes through `POST /api/fetch` and the SSE progress endpoint.

2. **Eager fetch mode** — `--exposure-id` + `--t-zero` supplied. CLI
   runs the same TAI-adjustment + `fetch.fetchAll` + `parse.summarizeAll`
   pipeline that the worker thread runs in home mode, but on the main
   thread before the server starts. A populated `ServerContext` is
   handed to `serve()`, so the browser lands directly on the explore
   view. Useful for scripting.

Both modes share `server.py`, `jobs.py`, the JSON API surface, and the
SPA. The difference is only *who first wrote* the initial `ServerState`.

## Non-goals

- Streaming / live tailing of logs. Snapshot-based; one window per run.
- Cross-exposure aggregation or trending. One exposure at a time.
- Authentication. Listens on `127.0.0.1` only.
- A persistent service. Process exits when the user Ctrl-C's, the cache
  persists on disk.
