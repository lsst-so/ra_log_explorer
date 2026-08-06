# ra_log_explorer — Architecture & Data Flow

A standalone tool for reconstructing what happened in the rapid analysis
distributed pipeline. Given **a dataId + t-zero** (single exposure),
**a dayObs** (night-wide AOS survey), or **a start/stop dataId pair**
(a contiguous range of exposures), it pulls every pod's logs from Loki
for the relevant window, parses them into structured events, and serves
an interactive browser timeline.

The three modes differ only in how the Loki window is chosen and how the
results are presented. Range mode fetches one wide all-pods window
spanning `shutterClose(start) - before → shutterClose(stop) + after` as a
**single cache block** (so the heavily-overlapping per-exposure windows
aren't downloaded N times), resolves each in-range dataId's shutter close
from ConsDB, and reuses the per-exposure timeline view with a navigator
to step between exposures — each anchored at its own shutter close.

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
    └─────────────┬──────────────┘  app logs + k8s/events lifecycle stream
                  │ pods/<pod>.jsonl  +  pods_events/<pod>.jsonl
                  ▼
    ┌────────────────────────────┐
    │   parse.py                 │  Loki JSONL → LogLine → Event (app log);
    │   (summarizeAll)           │  k8s/events → POD_* lifecycle Event;
    │                            │  carryover dataId attribution; tracebacks
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
                  │ ServerState / NightState / RangeState (via stateLock)
                  ▼
    ┌────────────────────────────┐         GET    /                          (home/explore/night SPA)
    │   server.py                │ ◄────── GET    /static/*
    │   (stdlib HTTP + SSE)      │ ◄────── GET    /api/summary?dataId=…
    │                            │ ◄────── GET    /api/summary?dayObs=…
    │                            │ ◄────── GET    /api/summary?rangeStart=&rangeStop=[&dataId=]
    │                            │ ◄────── GET    /api/pod/<pod>?dataId|dayObs=…
    │                            │ ◄────── GET    /api/pod/<pod>?rangeStart=&rangeStop=&dataId=
    │                            │ ◄────── GET    /api/night/traceback/<key>?dayObs=…
    │                            │ ◄────── GET    /api/cache                  (lists windows)
    │                            │ ◄────── DELETE /api/cache                  (wipe all)
    │                            │ ◄────── DELETE /api/cache/.../<slug>[/<pods=…>]
    │                            │ ◄────── GET    /api/exposure-time/<id>
    │                            │ ◄────── GET    /api/settings
    │                            │ ◄────── PUT    /api/settings
    │                            │ ◄────── POST   /api/fetch                  (exposure)
    │                            │ ◄────── POST   /api/fetch-night            (dayObs)
    │                            │ ◄────── POST   /api/fetch-range            (start/stop)
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
       ├─ night.js    (dayObs-wide histograms + failure drilldown)
       └─ range.js    (range navigator strip; drives the explore view
                       for the selected dataId in the range)

       cli.py         optional "eager mode" — fetch + parse on the CLI before
                      the server starts; populates an exposure ServerState
                      ahead of time. Home mode hands over an empty context.

       exposureTimes.py   dataId → curated ConsDB *exposure record* via
                          a SQL endpoint (SELECT * projected to a useful
                          column subset: obs_end + filter, exp time, image
                          type, program, reason, group/index, pointing,
                          seeing). obs_end is the shutter-close (TAI)
                          t-zero; the rest drive the explore-view info box
                          and the dataId-link tooltips. The endpoint and
                          bearer-token file are looked up per-site from
                          sites.toml (see below), so the same dataId can
                          resolve to a different record depending on the
                          active site. Persistent per-site on-disk cache
                          at <cache_root>/exposure-times/<site>.json so
                          once-resolved dataIds work offline.

       sites.py           Per-deployment site catalog. A *site* pairs a
                          Loki cluster with the ConsDB endpoint that
                          owns its shutter-close truth. yagan→summit
                          (USDF/summit RSP) and manke→bts (base-lsp);
                          the table lives in the checked-in
                          ra_log_explorer/sites.toml. The home page
                          shows a top-bar switcher; every fetch and
                          shutter-close lookup carries a `site` field
                          that the server resolves via the catalog.

       appSettings.py     Server-side settings persisted at
                          <cache_root>/settings.json. Currently just
                          `maxCacheBytes` (the LRU eviction threshold).
```

## Module Responsibilities

| Module             | Responsibility                                                                |
|--------------------|--------------------------------------------------------------------------------|
| `config.py`        | Defaults, `FetchSpec` (frozen dataclass), cache-path helpers, dayObs ↔ UTC conversions, the `NIGHT_AOS_POD_REGEX` constant. |
| `fetch.py`         | `logcli` subprocess wrapper. Lists pods, fetches each pod's JSONL in parallel as count-presized single-batch chunks (works around grafana/loki#17270; see [caching.md](caching.md)), plus a cheap second pass for each pod's `k8s/events` lifecycle stream into `pods_events/`. Manages the on-disk cache (exact / superset reuse), the schema-version flush, the `.partial` flag, the `_last_viewed.txt` and `_exposure_ids.txt` sidecars, and LRU disk eviction. |
| `parse.py`         | Parses Loki JSONL → `LogLine` → `Event`. Owns the regex taxonomy in [parsing.md](parsing.md). Also parses the `k8s/events` stream into `POD_*` lifecycle Events (`classifyK8sEvent`), captures `TracebackRecord`s with class + capped body, and the carryover-aware dataId attribution per pod group. |
| `night.py`         | dayObs-wide rollups computed off `list[PodSummary]`: top stats, errors-by-type and -by-pod, first-task-start and calcZernikes-end histograms, the failure-row drilldown table, and the gather-only completeness check (dataIds with step1b activity but no step1a — impossible, so a dropped-logs tell). No I/O. |
| `exposureTimes.py` | dataId → curated ConsDB *exposure record* (`{obs_end, exp_time, physical_filter, img_type, science_program, observation_reason, group_id, cur_index/max_index, …}`, the `EXPOSURE_RECORD_COLUMNS` projection of a `SELECT *`). `obs_end` is the shutter-close (TAI) t-zero; `obsEnd(record)` pulls it out. Every public helper takes the ConsDB URL and resolved bearer token from the caller, so the same dataId can be queried against multiple sites without crosstalk. Probes `cdb_lsstcam.exposure` first, falls through to LATISS/LSSTComCam/LSSTComCamSim. Persists records per-site to `<cache_root>/exposure-times/<siteName>.json` (a legacy obs_end-only string entry still reads back as a 1-field record) — exposure properties are immutable so the cache never goes stale. Provides `queryExposureRecordBatch` for night/range prefetches (one `IN (…)` query per instrument, chunked). |
| `sites.py`         | The site catalog (`sites.toml`). Loads at server start into `ServerContext.sites`. Each `Site` carries (`name`, `cluster`, `namespace`, `lokiAddr`, `consdbUrl`, `consdbTokenFile`). `siteByName` / `siteByCluster` are the lookups; the latter is how cache-rehydration paths figure out which site a window belongs to from its on-disk cluster component. |
| `jobs.py`          | `FetchJob` + `JobManager` — the in-process worker pool the browser uses to kick off fetches. One daemon thread per job, an append-only event log per job (guarded by a `threading.Condition`), and the single `stateLock` that guards the keyed-state dicts. `createJob` (exposure), `createNightJob` (dayObs), and `createRangeJob` (start/stop pair) put a `kind` discriminator on each job. |
| `appSettings.py`   | Reads / writes `<cache_root>/settings.json`. Schema is open-ended; today the only field is `maxCacheBytes`. Used by the LRU cache eviction in `fetch.evictToFit`. |
| `server.py`        | Stdlib `ThreadingHTTPServer` + JSON / SSE endpoints + static files. Holds a long-lived `ServerContext` containing the `JobManager` and three LRU `OrderedDict`s of loaded states (`exposureStates: {expId → ServerState}`, `nightStates: {dayObs → NightState}`, `rangeStates: {"start-stop" → RangeState}`). Multiple tabs / dataIds / dayObses / ranges coexist; oldest-by-access gets evicted when `_MAX_LOADED_STATES` (8) is exceeded. |
| `cli.py`           | Argument parsing + the optional "eager fetch" path (exposure mode only). Builds a `ServerContext` and hands it to `server.serve()`. When `--exposure-id`/`--t-zero` are omitted, hands over an empty context and lets the browser drive. Also hosts the `cache info`/`cache flush` subcommands. |
| `static/`          | Single-page vanilla JS UI split for clarity: `app.js` (bootstrap, URL routing, view switching), `home.js` (landing page forms, credentials, cache list, progress), `explore.js` (per-exposure timeline + detail drawer), `night.js` (dayObs histograms + failure drilldown), `range.js` (range navigator strip that drives the explore view per selected dataId). One HTML template (`templates/timeline.html`) holds the home/explore/night sections; the bootstrap shows whichever matches the URL. No build step. |

## Key Concepts

- **Site** — a (Loki cluster, ConsDB endpoint, bearer-token file)
  bundle that pairs the *log source* with the *truth source* for
  shutter-close times. Catalogued in the checked-in
  [`ra_log_explorer/sites.toml`](../ra_log_explorer/sites.toml);
  loaded once at startup into `ServerContext.sites`. Today there are
  two: **summit** (cluster `yagan`, ConsDB at
  `usdf-rsp.slac.stanford.edu`, token `~/.lsst/log-browser-token.txt`)
  and **bts** (cluster `manke`, ConsDB at `base-lsp.lsst.codes`, token
  `~/.lsst/manke-token.txt`).

  Sites matter because the same bare dataId can refer to a real-camera
  exposure on the summit and a *different*, simulated exposure on BTS
  — different `obs_end` values, separate sources of truth. Every
  shutter-close lookup and every exposure-time cache file is scoped by
  site to keep them from crosstalking. The home page exposes the
  current site as a top-bar switcher; every fetch + exposure-time
  request body carries a `site` field that the server resolves via the
  catalog (falls back to `default_site` if omitted). USDF will get its
  own site once we plumb that path; it'll share the summit ConsDB.

- **dataId / expId** — 13-digit `YYYYMMDDSSSSS` integer (e.g. `2026051900722`).
  Exposure mode targets a single one of these at a time.

- **dayObs** — 8-digit `YYYYMMDD` integer. The observatory rolls the
  calendar over at UTC-12, so dayObs 20260521 covers
  `2026-05-21T12:00Z → 2026-05-22T12:00Z` (`config.dayObsStartUtc` /
  `dayObsEndUtc`). Night mode targets a dayObs.

- **Range** — a contiguous `[startId, stopId]` span of dataIds, keyed
  in memory and in the URL by `"<startId>-<stopId>"`
  (`/?rangeStart=…&rangeStop=…`). Range mode fetches **one** wide
  all-pods window for the whole span and resolves every in-range
  dataId's shutter close from ConsDB. The integer span is the candidate
  set; ConsDB is the source of truth for which are real exposures (the
  rest are "skipped" integers, expected and simply omitted). Capped at
  `config.MAX_RANGE_SPAN` (500) as a fat-finger backstop. The per-dataId
  timeline is the ordinary exposure view, computed on demand from the
  shared parsed summaries with that dataId's own shutter close as t-zero.

- **t-zero** — exposure mode only. The reference time the timeline's
  `0s` line corresponds to. Conventionally the shutter-close time from
  `DimensionRecord.timespan.end`, which is **TAI**. The CLI and the
  POST body subtract 37 s by default; `--t-zero-utc` / `tZeroUtc: true`
  opt out. The canonical constant lives in
  `exposureTimes.TAI_MINUS_UTC_S`; `cli.TAI_MINUS_UTC_S` and the
  server's `_buildSpecFromRequest` both reference it, so there's
  exactly one source of truth.

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
  `taskLabel`, `durationS`, `flavor`. Events also come from the
  `k8s/events` stream as pod-lifecycle facts (`kind` of `POD_RESTARTED`,
  `POD_KILLED`, `POD_OOMKILLED`, `POD_FAILED`, `POD_UNHEALTHY`,
  `POD_STARTED`) — these carry no dataId and put the k8s `reason` in
  `flavor`. See [parsing.md](parsing.md) for the full taxonomy.

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
- `?rangeStart=<int>&rangeStop=<int>` — return that range's **index**
  payload (`mode: "range"`), or `{loaded: false, cache}` if not loaded.
- `?rangeStart=<int>&rangeStop=<int>&dataId=<int>` — return one
  in-range exposure's timeline (`mode: "range-exposure"`, the same shape
  as the exposure payload plus a `podDetailQuery`). 404 if the dataId
  has no resolved shutter close (a skipped integer).
- no params — home view shape (`{loaded: false, cache}`).

If the requested key isn't in the in-memory state dict, the server
makes one attempt to reconstruct it from disk: it walks the cache root
for a matching window (an exposure-mode cache whose
`_exposure_ids.txt` lists the dataId; a night-mode cache whose
window starts at noon UTC of the dayObs; or a range cache whose
`_range.txt` records the `[startId, stopId]` bounds), reparses it with
`parser.summarizeAll`, and returns the rebuilt payload. This lets a
deep-linked tab (e.g. opening the dataId column in the home page's
cache table) land directly on its explore/night view without an
intervening home → fetch click — the cache is the source of truth, so
no re-fetch is needed. For exposure caches the shutter close must
already be in the local `exposure-times.json` (it always is whenever
the cache itself exists, because the fetch path stores both); if it
isn't, the server falls back to `{loaded: false}` and the home view
takes over. Night-mode rebuilds skip the ConsDB fallback (there's no
SSE channel to report progress on a sync request) — any dataId not
already locally cached stays absent in the histograms until a real
fetch fills it in.

On a hit, the cache's `_last_viewed.txt` sidecar is touched so re-opening
a tab bumps that window up the LRU even without a fresh fetch.

When `loaded: true`, the `mode` field distinguishes the two payload
shapes:

#### Exposure payload  (`mode: "exposure"`)

```jsonc
{
  "loaded": true,
  "mode": "exposure",
  "site": "summit",
  "expId": 2026051900722,
  "tZero": "2026-05-20T08:45:39.267000+00:00",
  "exposure": {                                  // curated ConsDB record, or null
    "obs_end": "2026-05-31T04:16:18.199000", "exp_time": 30.0,
    "physical_filter": "z_20", "img_type": "science",
    "science_program": "BLOCK-407", "observation_reason": "template_blob_z_33.0",
    "cur_index": 1, "max_index": 1, ...          // drives the explore-view info box
  },
  "cacheDir": ".../yagan/rapid-analysis/<window-slug>",
  "cacheBytes": 84115620,
  "meta": { ...fetch metadata, including cacheReuse: "exact"|"superset"|"none" },
  "referencePoints": [
    { "label": "shutter close (caller-supplied)", "offsetS": 0.0, ... },  // or "(manual)" if hand-entered
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

`meta` is the fetch's `_meta.json` verbatim. Beyond `cacheReuse`, the
fields the UI cares about are `fetchComplete` (bool) and the two
fall-short maps it summarises: `errors` (`{pod: message}` — a hard
logcli failure) and `incomplete_pods` (`{pod: reason}` — a pod whose
chunks couldn't be verified lossless, i.e. data grafana/loki#17270 may
have dropped). `fetchComplete` is `false` iff either map is non-empty;
the explore and night views then render a loud banner (`renderFetchBanner`,
which merges both maps), since an incomplete night fetch otherwise
silently biases the Δshutter histograms. `meta` also carries `pod_bytes`,
`pod_lines`, and `pod_expected` (the `count_over_time` oracle per pod) for
sanity-checking. `fetchSchemaVersion` gates cache reuse (see
[caching.md](caching.md)). This same `meta` block appears in the night
and range payloads below.

`looksTruncatedEnd` is `true` for sfm/aos/step1b/step1b-aos/backlog
pods that touched this expId but did NOT emit a canonical finish event
(QUANTUM_DONE / WORKER_REPORT_* / WORKER_BINNED_*) — usually means the
fetch window ended before the pod did.

Each pod's `events` array also carries any **pod-lifecycle markers**
(`kind` of `POD_RESTARTED` / `POD_KILLED` / `POD_OOMKILLED` / `POD_FAILED`
/ `POD_UNHEALTHY` / `POD_STARTED`, with `expId: null` and the k8s `reason`
in `flavor`). Unlike dataId-keyed events, these are kept whenever they fall
in the broad exposure window (`tZero - 5 s … tZero + 5 min`), not the tight
per-dataId window — a pod usually dies a few seconds *after* its last work
line, so the loose window is what keeps "the pod died here" visible.
`looksTruncatedEnd` and a `POD_RESTARTED` marker are complementary: the
former says "no finish event", the latter says *why*.

#### Night payload  (`mode: "night"`)

```jsonc
{
  "loaded": true,
  "mode": "night",
  "site": "summit",
  "dayObs": 20260521,
  "startTime": "2026-05-21T12:00:00+00:00",
  "endTime":   "2026-05-22T12:00:00+00:00",
  "cacheDir":  ".../yagan/rapid-analysis/<window>/pods=__aos__",
  "cacheBytes": 1234567890,
  "meta":      { ...fetch metadata },
  "stats": {
    "nVisitsSeen": 612, "nPods": 14, "nTracebacks": 7,
    "nDataIdsWithTraceback": 4, "nPodsWithTraceback": 2,
    "nDistinctExceptionClasses": 3, "nMissingShutterClose": 0,
    "nPodRestarts": 2
  },
  "errorsByType": [ { "excClass": "RuntimeError", "count": 5, "sampleMessage": "..." }, ... ],
  "errorsByPod":  [ { "pod": "...", "group": "aos", "count": 3 }, ... ],
  "restarts": [                                  // POD_* lifecycle events (k8s/events)
    { "kind": "POD_RESTARTED", "reason": "Started", "pod": "...", "group": "aos",
      "dataId": 2026060400222,                   // the dataId the pod was processing then
      "offsetS": 99.0, "tIso": "...",            // offsetS = Δshutter of that dataId, or null
      "message": "Started container run-aos-worker (restart #2)  ·  on yagan01" }, ...
  ],
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
  ],
  "gatherOnly": [ 2026052100200, ... ],          // dataIds with gather (step1b)
                                                 // activity but no step1a — physically
                                                 // impossible, so a tell the fetch dropped
                                                 // step1a logs (surfaced as a warning banner)
  "exposureInfo": {                              // dataId (string) -> curated ConsDB record
    "2026052100050": { "img_type": "science", "physical_filter": "z_20",
                       "observation_reason": "...", ... }, ...
  }                                              // drives the dataId-link tooltips
}
```

#### Range index payload  (`mode: "range"`)

The lightweight navigator index — one entry per resolved dataId, no
pod/event arrays. The per-dataId timelines are fetched on demand (see
below).

```jsonc
{
  "loaded": true,
  "mode": "range",
  "site": "summit",
  "startId": 2026051900722,
  "stopId":  2026051900750,
  "fromTime": "2026-05-20T08:45:34+00:00",   // fetch window (UTC)
  "toTime":   "2026-05-20T08:51:39+00:00",
  "cacheDir": ".../yagan/rapid-analysis/<window>",
  "cacheBytes": 84115620,
  "meta":     { ...fetch metadata },
  "nMissing": 3,                              // ids in [start,stop] ConsDB had no row for
  "dataIds": [
    { "expId": 2026051900722, "tZero": "<utc iso>",
      "nPods": 12, "nTraceback": 0, "hasLogs": true,
      "exposure": { "img_type": "science", ... } },  // curated record (or null) for the chip tooltip
    ...
  ]
}
```

#### Range per-exposure payload  (`mode: "range-exposure"`)

Returned by `?rangeStart=&rangeStop=&dataId=`. Byte-for-byte the
exposure payload shape (built by reusing `_buildSummaryPayload` against
the range's shared summaries with the dataId's own shutter close as
`tZero`, so it carries that dataId's `exposure` record too), plus:

```jsonc
{
  "mode": "range-exposure",
  "startId": 2026051900722, "stopId": 2026051900750,
  "podDetailQuery": "rangeStart=2026051900722&rangeStop=2026051900750&dataId=2026051900725",
  // ...all the exposure-payload fields (pods, podsAll, taskColors, ...)
}
```

`podDetailQuery` is what the explore renderer appends to `/api/pod/<pod>`
so pod-detail lookups route back through the range state (and anchor
their offsets at this dataId's shutter close).

### `GET /api/pod/<podName>?dataId=<int>` / `?dayObs=<int>` / `?rangeStart=&rangeStop=&dataId=`

Returns every parsed `LogLine` from that pod's JSONL file. The query
string routes to the right loaded state (`dataId` → exposure, `dayObs`
→ night, `rangeStart`+`rangeStop`+`dataId` → that dataId within a loaded
range, with offsets anchored at its shutter close). 400 if no key, 404
if the targeted state isn't loaded.

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

### `GET /api/exposure-time/<dataId>?site=<name>`

dataId → curated ConsDB exposure record, used by the home form to
resolve a user-typed dataId (and show its properties) before kicking
off the fetch. The optional `site` query param picks which entry from
the sites catalog to use; omitted = the catalog's `default_site`.

- 200 with `{"dataId", "tZero", "scale": "TAI", "fromCache": bool,
  "manual": bool, "site", "exposure": {<curated record>}}`. `tZero` is
  the record's `obs_end`; `exposure` carries the rest (filter, exp time,
  image type, program, reason, group/index, …) for the explore-view info
  box. `manual: true` flags a hand-entered stand-in (see below) rather
  than a ConsDB value.
- 400 `"No site named '<x>'; known: [...]"` — unknown site.
- 404 `"No exposure-time record for dataId=N"` — every instrument
  table searched, no row with an `obs_end` anywhere.
- 502 `"ConsDB query failed: ..."` — typed ConsDB error (5xx, etc.).
- 503 `"ConsDB token file for site '<name>' not found at <path>. Get a
  token from the relevant RSP and drop it there."` — token missing.
- 503 `"ConsDB token file is empty: <path>"` — token file present but
  blank.

The per-site on-disk cache at `<cache_root>/exposure-times/<site>.json`
is checked first; a *ConsDB-sourced* cache hit returns immediately with
`fromCache: true` and no network call. Sites have separate cache files
so a colliding bare dataId between scopes (BTS simulated vs. summit real)
can't return the wrong record.

**Manual stand-ins.** When ConsDB is down or has no row for a dataId, the
home form lets the user type a shutter close by hand (`POST /api/fetch`
with `tZeroManual: true`, below), which persists a `_manual`-tagged
record to the same per-site cache. Because a manual value is a stand-in —
not the immutable truth a ConsDB row is — this endpoint treats it as a
*fallback*, not an authoritative cache hit: a `_manual` record does **not**
short-circuit the lookup; ConsDB is still queried, and the manual value is
only returned (with `manual: true`, and one of the error statuses' would-be
message suppressed) when ConsDB still can't resolve the dataId. A later
real ConsDB hit overwrites the stand-in.

**This policy is global, not per-endpoint.** The night and range prefetch
path (`_resolveShutterClosesInto`, which resolves hundreds of ids in one
batch) applies the same rule: a `_manual` record anchors its id
immediately *and* joins the ConsDB batch, so a real row supersedes it as
soon as ConsDB can answer. Were it treated as an ordinary cache hit, one
manual entry made during an outage would anchor that dataId in every
future night/range view forever, silently biasing every Δshutter offset
and histogram computed from it. The path's `shutter-close` progress
events carry `manualStandins` (how many hits were provisional) alongside
`cacheHits`, and `remaining` / `stillMissing` count only ids with no t₀
at all — a re-queried stand-in is anchored, not missing.

### `GET /api/sites`

Return the per-deployment site catalog plus the default site name.
The token-file paths are *not* echoed — they're a server-side detail.

```jsonc
{
  "default_site": "summit",
  "sites": [
    { "name": "summit", "cluster": "yagan", "namespace": "rapid-analysis",
      "lokiAddr": "https://loki-query.ls.lsst.org",
      "consdbUrl": "https://usdf-rsp.slac.stanford.edu/consdb/query" },
    { "name": "bts",    "cluster": "manke", "namespace": "rapid-analysis",
      "lokiAddr": "https://loki-query.ls.lsst.org",
      "consdbUrl": "https://base-lsp.lsst.codes/consdb/query" }
  ]
}
```

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
      "kind":      "exposure",                       // or "night" / "range"
      "dayObs":    null,                             // night caches recover dayObs
                                                     // from the window start
      "rangeStart": null, "rangeStop": null,         // range caches: the [start, stop]
                                                     // bounds from _range.txt
      "exposureIds": [2026051900722, 2026051900723], // (exposure caches) dataIds that
                                                     // triggered fetches landing here
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

Request body (`site` falls through to the catalog's `default_site`;
the password is consumed by the fetch worker thread to set
`LOKI_PASSWORD` in its process env, and is never echoed back or
persisted):

```jsonc
{
  "exposureId": 2026051900722,         // required, integer
  "tZero":      "2026-05-20T08:46:16.267",  // required, ISO-8601
  "tZeroUtc":   false,                 // optional; default false (treat as TAI)
  "tZeroManual": false,                // optional; true ⇒ tZero was hand-entered
  "site":       "summit",              // optional; falls back to default_site
  "username":   "merlin", "password": "...",
  "workers":    8,
  "windowBefore": 5.0, "windowAfter": 300.0
}
```

`cluster` / `namespace` / `lokiAddr` are *derived* server-side from
the named site — clients no longer send them. An unknown site returns
`400 Bad Request`.

`tZeroManual: true` marks a shutter close the user typed by hand because
ConsDB couldn't resolve the dataId. The server persists it as a
`_manual`-tagged record in the per-site exposure-time cache (in TAI form,
the inverse of the `obs_end → tZero` conversion), so the resulting
explore view can be reopened or refreshed without re-typing. The
exposure's reference point is then labelled `shutter close (manual)`
instead of `shutter close (caller-supplied)`. See
`GET /api/exposure-time` for how the stand-in is treated on later lookups.

Response: `202 Accepted`, `{"jobId": "<12-char hex>"}`. Validation
errors return `400` with `{"error": "..."}`.

### `POST /api/fetch-night`  (dayObs)

```jsonc
{
  "dayObs":   20260521,                // required, YYYYMMDD integer
  "site":     "summit",                // optional; falls back to default_site
  "username": "merlin", "password": "...",
  "workers":  8
}
```

The window is the full 24-hour dayObs (noon UTC → noon UTC) with the
`pod=~".*aos.*"` filter applied at the Loki layer. Same response
shape as `/api/fetch`.

### `POST /api/fetch-range`  (start/stop)

```jsonc
{
  "rangeStart": 2026051900722,         // required, integer
  "rangeStop":  2026051900750,         // required, integer; > rangeStart
  "tZeroStart": "2026-05-20T08:46:16.267",  // required, ISO-8601 (start shutter close)
  "tZeroStop":  "2026-05-20T08:51:09.512",  // required, ISO-8601 (stop shutter close)
  "tZeroUtc":   false,                 // optional; default false (treat both as TAI)
  "site":       "summit",              // optional; falls back to default_site
  "username":   "merlin", "password": "...",
  "workers":    8,
  "windowBefore": 5.0, "windowAfter": 300.0
}
```

The client resolves `tZeroStart` / `tZeroStop` up front via
`/api/exposure-time`. The window is one wide all-pods span,
`[tZeroStart - windowBefore, tZeroStop + windowAfter]`. The span
`rangeStop - rangeStart` is capped at `config.MAX_RANGE_SPAN` (currently
500); a larger span — or `rangeStop <= rangeStart` — returns `400`.
Per-dataId shutter closes
for the whole span are resolved server-side post-parse (ConsDB batch,
reporting on the `shutter-close` SSE event). Same response shape as
`/api/fetch`.

### `GET /api/fetch/<jobId>/status`

JSON snapshot of one job:

```jsonc
{
  "jobId": "8970db79c0a6",
  "status": "running" | "parsing" | "done" | "error",
  "kind":   "exposure" | "night" | "range",
  "site":   "summit",                  // the named site this job fetched against
  "expId":  2026051900722,             // null for night / range jobs
  "tZero":  "...",                     // null for night / range jobs
  "dayObs": null,                      // 20260521 for night jobs
  "startId": null, "stopId": null,     // set for range jobs
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
- `shutter-close`: `{ phase, ... }` — night **and range** modes (both
  resolve per-dataId shutter closes post-parse via the shared
  `_resolveShutterClosesInto`). Phases:
    - `starting`: `{ total }` (number of dataIds we'll try to
       resolve)
    - `cache-checked`: `{ cacheHits, manualStandins, remaining }` —
       after the on-disk shutter-close cache pass. `manualStandins`
       counts `_manual` hits, which anchor their id *and* still join
       the ConsDB batch so a real row can supersede them.
    - `no-token`: `{ remaining, tokenPath }` — token file missing;
       remaining dataIds can't be resolved.
    - `empty-token`: `{ remaining }` — token file blank.
    - `consdb-error`: `{ error }` — typed ConsDB error.
    - `done`: `{ consdbHits, stillMissing }` — happy path.

  `remaining` / `stillMissing` count only dataIds left with **no** t₀ at
  all; a manual stand-in that ConsDB still couldn't supersede is anchored,
  so it is not counted as missing.
- `done`: `{ kind, expId, tZero, dayObs, startId, stopId, cacheDir,
  cacheReuse, podCount, totalBytes, elapsedS }` — after the server's
  keyed `ServerState` / `NightState` / `RangeState` slot has been
  populated (`startId`/`stopId` set for range jobs). **Always** fired
  after `onComplete` so SSE consumers can rely on the summary being
  ready when they see `done`.
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

- inserting / evicting entries in `ctx.exposureStates`,
  `ctx.nightStates`, and `ctx.rangeStates` (worker thread → ✓ insert;
  DELETE handlers → ✓ evict);
- reading those dicts to render `/api/summary` or `/api/pod/<>`
  (request threads → snapshot read).

Each fetch runs on its own daemon thread spawned by
`JobManager.startJob`. SSE handlers block on
`FetchJob.condition.wait()` to be notified when new events arrive.

### Multi-tab support

All three keyed-state dicts are LRU-ordered (`OrderedDict.move_to_end`
on every read) and each capped at `_MAX_LOADED_STATES = 8`. A user with
several open tabs (different dataIds / dayObses / ranges) sees each one
keep its state until 9+ tabs of that kind are in play; the
least-recently-opened gets evicted then. The browser's URL carries the
routing key so reload / back-button on an evicted tab triggers a fresh
`/api/summary` fetch against the cache.

Pod detail and traceback drilldown responses re-read the JSONL
files from disk on each request rather than buffering them in
memory — the cache for one exposure is typically ~40 MiB so this
stays cheap.

## Three startup modes

1. **Home mode** — `python3 -m ra_log_explorer.cli` with no
   `--exposure-id`/`--t-zero`. CLI just spins up a fresh `JobManager`
   and an empty `ServerContext`, hands it to `server.serve()`, and
   the user picks an exposure (or a dayObs for night mode, or a
   start/stop pair for range mode) in the browser. Every fetch from
   then on goes through `POST /api/fetch`, `POST /api/fetch-night`, or
   `POST /api/fetch-range` and the SSE progress endpoint.

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
