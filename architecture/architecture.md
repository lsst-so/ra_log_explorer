# ra_log_explorer — Architecture & Data Flow

A standalone tool for reconstructing what happened in the rapid analysis
distributed pipeline. Given **either** a dataId + t-zero (single
exposure) **or** a dayObs (night-wide AOS survey), it pulls every pod's
logs from Loki for the relevant window, parses them into structured
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
    ┌────────────────────────────┐
    │   fetch.py                 │  parallel per-pod queries, on-disk JSONL cache
    │   (listPods, fetchAll)     │  + LRU eviction + .partial flag + sidecars
    └─────────────┬──────────────┘
                  │ pods/<pod>.jsonl
                  ▼
    ┌────────────────────────────┐
    │   parse.py                 │  Loki JSONL → LogLine → Event;
    │   (summarizeAll)           │  carryover-aware dataId attribution;
    │                            │  traceback capture into TracebackRecord
    └─────────────┬──────────────┘
                  │ list[PodSummary]
                  ▼
    ┌────────────────────────────┐
    │   night.py                 │  dayObs-wide rollups: top stats, errors-
    │   (errorsByType, …)        │  by-type, histograms, failureRows. Pure
    │                            │  list-and-dict massaging over PodSummary.
    └─────────────┬──────────────┘
                  │
                  ▼
    ┌────────────────────────────┐
    │   jobs.py                  │  FetchJob + JobManager; one daemon thread
    │   (in-process worker pool) │  per fetch (exposure OR night), append-only
    │                            │  event log + threading.Condition; the
    │                            │  shared stateLock.
    └─────────────┬──────────────┘
                  │ ServerState / NightState (via stateLock)
                  ▼
    ┌────────────────────────────┐         GET    /                          (home/explore/night SPA)
    │   server.py                │ ◄────── GET    /static/*
    │   (stdlib HTTP + SSE)      │ ◄────── GET    /api/summary?dataId=…
    │                            │ ◄────── GET    /api/summary?dayObs=…
    │                            │ ◄────── GET    /api/pod/<pod>?dataId|dayObs=…
    │                            │ ◄────── GET    /api/night/traceback/<key>?dayObs=…
    │                            │ ◄────── GET    /api/cache                  (lists windows)
    │                            │ ◄────── DELETE /api/cache                  (wipe all)
    │                            │ ◄────── DELETE /api/cache/.../<slug>[/<pods=…>]
    │                            │ ◄────── GET    /api/exposure-time/<id>
    │                            │ ◄────── GET    /api/settings
    │                            │ ◄────── PUT    /api/settings
    │                            │ ◄────── POST   /api/fetch                  (exposure)
    │                            │ ◄────── POST   /api/fetch-night            (dayObs)
    │                            │ ◄────── GET    /api/fetch/<id>/status
    │                            │ ◄────── GET    /api/fetch/<id>/progress    (SSE)
    └────────────────────────────┘
                  ▲
                  │ HTML / CSS / JS (vanilla; no build step)
                  │
       static/    │           templates/
       ├─ app.js (bootstrap)   └─ timeline.html (home + explore + night SPA)
       ├─ home.js
       ├─ explore.js  (per-exposure timeline + detail drawer)
       └─ night.js    (dayObs-wide histograms + failure drilldown)

       cli.py         optional "eager mode" — fetch + parse on the CLI before
                      the server starts; populates an exposure ServerState
                      ahead of time. Home mode hands over an empty context.

       exposureTimes.py   dataId → shutter-close (TAI) via the ConsDB SQL
                          endpoint at https://usdf-rsp.slac.stanford.edu/consdb/query.
                          Needs a bearer token from ~/.lsst/log-browser-token.txt
                          (overridable). Persistent on-disk cache at
                          <cache_root>/exposure-times.json so once-resolved
                          dataIds work offline.

       appSettings.py     Server-side settings persisted at
                          <cache_root>/settings.json. Currently just
                          `maxCacheBytes` (the LRU eviction threshold).
```

## Module Responsibilities

| Module             | Responsibility                                                                |
|--------------------|--------------------------------------------------------------------------------|
| `config.py`        | Defaults, `FetchSpec` (frozen dataclass), cache-path helpers, dayObs ↔ UTC conversions, the `NIGHT_AOS_POD_REGEX` constant. |
| `fetch.py`         | `logcli` subprocess wrapper. Lists pods, fetches per-pod JSONL in parallel, manages the on-disk cache (exact / superset reuse), the `.partial` flag, the `_last_viewed.txt` and `_exposure_ids.txt` sidecars, and LRU disk eviction. |
| `parse.py`         | Parses Loki JSONL → `LogLine` → `Event`. Owns the regex taxonomy in [parsing.md](parsing.md). Also captures `TracebackRecord`s with class + capped body, and the carryover-aware dataId attribution per pod group. |
| `night.py`         | dayObs-wide rollups computed off `list[PodSummary]`: top stats, errors-by-type and -by-pod, first-task-start and calcZernikes-end histograms, the failure-row drilldown table. No I/O. |
| `exposureTimes.py` | dataId → shutter-close ISO (TAI) lookup against the RSP ConsDB. Probes `cdb_lsstcam.exposure` first, falls through to LATISS/LSSTComCam/LSSTComCamSim. Reads its bearer token from `~/.lsst/log-browser-token.txt` (overridable via `RA_LOG_EXPLORER_RSP_TOKEN_FILE`). Persists results to `<cache_root>/exposure-times.json` — exposure end-times are immutable so the cache never goes stale. Provides `queryIsotBatch` for night-mode prefetches (one `IN (…)` query per instrument, chunked). |
| `jobs.py`          | `FetchJob` + `JobManager` — the in-process worker pool the browser uses to kick off fetches. One daemon thread per job, an append-only event log per job (guarded by a `threading.Condition`), and the single `stateLock` that guards both keyed-state dicts. `createJob` (exposure) and `createNightJob` (dayObs) put a `kind` discriminator on each job. |
| `appSettings.py`   | Reads / writes `<cache_root>/settings.json`. Schema is open-ended; today the only field is `maxCacheBytes`. Used by the LRU cache eviction in `fetch.evictToFit`. |
| `server.py`        | Stdlib `ThreadingHTTPServer` + JSON / SSE endpoints + static files. Holds a long-lived `ServerContext` containing the `JobManager` and two LRU `OrderedDict`s of loaded states (`exposureStates: {expId → ServerState}`, `nightStates: {dayObs → NightState}`). Multiple tabs / dataIds / dayObses coexist; oldest-by-access gets evicted when `_MAX_LOADED_STATES` (8) is exceeded. |
| `cli.py`           | Argument parsing + the optional "eager fetch" path (exposure mode only). Builds a `ServerContext` and hands it to `server.serve()`. When `--exposure-id`/`--t-zero` are omitted, hands over an empty context and lets the browser drive. Also hosts the `cache info`/`cache flush` subcommands. |
| `static/`          | Single-page vanilla JS UI split for clarity: `app.js` (bootstrap, URL routing, view switching), `home.js` (landing page form, credentials, cache list, progress), `explore.js` (per-exposure timeline + detail drawer), `night.js` (dayObs histograms + failure drilldown). One HTML template (`templates/timeline.html`) holds all three sections; the bootstrap shows whichever matches the URL. No build step. |

## Key Concepts

- **dataId / expId** — 13-digit `YYYYMMDDSSSSS` integer (e.g. `2026051900722`).
  Exposure mode targets a single one of these at a time.

- **dayObs** — 8-digit `YYYYMMDD` integer. The observatory rolls the
  calendar over at UTC-12, so dayObs 20260521 covers
  `2026-05-21T12:00Z → 2026-05-22T12:00Z` (`config.dayObsStartUtc` /
  `dayObsEndUtc`). Night mode targets a dayObs.

- **t-zero** — exposure mode only. The reference time the timeline's
  `0s` line corresponds to. Conventionally the shutter-close time from
  `DimensionRecord.timespan.end`, which is **TAI**. The CLI and the
  POST body subtract 37 s by default; `--t-zero-utc` / `tZeroUtc: true`
  opt out. `cli.py:TAI_MINUS_UTC_S` and `exposureTimes.TAI_MINUS_UTC_S`
  hold the constant.

- **Window** — the (UTC) `[from, to]` time range we query Loki for.
  Exposure-mode default: `(t-zero - 5 s, t-zero + 5 min)`. Night-mode:
  the full 24-hour dayObs window with a `pod=~".*aos.*"` filter applied
  at the Loki layer.

- **PodSummary** — per-pod parsed-log statistics: line/warn/error/traceback
  counts, the set of exposure IDs seen anywhere in the file, the
  classified `Event` list, per-dataId first/last timestamps (carryover-
  aware), per-dataId wait-seconds totals, and a list of `TracebackRecord`s.

- **TracebackRecord** — one Python traceback captured by
  `summarizePod`: pod, timestamp, carryover-attributed dataId,
  exception class, exception message, and a capped body. Used by both
  the per-exposure timeline (TB pill on a pod row) and the night-mode
  drilldown table.

- **Event** — a structured fact extracted from one log line: `kind`
  (string, e.g. `HEAD_INCOMING`, `HEAD_DEFINE_VISIT`, `WORKER_PICKUP`,
  `QUANTUM_DONE`) plus optional `expId`, `detector`, `visit`, `who`,
  `taskLabel`, `durationS`, `flavor`.

- **Pod group** — coarse classification of a pod by name prefix
  (`head`, `sfm`, `aos`, `step1b`, `step1b-aos`, `mosaic`, `psf-plot`,
  `metadata-server-aos`, …). Drives the row grouping in the UI. Owned
  by `parse.POD_GROUPS`. Matching is **longest-prefix-wins** so
  `metadata-server-aos` doesn't collapse into `metadata-server` and
  `step-1b-aos-worker` doesn't collapse into `aos-worker`.

- **Carryover** — for worker pods (sfm, aos, step1b, step1b-aos,
  backlog, plotters, mosaic, one-offs), every line after a dataId is
  first mentioned belongs to that dataId until the next pickup. For
  control-plane pods (head, butler-watcher, metadata-servers, cluster-
  mgr, cleanup, performance-monitor) only lines that explicitly mention
  a dataId get tagged. `parse.carryoverGroups()` is the source of
  truth.

- **`who`** — pipeline tag (`SFM`, `AOS`, `AOS_DANISH`, `ISR`, …) that
  the rapid analysis system uses to distinguish concurrent pipelines on
  the same exposure. Surfaced on head-node events.

## JSON API

### `GET /api/summary`

The view-state lookup. The query string picks which loaded state to
return:

- `?dataId=<int>` — return that exposure's payload, or `{loaded:
  false, cache}` if not loaded.
- `?dayObs=<int>` — return that night's payload, or `{loaded: false,
  cache}` if not loaded.
- no params — home view shape (`{loaded: false, cache}`).

On a hit, the cache's `_last_viewed.txt` sidecar is touched so re-opening
a tab bumps that window up the LRU even without a fresh fetch.

When `loaded: true`, the `mode` field distinguishes the two payload
shapes:

#### Exposure payload  (`mode: "exposure"`)

```jsonc
{
  "loaded": true,
  "mode": "exposure",
  "expId": 2026051900722,
  "tZero": "2026-05-20T08:45:39.267000+00:00",
  "cacheDir": ".../yagan/rapid-analysis/<window-slug>",
  "cacheBytes": 84115620,
  "meta": { ...fetch metadata, including cacheReuse: "exact"|"superset"|"none" },
  "referencePoints": [
    { "label": "shutter close (caller-supplied)", "offsetS": 0.0, ... },
    { "label": "head node first defined visit",   "offsetS": 6.95, ... }
  ],
  "taskColors":  { "isr": "#d4801f", "calibrateImage": "#1a9c8c", ... },
  "groupLabels": { "sfm": "sfm-runner", ... },
  "pods":    [ { "pod": "...", "group": "sfm", "ordinal": 0, "nLines": 541,
                 "nWarn": 39, "nError": 0, "events": [ ... ],
                 "firstRelevantOffsetS": 5.2, "lastRelevantOffsetS": 71.3,
                 "relevantDurationS": 66.1, "qgBuildSeconds": 1.4,
                 "waitSeconds": 0.36, "looksTruncatedEnd": false }, ... ],
  "podsAll": [ { "pod": "...", "group": "...", "nLines": N, ... }, ... ]
}
```

`pods` is the subset whose `expIdsSeen` contains `expId`, plus every
`group == "other"` pod as a safety net for unknown roles. `podsAll` is
every pod that emitted in the window.

`looksTruncatedEnd` is `true` for sfm/aos/step1b/step1b-aos/backlog
pods that touched this expId but did NOT emit a canonical finish event
(QUANTUM_DONE / WORKER_REPORT_* / WORKER_BINNED_*) — usually means the
fetch window ended before the pod did.

#### Night payload  (`mode: "night"`)

```jsonc
{
  "loaded": true,
  "mode": "night",
  "dayObs": 20260521,
  "startTime": "2026-05-21T12:00:00+00:00",
  "endTime":   "2026-05-22T12:00:00+00:00",
  "cacheDir":  ".../yagan/rapid-analysis/<window>/pods=__aos__",
  "cacheBytes": 1234567890,
  "meta":      { ...fetch metadata },
  "stats": {
    "nVisitsSeen": 612, "nPods": 14, "nTracebacks": 7,
    "nDataIdsWithTraceback": 4, "nPodsWithTraceback": 2,
    "nDistinctExceptionClasses": 3, "nMissingShutterClose": 0
  },
  "errorsByType": [ { "excClass": "RuntimeError", "count": 5, "sampleMessage": "..." }, ... ],
  "errorsByPod":  [ { "pod": "...", "group": "aos", "count": 3 }, ... ],
  "histograms": {
    "firstTaskStart":  { "label": "First task pickup (Δshutter)",  "unit": "s",
                         "xMin": ..., "xMax": ..., "binWidth": ...,
                         "counts": [...], "dataIdsByBin": [[...], ...],
                         "nValues": N, "nDropped": M },
    "calcZernikesEnd": { ... same shape ... }
  },
  "failures": [
    { "dataId": 2026052100050, "pod": "...", "group": "aos",
      "excClass": "RuntimeError", "excMessage": "...", "offsetS": 87.2,
      "tIso": "...", "bodyKey": "<pod>@<iso>" }, ...
  ]
}
```

### `GET /api/pod/<podName>?dataId=<int>` or `?dayObs=<int>`

Returns every parsed `LogLine` from that pod's JSONL file. The query
string routes to the right loaded state (`dataId` → exposure, `dayObs`
→ night). 400 if no key, 404 if the targeted state isn't loaded.

```jsonc
{
  "pod": "s-lsstcam-run-head-node-...",
  "lines": [
    { "t": "...", "offsetS": -1.234,    // exposure mode: from tZero
                                         // night mode:    from nightStart (noon UTC)
      "level": "info",
      "logger": "lsst.rubintv.production.processControl.HeadProcessController",
      "function": "doDetectorFanout",
      "message": "Fanning ...",
      "raw":     "<full original line>",
      "expId":   2026051900722           // carryover-attributed for worker pods
    }, ...
  ]
}
```

`podName` is checked against a `[A-Za-z0-9._-]+` allowlist so it
can't break out of `pods/`.

### `GET /api/night/traceback/<bodyKey>?dayObs=<int>`

Drilldown for a single failure row. Returns the pod's log lines
spanning the dataId's full processing block when the traceback's expId
is carryover-attributable, or a ±N-second window around the traceback
itself otherwise. `contextSource` is `"dataId-block"` or
`"time-window"` accordingly.

```jsonc
{
  "bodyKey": "<pod>@<iso>",
  "pod": "s-lsstcam-run-aos-worker-…", "group": "aos",
  "expId": 2026052100050,
  "excClass": "RuntimeError", "excMessage": "…",
  "firstTs": "...", "lastTs": "...", "tracebackTs": "...",
  "contextSource": "dataId-block",
  "lines":    [ { "t": "...", "level": "...", "raw": "..." }, ... ],
  "truncated": false,
  "body":     "<the captured traceback body, as a fallback>"
}
```

Lines are capped at ~4 000 / ~600 kB so a pathological run can't
generate a multi-megabyte drilldown response. The cap shows up as
`truncated: true`.

### `GET /api/exposure-time/<dataId>`

dataId → shutter-close ISOT (TAI) lookup, used by the home form to
resolve a user-typed dataId before kicking off the fetch.

- 200 with `{"dataId", "tZero", "scale": "TAI", "fromCache": bool}`.
- 404 `"No exposure-time record for dataId=N"` — every instrument
  table searched, no row anywhere.
- 502 `"ConsDB query failed: ..."` — typed ConsDB error (5xx, etc.).
- 503 `"RSP token file not found at <path>. Set the path in the home
  page Credentials card or via the RA_LOG_EXPLORER_RSP_TOKEN_FILE env
  var."` — token missing.
- 503 `"RSP token file is empty: <path>"` — token file present but
  blank.

The on-disk cache at `<cache_root>/exposure-times.json` is checked
first; a cache hit returns immediately with `fromCache: true` and no
network call. The home page also accepts a `?tokenFile=` query param
that overrides the env-var/default lookup for one request — used by
the Credentials card so users can pick a token without restarting.

### `GET /api/cache`

```jsonc
{
  "root":    { "path": ".../ra_log_explorer", "totalBytes": 42330276 },
  "windows": [
    {
      "cluster": "yagan", "namespace": "rapid-analysis",
      "windowDir": "2026-05-20T084534_267000Z__2026-05-20T085039_267000Z",
      "relPath":   "2026-05-20T...__2026-05-20T..."  // or "<window>/pods=__aos__"
                                                     // for night caches
      "podFilter": null,                             // or ".*aos.*" for night
      "kind":      "exposure",                       // or "night"
      "dayObs":    null,                             // night caches recover dayObs
                                                     // from the window start
      "exposureIds": [2026051900722, 2026051900723], // dataIds that triggered
                                                     // fetches landing here
      "fromIso":      "...", "toIso":       "...",
      "fetchedAt":    "...", "lastViewedAt": "...",
      "podCount": 432, "totalBytes": 42289444, "sizeOnDisk": 42330276
    }, ...
  ]
}
```

Sorted most-recently-fetched-first. Skips directories with a `.partial`
flag or a missing/corrupt `_meta.json`. Night caches nest one level
deeper than exposure caches (`<window>/pods=<slug>`) so a same-window
exposure-mode and night-mode cache can coexist; both flavours are
listed independently here.

### `DELETE /api/cache`

Wipe the entire cache root. Always clears every loaded state (since
they all reference the now-gone cache). Returns the same shape as
`GET /api/cache`. The root itself is recreated empty so subsequent
fetches still work.

### `DELETE /api/cache/<cluster>/<namespace>/<slug>[/<pods=…>]`

Remove one cached window. The optional 4th segment targets the
nested night-mode `pods=<regex-slug>` subdir; it must start with
`pods=`. Each component is validated against `[A-Za-z0-9._=-]+` so
the URL can't escape `cache_root()`. If any loaded state's `cacheDir`
matches the directory being deleted, that state is evicted first
(the UI gets booted back to the home view on next summary fetch).
Empty per-cluster / per-namespace parent directories are pruned.
Returns the same shape as `GET /api/cache`. 404 on a path mismatch.

### `GET /api/settings` / `PUT /api/settings`

Server-side settings persisted under `<cache_root>/settings.json`.
Today there's exactly one knob — the LRU cache size cap:

```jsonc
{ "maxCacheBytes": 5368709120 }   // 5 GiB by default
```

`PUT` validates `maxCacheBytes` is a non-negative integer; rejects
bad JSON, missing field, non-int, or negative values with 400.

### `POST /api/fetch`  (exposure)

Request body (every field except `exposureId`/`tZero` falls back to
the CLI defaults; the password is consumed by the fetch worker thread
to set `LOKI_PASSWORD` in its process env, and is never echoed back
or persisted):

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

Response: `202 Accepted`, `{"jobId": "<12-char hex>"}`. Validation
errors return `400` with `{"error": "..."}`.

### `POST /api/fetch-night`  (dayObs)

```jsonc
{
  "dayObs":   20260521,                // required, YYYYMMDD integer
  "username": "merlin", "password": "...",
  "cluster":  "yagan", "namespace": "rapid-analysis",
  "lokiAddr": "https://loki-query.ls.lsst.org",
  "workers":  8
}
```

The window is the full 24-hour dayObs (noon UTC → noon UTC) with the
`pod=~".*aos.*"` filter applied at the Loki layer. Same response
shape as `/api/fetch`.

### `GET /api/fetch/<jobId>/status`

JSON snapshot of one job:

```jsonc
{
  "jobId": "8970db79c0a6",
  "status": "running" | "parsing" | "done" | "error",
  "kind":   "exposure" | "night",
  "expId":  2026051900722,             // null for night jobs
  "tZero":  "...",                     // null for night jobs
  "dayObs": null,                      // 20260521 for night jobs
  "fromIso": "...", "toIso": "...",
  "startedAt": "...", "finishedAt": "...",
  "cacheDir":  "...",
  "cacheReuse": "exact" | "superset" | "none" | null,
  "error":   null | "...",
  "eventCount": 5
}
```

### `GET /api/fetch/<jobId>/progress`  (Server-Sent Events)

`text/event-stream` of one job's progress events. Each `data:` line
is one JSON event (`{"type": ...}`):

- `start`: `{ fromIso, toIso }` — once at job kick-off.
- `pod-done`: `{ pod, i, total }` — once per fetched pod (i ascends
  from 1; reaches `total` on the last).
- `parsing`: `{ cacheReuse, podCount, totalBytes }` — after the
  fetch finishes, before `summarizeAll` runs.
- `shutter-close`: `{ phase, ... }` — night-mode only. Phases:
    - `starting`: `{ total }` (number of dataIds we'll try to
       resolve)
    - `cache-checked`: `{ cacheHits, remaining }` — after the
       on-disk shutter-close cache pass.
    - `no-token`: `{ remaining, tokenPath }` — token file missing;
       remaining dataIds can't be resolved.
    - `empty-token`: `{ remaining }` — token file blank.
    - `consdb-error`: `{ error }` — typed ConsDB error.
    - `done`: `{ consdbHits, stillMissing }` — happy path.
- `done`: `{ kind, expId, tZero, dayObs, cacheDir, cacheReuse,
  podCount, totalBytes, elapsedS }` — after the server's keyed
  `ServerState`/`NightState` slot has been populated. **Always**
  fired after `onComplete` so SSE consumers can rely on the
  summary being ready when they see `done`.
- `error`: `{ error }` — terminal; the job failed.

History is replayable: the SSE handler emits every event already in
the job's log on connection, then waits for new ones. Multiple
concurrent readers are fine — each iterates the log independently,
including reattaching *after* the job has already finished.

Keepalive comments (`: keepalive\n\n`) are emitted every 15 s so
intermediate proxies don't time the stream out.

## Threading model

`server.serve()` runs a `ThreadingHTTPServer`; one thread per request.

The shared `ServerContext` is mutated only while holding
`ctx.jobs.stateLock`, which guards:

- inserting / evicting entries in `ctx.exposureStates` and
  `ctx.nightStates` (worker thread → ✓ insert; DELETE handlers →
  ✓ evict);
- reading those dicts to render `/api/summary` or `/api/pod/<>`
  (request threads → snapshot read).

Each fetch runs on its own daemon thread spawned by
`JobManager.startJob`. SSE handlers block on
`FetchJob.condition.wait()` to be notified when new events arrive.

### Multi-tab support

Both keyed-state dicts are LRU-ordered (`OrderedDict.move_to_end`
on every read) and capped at `_MAX_LOADED_STATES = 8`. A user with
several open tabs (different dataIds / dayObses) sees each one keep
its state until 9+ tabs are in play; the least-recently-opened gets
evicted then. The browser's URL carries the routing key so reload /
back-button on an evicted tab triggers a fresh `/api/summary` fetch
against the cache.

Pod detail and traceback drilldown responses re-read the JSONL
files from disk on each request rather than buffering them in
memory — the cache for one exposure is typically ~40 MiB so this
stays cheap.

## Three startup modes

1. **Home mode** — `python3 -m ra_log_explorer.cli` with no
   `--exposure-id`/`--t-zero`. CLI just spins up a fresh `JobManager`
   and an empty `ServerContext`, hands it to `server.serve()`, and
   the user picks an exposure (or a dayObs for night mode) in the
   browser. Every fetch from then on goes through `POST /api/fetch`
   or `POST /api/fetch-night` and the SSE progress endpoint.

2. **Eager fetch mode** — `--exposure-id` + `--t-zero` supplied. CLI
   runs the same TAI-adjustment + `fetch.fetchAll` + `parse.summarizeAll`
   pipeline that the worker thread runs in home mode, but on the
   main thread before the server starts. The populated `ServerContext`
   is handed to `serve()`, so the browser lands directly on the
   explore view. Exposure-only; night mode is browser-driven. Useful
   for scripting.

3. **No-serve mode** — `--no-serve` (paired with `--exposure-id`/
   `--t-zero`). Performs the eager fetch + parse, then exits without
   starting the HTTP server. Useful for batch-priming the cache.

All three share `server.py`, `jobs.py`, the JSON API surface, and
the SPA.

## Non-goals

- Streaming / live tailing of logs. Snapshot-based; one window per
  fetch.
- Cross-night aggregation or trending. One dayObs at a time in
  night mode; one exposure at a time in exposure mode.
- Authentication. Listens on `127.0.0.1` only. The single-user
  assumption is baked in (e.g. `LOKI_PASSWORD` is set process-wide
  by a fetch request).
- A persistent service. Process exits on Ctrl-C; the cache persists
  on disk and survives restarts.
