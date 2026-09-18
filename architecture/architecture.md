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

## How it runs

**The tool is a deployed service.** It runs as the Phalanx application
`log-explorer` on the two clusters whose pipelines it explains — the Base
Test Stand (`manke`) and the summit (`yagan`) — served at
`https://<fqdn>/log-explorer` behind Gafaelfawr, from the container image
built by this repo. That is how everybody who uses it uses it, and it is
the code path to keep working.

One deployment serves exactly one observatory. Which one is not a runtime
choice: it follows from where the process runs, and reaches that
cluster's own ConsDB at an in-cluster Service address. All the
configuration that makes an instance *that* instance arrives as
environment variables (see [Configuration](#configuration)); the browser
can set none of it.

Running it from a laptop still works and is how this repo is developed —
`python3 -m ra_log_explorer.cli` binds `127.0.0.1`, serves at the root
instead of under a path prefix, and reads a ConsDB bearer token from disk
because it is outside the cluster. Treat that as a development
convenience rather than a supported way to operate the tool: it is
single-user, unauthenticated, and depends on the developer's own
credentials. When the two modes disagree about something, the deployed
one is right.

The chart lives in the [Phalanx](https://github.com/lsst-sqre/phalanx)
repository under `applications/log-explorer/`; this repo owns the
application and the image. A change to the configuration surface here
means a matching change there — see
[Configuration](#configuration).

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
    │   live.py                  │  live-mode poller (deployments): one daemon
    │   (LiveNightManager)       │  thread appending the current night into a
    │                            │  live night dir via fetch.py, ConsDB per
    │                            │  tick, readiness snapshot for /api/live
    └─────────────┬──────────────┘
                  │ (the night dir it maintains is what fetchAll's
                  │  night-slice path serves windows out of)
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
    │                            │ ◄────── GET    /healthz                   (readiness probe)
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
    │                            │ ◄────── GET    /api/site                   (read-only label)
    │                            │ ◄────── GET    /api/live                   (live-mode snapshot)
    │                            │ ◄────── POST   /api/fetch                  (exposure)
    │                            │ ◄────── POST   /api/fetch-night            (dayObs)
    │                            │ ◄────── POST   /api/fetch-range            (start/stop)
    │                            │ ◄────── GET    /api/fetch/<id>/status
    │                            │ ◄────── GET    /api/fetch/<id>/progress    (SSE)
    └────────────────────────────┘
       every route above is mounted under ServerContext.basePath — "" for a
       local run, e.g. /log-explorer when deployed behind a shared hostname
                  ▲
                  │ HTML / CSS / JS (vanilla; no build step)
                  │
       static/    │           templates/
       ├─ app.js (bootstrap)   └─ timeline.html (home + explore + night SPA)
       ├─ home.js
       ├─ explore.js  (per-exposure timeline + detail drawer)
       ├─ night.js    (dayObs-wide histograms + failure drilldown)
       ├─ range.js    (range navigator strip; drives the explore view
       │               for the selected dataId in the range)
       ├─ favicon.png (tab icon)
       └─ logo.png    (the mark in every view's topbar)

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

       sites.py           Site catalog. A *site* pairs a Loki cluster
                          with the ConsDB endpoint that owns its
                          shutter-close truth. yagan→summit and
                          manke→bts; the table lives in sites.toml,
                          which a deployment replaces via
                          RA_LOG_EXPLORER_SITES_FILE. `--site` picks
                          one entry at startup and that is the site
                          for the life of the process.
```

## Module Responsibilities

| Module             | Responsibility                                                                |
|--------------------|--------------------------------------------------------------------------------|
| `config.py`        | `FetchSpec` (frozen dataclass), cache-path helpers, dayObs ↔ UTC conversions, the `NIGHT_AOS_POD_REGEX` / `NIGHT_VIEWS` constants, base-path canonicalisation (`normalizeBasePath` / `defaultBasePath`), and **every deployment-varying default**, read from the environment once at import (see *Configuration* below). `_envInt` / `_envFloat` raise `ConfigError` on a malformed value — blank included — rather than falling back, so a typo'd Helm value stops the container instead of silently never taking effect. |
| `fetch.py`         | `logcli` subprocess wrapper. Lists pods, fetches each pod's JSONL in parallel as count-presized single-batch chunks (works around grafana/loki#17270; see [caching.md](caching.md)), plus a cheap second pass for each pod's `k8s/events` lifecycle stream into `pods_events/`. Manages the on-disk cache (exact / superset / night-slice reuse), the schema-version flush, the `.partial` flag, the `_last_viewed.txt` and `_exposure_ids.txt` sidecars, the per-window write lock, and LRU disk eviction. |
| `parse.py`         | Parses Loki JSONL → `LogLine` → `Event`. Owns the regex taxonomy in [parsing.md](parsing.md). Also parses the `k8s/events` stream into `POD_*` lifecycle Events (`classifyK8sEvent`), captures `TracebackRecord`s with class + capped body, and the carryover-aware dataId attribution per pod group. |
| `night.py`         | dayObs-wide rollups computed off `list[PodSummary]`: top stats, errors-by-type and -by-pod, first-task-start and calcZernikes-end histograms, the failure-row drilldown table, and the gather-only completeness check (dataIds with step1b activity but no step1a — impossible, so a dropped-logs tell). No I/O. |
| `exposureTimes.py` | dataId → curated ConsDB *exposure record* (`{instrument, obs_end, exp_time, physical_filter, img_type, science_program, observation_reason, group_id, cur_index/max_index, …}`, the `EXPOSURE_RECORD_COLUMNS` projection of a `SELECT *` plus the `instrument` the record was read out of). Exposure ids are unique only *within* an instrument, so records are keyed `(instrument, id)` as well as by the bare id; `probeOrderWinners` is the one place that decides what a bare id resolves to, and `queryExposureRecordsForDayObs` returns a list so a shared id can't drop an exposure. `obs_end` is the shutter-close (TAI) t-zero; `obsEnd(record)` pulls it out. Every public helper takes the ConsDB URL and resolved bearer token from the caller, so the same dataId can be queried against multiple sites without crosstalk. Probes `cdb_lsstcam.exposure` first and falls through to LATISS; `INSTRUMENTS_BY_PROBE_ORDER` is exactly those two — the whole set the observatory runs today — so an instrument outside the tuple is rejected rather than probed. Persists records per-site to `<cache_root>/exposure-times/<siteName>.json` — exposure properties are immutable so the cache never goes stale. Provides `queryExposureRecordBatch` for night/range prefetches (one `IN (…)` query per instrument, chunked). |
| `sites.py`         | The site catalog (`sites.toml`). Loads at server start into `ServerContext.sites`. Each `Site` carries (`name`, `cluster`, `namespace`, `lokiAddr`, `consdbUrl`, `consdbTokenFile` — the last optional, `None` for a ConsDB that needs no auth). `siteByName` / `siteByCluster` are the lookups; the latter is how cache-rehydration paths figure out which site a window belongs to from its on-disk cluster component. |
| `jobs.py`          | `FetchJob` + `JobManager` — the in-process worker pool the browser uses to kick off fetches. One daemon thread per job, an append-only event log per job (guarded by a `threading.Condition`), and the single `stateLock` that guards the keyed-state dicts. `createJob` (exposure), `createNightJob` (dayObs), and `createRangeJob` (start/stop pair) put a `kind` discriminator on each job. |
| `live.py`          | `LiveNightManager` — the live-mode poller (deployments only; enabled by `RA_LOG_EXPLORER_LIVE_POLL_S > 0`). One daemon thread that incrementally fetches the current night's all-pods logs into a live night dir every tick, queries ConsDB for tonight's exposures per instrument, computes which are *ready* (shutter close + windowAfter ≤ watermark), finalises the night at noon-UTC rollover (verification pass + `_meta.json`), sweeps up nights an earlier restart left unfinalised, and publishes the snapshot `GET /api/live` serves. See *Live mode* below and [caching.md](caching.md) for the on-disk contract. |
| `server.py`        | Stdlib `ThreadingHTTPServer` + JSON / SSE endpoints + static files, all mounted under `ServerContext.basePath`. Holds a long-lived `ServerContext` containing the `JobManager` and three LRU `OrderedDict`s of loaded states (`exposureStates: {expId → ServerState}`, `nightStates: {dayObs → NightState}`, `rangeStates: {"start-stop" → RangeState}`). Multiple tabs / dataIds / dayObses / ranges coexist; oldest-by-access gets evicted when `_MAX_LOADED_STATES` (8) is exceeded. |
| `cli.py`           | Argument parsing + the optional "eager fetch" path (exposure mode only). Builds a `ServerContext` and hands it to `server.serve()`. When `--exposure-id`/`--t-zero` are omitted, hands over an empty context and lets the browser drive. Also hosts the `cache info`/`cache flush` subcommands. |
| `static/`          | Single-page vanilla JS UI split for clarity: `app.js` (bootstrap, URL routing, browser-history entries, view switching), `home.js` (landing page forms, Tonight panel, progress, site badge, and the admin view's cache browser), `explore.js` (per-exposure timeline + detail drawer), `night.js` (dayObs histograms + failure drilldown), `range.js` (range navigator strip that drives the explore view per selected dataId), plus the two images the page draws: `favicon.png` (the tab icon, declared in the template's `<link rel=icon>`) and `logo.png` (the observatory lockup, shown whole at the top-left of every view's topbar, which is what sets the bar's ~73 px height). Both are downscaled copies of the full-resolution art in `assets/`, because static files are sent with `Cache-Control: no-store` and are therefore re-fetched on every page load. One HTML template (`templates/timeline.html`) holds the home/admin/explore/night sections; the bootstrap shows whichever matches the URL (`/?admin=1` routes to the admin view, which hosts the cached-windows table and the flush-cache button). No build step. |

## Key Concepts

- **Base path** — the URL prefix the app is served under. Empty for a
  local run (`http://127.0.0.1:8780/`); `/log-explorer` when deployed
  behind a Gafaelfawr ingress that shares a hostname with the rest of
  the RSP. Set with `--base-path` or `RA_LOG_EXPLORER_BASE_PATH`,
  canonicalised by `config.normalizeBasePath`, and carried on
  `ServerContext.basePath`.

  It acts in two places, which have to agree. Inbound, every `do_*`
  method runs the request path through `Handler._routePath`, which
  strips the prefix and 404s anything outside it (rather than serving
  the home page to a URL belonging to another app on the same host).
  Outbound, `templates/timeline.html` carries a literal `__BASE_PATH__`
  at each URL back to us (and `__APP_TITLE__` wherever the page names
  itself); `Handler._send_index` substitutes them at request time, which is also what defines `window.BASE_PATH` and the
  `window.apiUrl()` helper that every `fetch` / `EventSource` / deep
  link in `static/*.js` goes through. Substituting per-request rather
  than at build time keeps the container image environment-agnostic
  and keeps the no-build-step edit-and-reload loop working locally.

  What survives `_routePath` as `/static/<rel>` is then resolved by
  `_resolveStaticFile`, which joins and *then* checks containment:
  `rel` must be relative, and the resolved path must sit inside
  `STATIC_DIR` or the request 404s. Screening the request text for
  `..` is not sufficient, because `Path("static") / "/etc/passwd"`
  discards the left operand entirely — `/static//proc/self/environ`
  read an absolute path that way and handed back the process
  environment, `LOKI_PASSWORD` included.

- **URL and history** — the query string *is* the router. `app.js`'s
  `bootstrap()` reads `?dataId` / `?dayObs&nightView` /
  `?rangeStart&rangeStop` / `?admin=1` off the URL and renders the
  matching view, which is what makes every view a link that can be
  pasted, reloaded, or opened in a second tab.

  Because the views swap inside one document, the entries the browser's
  Back button walks are the app's own to create — and until
  `window.navigateTo(query, {replace})` existed it created none, leaving
  the whole session on one entry so Back left the application entirely.
  It pushes for a transition the user asked for (home → a view, a view →
  home) and rewrites the current entry for anything that merely refines
  the view already on screen: the instrument pin, the range navigator's
  selected exposure, and the `?dataId=…&autoFetch=1` entry a night-view
  drilldown lands on — Back onto that one would re-fire the fetch rather
  than return anywhere. A `popstate` listener re-runs `bootstrap()`,
  which is the whole of back/forward handling, and a `routeToken` guard
  discards a routing pass whose `/api/summary` answer arrives after a
  later one has overtaken it.

- **Site** — a (Loki cluster, ConsDB endpoint, optional bearer-token
  file) bundle that pairs the *log source* with the *truth source* for
  shutter-close times. Catalogued in the checked-in
  [`ra_log_explorer/sites.toml`](../ra_log_explorer/sites.toml);
  loaded once at startup into `ServerContext.sites`. Today there are
  two: **summit** (cluster `yagan`, ConsDB at
  `usdf-rsp.slac.stanford.edu`, token `~/.lsst/log-browser-token.txt`)
  and **bts** (cluster `manke`, ConsDB at `base-lsp.lsst.codes`, token
  `~/.lsst/manke-token.txt`).

  A site also carries a **title** — what the page calls itself in the
  browser tab and beside the logo in every topbar. `summit` is *Summit
  Log Explorer* and `bts` is *Base Log Explorer*; neither names a
  pipeline, because the tool is pointed at a namespace rather than
  married to one. The mapping lives in `sites.SITE_TITLES`, keyed by
  site name, rather than being required in the catalog: a deployment's
  catalog is rendered by the Phalanx chart, which has no field for it,
  so requiring one would leave both deployed instances calling
  themselves something nobody calls them. A catalog entry can still set
  `title` outright, which is where a site added later should say what it
  wants to be called, and `sites.DEFAULT_TITLE` (a plain *Log Explorer*)
  covers a site with neither.

  `consdbTokenFile` is optional. Omitting it (or leaving it blank)
  means the endpoint takes no bearer token — the case for a
  cluster-internal ConsDB Service address, which is reached without
  passing through Gafaelfawr. A deployed instance ships a generated
  one-site catalog of exactly this shape via
  `RA_LOG_EXPLORER_SITES_FILE`.

  Sites matter because the same bare dataId can refer to a real-camera
  exposure on the summit and a *different*, simulated exposure on BTS
  — different `obs_end` values, separate sources of truth. Every
  shutter-close lookup and every exposure-time cache file is scoped by
  site to keep them from crosstalking.

  **One process serves exactly one site.** `--site` (or the catalog's
  `default_site`) picks it at startup; `ServerContext.site()` is the only
  way to reach it, and no request can name a different one. A deployment
  on manke means BTS and one on yagan means the summit, so a `site` field
  in a request body is ignored rather than honoured — the same dataId
  exists at both observatories with different `obs_end` values, and a
  summit deployment answering with BTS data would look plausible rather
  than wrong. The catalog can still list several entries, which is what
  lets a laptop point at either.

- **dataId / expId** — 13-digit `YYYYMMDDSSSSS` integer (e.g. `2026051900722`).
  Exposure mode targets a single one of these at a time.

  **A dataId is not unique.** `SSSSS` is a per-instrument sequence
  number restarting at 1 each night, so on any night LSSTCam and LATISS
  both observe — most of them — `2026051900001` names a different
  exposure on each, with a different shutter close. The full identity is
  `(instrument, dataId)`.

  The **data layer** treats it that way: every ConsDB record carries its
  `instrument` (stamped from the table it was read out of, not trusted
  to a column), `queryExposureRecordsForDayObs` returns a list rather
  than an id-keyed map, the on-disk exposure-time cache keys records
  under `<instrument>:<id>`, and `/api/exposure-time/<id>` accepts an
  `?instrument=`. The **bare id** is still a meaningful question with a
  defined answer — "whichever instrument `INSTRUMENTS_BY_PROBE_ORDER`
  reaches first" — and that is what an unqualified *lookup* and the bare
  cache key resolve to; every writer agrees on that rule so the answer
  can't depend on who wrote last.

  That rule governs the data layer, not the in-memory slots. An
  unpinned `/api/summary?dataId=` is answered from whichever instrument's
  state currently occupies the bare key, which need not be the
  probe-order one — the guard below only refuses a *mismatch*, and with
  no pin there is nothing to mismatch. Every link the UI emits carries
  its instrument, so this is reachable only by hand-typing a URL or
  following a bookmark predating the pin; the payload names the
  instrument it is describing.

  The **serving layer** is pinned per page. The home topbar carries an
  instrument switch (LSSTCam default — always the default when one must
  be picked); the Tonight list, every dataId lookup, and every fetch it
  launches are scoped to the selected instrument. `POST /api/fetch` and
  `POST /api/fetch-range` take an `instrument` (defaulting to
  `lsstcam`), the resulting states carry it, their shutter-close
  resolutions — including the range mode's server-side batch — query
  only that instrument's table, and the exposure payload attributes
  work only to pods of that instrument (plus instrument-neutral pods
  like redis; see `_podBelongsToInstrument`). Night mode needs no
  parameter: AOS runs on LSSTCam only, and its resolutions are
  hard-pinned there.

  One deliberate simplification remains: the in-memory `exposureStates`
  dict and the range key are still keyed by the bare id, so two
  colliding exposures share one in-memory slot — opening one evicts the
  other. (The on-disk `_exposure_ids.txt` is not: it records
  `<instrument>:<dataId>`, because a cache listing has to say which of
  the two a window holds.) Correctness is protected by a guard
  rather than by the key, and the guard covers **both** keyed forms.
  `/api/summary` and `/api/pod/<pod>` refuse to serve a loaded state
  pinned to a different instrument, whether the key is `dataId=` or
  `rangeStart=&rangeStop=`: the summary treats the mismatch as
  not-loaded and falls through to the rebuild/fetch path, which
  re-pins; the pod detail 404s and the next summary reload re-pins.
  The rebuild paths carry the pin too — `_loadExposureFromCache`
  verifies the window it found actually contains the pinned t₀, and
  `_findRangeCacheDir` only matches a cache whose `_range.txt` records
  the same instrument. A range needs this as much as an exposure does:
  `[startId, stopId]` names a different run of exposures on every
  instrument that observed that many, so an unpinned lookup hands a
  LATISS tab whichever twin span was fetched most recently.

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
  `POD_MOUNT_FAILED`, `POD_STARTED`) — these carry no dataId and put the
  k8s `reason` in `flavor`. See [parsing.md](parsing.md) for the full
  taxonomy.

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

## Live mode

Deployed next to the data, the tool no longer treats Loki as distant and
expensive: when `RA_LOG_EXPLORER_LIVE_POLL_S > 0`, a
`live.LiveNightManager` daemon thread keeps the **current night hot on
disk** so that routine "how did that image process" questions during
observing are answered from the cache volume, not by a fresh Loki fetch.

Each tick (every `LIVE_POLL_S` seconds):

1. **Incremental fetch.** Every pod's app logs are advanced from its
   per-pod watermark to `now - LIVE_LAG_S` with the same lossless
   count-presized single-batch chunking as a batch fetch, appended to
   the per-pod JSONL files of one all-pods *live night dir* (the
   ordinary window path for the dayObs's noon→noon span). Consecutive
   ticks tile the night exactly — half-open windows, no line fetched
   twice, none dropped — so a night costs O(night) rather than the
   O(night²) of re-fetching from night start each time. The `_live.json`
   sidecar (schema in `fetch.py`) records watermarks and per-pod byte
   counts; appends are staged through temp files *on the cache volume*
   so a failed pod's span is simply retried next tick, and a crash
   recovers by truncating files back to the recorded counts (and
   deleting the stranded temps).

   An append that fails part-way is rolled back to the pre-append size
   — app logs and the events stream alike. It has to be: the watermark
   is left alone on failure, so the same span is fetched again next
   tick and appended *after* whatever landed, and the byte count
   (advanced only on success) would later be extended over the
   stranded bytes. That yields a range the sidecar vouches for holding
   duplicated, torn, non-ascending lines, which the slicer's bisect
   silently mis-answers and the finalisation audit cannot see, because
   the line counter was never advanced either.

   The k8s/events stream is fetched **namespace-wide** once per tick and
   demuxed by the `name` label. That is deliberately broader than "the
   pods we know about": the stream also carries ReplicaSet, Job and
   Deployment events, and keeping them costs almost nothing while a
   narrower filter would throw away lifecycle context. Nothing
   downstream is confused by the extra names — `classifyK8sEvent`
   ignores any event whose involved object isn't a Pod, and
   `summarizeAll` enumerates pods from `pods/`, so a name with no app
   logs is never opened. They are filed in the sidecar's `eventPods`
   section rather than `pods`, because **only app-log pods have a
   watermark**: the global watermark is the minimum over `pods`, and a
   name that has never emitted an app log makes no claim about coverage.
   (Letting one in would drag the watermark back to night start every
   time a pod was rescheduled.) A name promotes into `pods`, carrying
   its event counters, the first time it appears in an app-log listing.

   A line carrying no `name` label at all can't be filed under anything,
   so it is dropped — and the watermark advances past it, so nothing
   comes back for it later. Whether live Loki ever emits one isn't
   answerable from this repo (the captured corpus is already demuxed, so
   every line in it lost the label on the way in), so they are counted
   cumulatively into the snapshot's `eventsUnfiled` instead of assumed
   away. A non-zero value there means POD_\* markers are going missing.

   The events stream keeps its own watermark, and a failed events fetch
   is retried even on a tick where the app-log frontier has nothing to
   do — which is permanent at night end, where every later tick (and
   finalisation itself) finds the app-log watermark already at its
   target. Tying the retry to app-log progress would strand a
   late-night events failure forever, and the night would finalise
   missing the tail's lifecycle markers.
2. **ConsDB.** One id-range query per instrument returns every exposure
   of the current dayObs (in-cluster, this is ~free). All instruments
   are queried, not just the first with rows, and the result is a *list*
   rather than an id-keyed map: an exposure id is only unique within one
   instrument, and LSSTCam and LATISS share ids on any night they both
   observe. Records land in the per-site exposure-time cache — each
   under its own `(instrument, id)` key, with the probe-order winner
   also under the bare id — so the home form resolves tonight's dataIds
   instantly, and in the live snapshot.
3. **Readiness.** An exposure is *ready* when
   `shutterClose(UTC) + windowAfterS <= watermark`: the whole default
   exposure window is already on disk. The snapshot carries one row per
   (instrument, exposure); the home page's **Tonight** panel shows the
   rows for the topbar's selected instrument, newest-first, with
   ready/wait status, and links ready ones straight into the ordinary
   explore flow — carrying `&instrument=` so the link resolves the
   right exposure's shutter close.
4. **Housekeeping.** When the poller opens a night it sweeps its
   cluster/namespace for night dirs it left *unfinalised* — what a
   restart across noon produces — and finalises one per tick. Nothing
   else ever revisits a past dayObs, so without the sweep such a
   directory is stranded forever: no `_meta.json`, so the cache listing
   skips it and LRU eviction can't reclaim it, while `du` still counts
   its ~9 GiB against the size cap.

Serving leans on one mechanism: `fetch.fetchAll` tries the night dir
before the superset path, and any request whose window ends at or
before the watermark is **sliced** out of it (`materializeNightSlice`:
binary-search each time-ascending JSONL for the boundary offsets, copy
the byte ranges) into a completely ordinary cache dir, returned with
`cacheReuse: "night-slice"`. A `podRegex` request (night mode) slices
only the matching pods, into the nested `pods=` dir a real filtered
fetch would use. No Loki round trip, no new serving path downstream —
the slice is a first-class window that later requests exact-hit or
superset-reuse. Night dirs themselves are excluded from superset reuse
(parsing a whole night to answer a five-minute question would take
minutes; slicing is near-instant).

The night dir is the poller's, and nothing else may write into it: a
request for the night's *own* window is handed the directory itself with
a synthesized meta rather than sliced (copying it onto itself would open
every pod file for writing while reading it), and a fresh fetch that
would land on a path carrying a `_live.json` is refused outright rather
than truncating files the poller is appending to.

One window gets special treatment: a request for the in-progress
night's *own* window — which is exactly what night mode asks for on the
current dayObs — is served **clamped to the watermark**, i.e. "the
night so far", so opening the night view during observing costs no Loki
fetch (the parse still takes its normal time). Only the night's own
window is clamped; an arbitrary user window extending past the
watermark falls through to a real fetch rather than silently coming
back short. Successive opens as the watermark advances each materialize
a fresh clamped slice (an unchanged watermark reuses the previous one);
the stale ones are ordinary windows that LRU eviction reclaims.

At noon-UTC rollover the night is **finalised**: a last top-up to night
end, then a verification pass comparing each pod's appended line count
against the `count_over_time` oracle — a pod falling short by more than
the oracle's dedup slack (metric queries count duplicate storage-chunk
entries the log path deduplicates; measured ~0.03%, so the bar is
`max(100, 0.2%)` — see `live.VERIFY_TOLERANCE_*`) is refetched whole,
catching e.g. lines ingested later than `LIVE_LAG_S` allowed for — and
a normal `_meta.json` is written.

While a pod is being swapped out that way the sidecar carries
`rewritingPod: "<pod>"`, and `_tryNightSlice` declines to slice the
night at all, falling back to an ordinary fetch for the minutes the
refetch takes. Zeroing the pod's byte count is not protection on its
own: a slicer reading a zeroed record doesn't wait, it *omits the
pod*, and since finalisation has already cleared that pod's
fall-short flags the resulting slice is written `fetchComplete: true`,
missing a pod entirely, and then exact-hits every later identical
request forever. The window is wide and badly timed — a whole-night
refetch takes minutes, and it runs exactly when people open the night
that just ended.

From then on the dir is a
complete, trustworthy all-pods night window (still sliced, never
superset-parsed), subject to ordinary LRU eviction; the poller moves on
to the new night. On the deployments, one busy night is ~9 GiB of JSONL
on the cache volume (measured: 35.7M lines / 576 pods for 20260711), so
the chart's 50 GiB volume holds four-or-so finalised nights plus slices
before LRU eviction reclaims the oldest. Night mode's own "night so
far" windows do not accumulate against that: each fetch supersedes the
last one's, and the fetch-job callback drops the windows it contains
(see *Night slice* in [caching.md](caching.md)).

The parse cost of the *night view* is deliberately left alone: opening
the in-progress night slices the AOS subset instantly but still parses
it (~1–2 minutes for a busy night), and that is an accepted cost — an
incremental in-memory summarizer that the poller feeds each tick was
considered and rejected as not worth the refactor of the parser's
stateful loop. If that ever changes, the path is a resumable
`summarizePod` plus more pod memory (whole-night summaries measure
~1–2 GiB); nothing in the current design blocks it.

Live mode changes nothing when it is off (the local default): no
thread starts, `/api/live` reports `{enabled: false}`, the Tonight
panel stays hidden, and every fetch path behaves as before.

## Configuration

Everything that varies between deployments is an environment variable,
read in `config.py` — the numeric ones once at import, so a bad value
stops the container rather than surfacing hours later; the paths
(`RA_LOG_EXPLORER_CACHE`, `_BASE_PATH`, `_SITES_FILE`) and
`LOKI_PASSWORD` per call, so those fail at first use instead. Nothing
is configurable from the browser: the UI asks questions about
exposures, it does not reconfigure the service that answers them. That
is not tidiness — a shared deployment has many users and one process,
so a settings field would let whoever touched it last change how
everyone else's fetches behave.

| Variable | Drives | Default |
|----------|--------|---------|
| `RA_LOG_EXPLORER_BASE_PATH` | URL prefix the app is served under | `""` (the root) |
| `RA_LOG_EXPLORER_CACHE` | `cache_root()` | `~/.cache/ra_log_explorer` |
| `RA_LOG_EXPLORER_SITES_FILE` | which `sites.toml` to load | the packaged one |
| `RA_LOG_EXPLORER_WORKERS` | `DEFAULT_WORKERS`, the parallel fetch width | `8` |
| `RA_LOG_EXPLORER_WINDOW_BEFORE_S` | starting value of the window-before field | `5` |
| `RA_LOG_EXPLORER_WINDOW_AFTER_S` | starting value of the window-after field | `300` |
| `RA_LOG_EXPLORER_MAX_CACHE_BYTES` | LRU eviction ceiling in `fetch.evictToFit` | 5 GiB |
| `RA_LOG_EXPLORER_LIVE_POLL_S` | live-mode poll interval; `0` disables live mode | `0` (off) |
| `RA_LOG_EXPLORER_LIVE_LAG_S` | how far behind *now* each live increment stops (Loki ingestion lag) | `60` |
| `LOKI_USERNAME` | `DEFAULT_USERNAME`, the Loki basic-auth user | `merlin` |
| `LOKI_PASSWORD` | the Loki basic-auth password, read by `logcli` | *(required)* |

A malformed numeric value raises `ConfigError` at import rather than
falling back to the default: a container that refuses to start is much
easier to notice than a setting that quietly never took effect.

**A present-but-blank value counts as malformed.** Only an *absent*
variable asks for the default. Blank is what a mistyped Helm reference
renders to (`value: {{ .Values.typo }}`), which is precisely the
never-took-effect case the loud failure exists to prevent.
`LOKI_PASSWORD` follows the same rule for the same reason: its
VaultSecret is marked optional, so the variable can exist and be empty
while the secret is still missing, and `fetch._run_logcli` treats a
blank or whitespace value as unset — otherwise the operator gets a
bare auth failure out of `logcli` instead of the sentence naming the
variable.

The two window values are the *starting* values of editable form fields,
not fixed limits — widening a window to catch a neighbouring exposure is
a real investigative move, so the deployment chooses where the fields
start and the user can still change them for a given fetch. Everything
else in this table the browser cannot influence at all: a `site`,
`username`, `password` or `workers` field in a request body is ignored,
not honoured.

Deployed, `RA_LOG_EXPLORER_MAX_CACHE_BYTES` is derived from the size of
the volume provisioned for the cache, so "how much disk may this use" is
answered once, in the Helm values, rather than in two places that can
disagree.

### How the deployment supplies it

The Phalanx chart at `applications/log-explorer/` in the
[Phalanx](https://github.com/lsst-sqre/phalanx) repo is what sets all of
the above. Its shape, and the reasons behind it:

| Piece | Why it is the way it is |
|-------|-------------------------|
| One replica, `Recreate` | Loaded exposures live in the serving process's memory and the cache volume is ReadWriteOnce, so a second replica would answer differently depending on which pod took the request. At one replica RollingUpdate's `maxUnavailable` floors to zero and wedges any rollout whose new pod fails readiness. |
| PVC for the cache | Fetching a night out of Loki takes minutes. An emptyDir would discard it on every restart — worst precisely when somebody is restarting things to investigate. `RA_LOG_EXPLORER_MAX_CACHE_BYTES` is derived from the volume's own size so the app cannot believe it has more room than it does. With live mode on, the volume also absorbs ~9 GiB of live night per night, and a restart resumes the night from the sidecar's watermark instead of re-pulling from noon — another reason it must not be an emptyDir. |
| `livePollS: 300` / `liveLagS: 60` | Turns on live mode (see *Live mode*). 300 s keeps the steady Loki load modest — an exposure becomes viewable at most ~5 min later than its shutter+5 min ideal — and 60 s of lag keeps the fetch frontier behind Loki's ingestion frontier so late-arriving lines aren't skipped. `livePollS: 0` reverts the deployment to purely on-demand fetching. |
| ConfigMap for `sites.toml` | Mounted at `/etc/ra-log-explorer/`, naming exactly one site. A checksum annotation on the pod rolls it when the catalog changes, since a file mount is not an env var and would otherwise go unnoticed. |
| `GafaelfawrIngress`, `loginRedirect: true` | A browser app, so anonymous users get sent to log in rather than a 401 they cannot act on. Scope `read:image`, matching rubintv on the same environments. |
| `proxy-buffering: "off"` | `/api/fetch/<id>/progress` is Server-Sent Events for the length of a fetch. nginx buffers proxied responses by default, which would hold the whole stream until the fetch had already finished. |
| Readiness probe on `<base>/healthz` | With headroom over the defaults: parsing is CPU-bound pure Python contending with the fetch threads, so latency spikes mid-fetch — the worst moment to drop the only pod out of the Service. |
| `LOKI_PASSWORD` from a VaultSecret | Marked `optional` so the pod still starts before the secret exists, which is safe rather than silent: the app refuses to run logcli without a password — blank counts as without — and says so. |

**A change to the configuration surface here needs a matching change
there, in the same breath.** Adding an environment variable this code
reads without adding it to the chart produces an application that
silently runs on defaults in production — the failure mode is a setting
that appears to do nothing, which is exactly the sort of thing nobody
notices for months. The
[architecture-sync skill](../.claude/skills/ra-log-explorer-architecture-sync/SKILL.md)
spells out the cross-repo rule.

## JSON API

Every path below is relative to the deployment's **base path** (see Key
Concepts): served verbatim for a local run, and under e.g.
`/log-explorer` when deployed. The browser never hard-codes them — they
all go through `window.apiUrl()`.

### `GET /healthz`

`{"status": "ok"}`, always 200. Deliberately touches no state: it is what
the deployment's readiness probe polls, and a probe that a slow fetch
could make fail would pull the only pod out of the Service mid-
investigation.

### `GET /api/summary`

The view-state lookup. The query string picks which loaded state to
return:

- `?dataId=<int>[&instrument=<name>]` — return that exposure's payload,
  or `{loaded: false, cache}` if not loaded. With `instrument` given, a
  loaded state pinned to a *different* instrument is treated as not
  loaded rather than served — the same bare id names a different
  exposure per instrument — and the rebuild path resolves under the
  pin. 400 for an unknown instrument name.
- `?dayObs=<int>[&nightView=aos|sfm]` — return that night's payload for
  the named half, or `{loaded: false, cache}` if that half isn't loaded.
  `nightView` defaults to `aos` and an unrecognised value falls back to
  it rather than erroring: this is a query string a person can type, and
  a 400 there helps nobody. The two halves are separate states, so the
  same dayObs can have one loaded and not the other.
- `?rangeStart=<int>&rangeStop=<int>[&instrument=<name>]` — return that
  range's **index** payload (`mode: "range"`), or `{loaded: false,
  cache}` if not loaded.
- `?rangeStart=<int>&rangeStop=<int>&dataId=<int>[&instrument=<name>]` —
  return one in-range exposure's timeline (`mode: "range-exposure"`, the
  same shape as the exposure payload plus a `podDetailQuery`). 404 if
  the dataId has no resolved shutter close (a skipped integer).

  `instrument` guards the range key exactly as it guards the bare
  dataId: `[startId, stopId]` is the whole key, and every instrument
  that observed that many exposures has a span with those bounds, so a
  loaded state pinned elsewhere is treated as not loaded and the
  rebuild re-pins (`_findRangeCacheDir` matching on the instrument
  `_range.txt` records). 400 for an unknown instrument name.
- no params — home view shape (`{loaded: false, cache}`).

If the requested key isn't in the in-memory state dict, the server
makes one attempt to reconstruct it from disk: it walks the cache root
for a matching window (an exposure-mode cache whose
`_exposure_ids.txt` lists the dataId *under the pinned instrument*; a
night-mode cache whose
window starts at noon UTC of the dayObs; or a range cache whose
`_range.txt` records the `[startId, stopId]` bounds), reparses it with
`parser.summarizeAll`, and returns the rebuilt payload. This lets a
deep-linked tab (e.g. opening the dataId column in the admin view's
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
  "instrument": "lsstcam",          // the pin this state was fetched under
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
`group == "other"` pod as a safety net for unknown roles — restricted
to pods of the state's `instrument` (plus instrument-neutral pods like
redis; see `_podBelongsToInstrument`), because a cross-instrument pod
that logged this bare id was working on a *different exposure*.
`podsAll` is every pod that emitted in the window, under the same
instrument restriction.

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
/ `POD_UNHEALTHY` / `POD_MOUNT_FAILED` / `POD_STARTED`, with `expId: null`
and the k8s `reason` in `flavor`). Unlike dataId-keyed events, these are kept whenever they fall
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
  "instrument": "lsstcam",          // always: both views pin LSSTCam
  "dayObs": 20260521,
  "view": "aos",                    // "aos" | "sfm" — which half this is
                                    // (see POST /api/fetch-night below)
  "startTime": "2026-05-21T12:00:00+00:00",
  "endTime":   "2026-05-22T12:00:00+00:00",
  "cacheDir":  ".../yagan/rapid-analysis/<window>/pods=__aos__",  // sfm: the <window> itself
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
  "instrument": "lsstcam",          // the run's instrument: all in-range ids
                                    // resolved against this one table
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
  "podDetailQuery": "rangeStart=2026051900722&rangeStop=2026051900750&dataId=2026051900725&instrument=lsstcam",
  // ...all the exposure-payload fields (pods, podsAll, taskColors, ...)
}
```

`podDetailQuery` is what the explore renderer appends to `/api/pod/<pod>`
so pod-detail lookups route back through the range state (and anchor
their offsets at this dataId's shutter close). It carries the range's
`instrument` so the pod lookup runs under the same pin the summary was
served under.

### `GET /api/pod/<podName>?dataId=<int>[&instrument=<name>]` / `?dayObs=<int>[&nightView=]` / `?rangeStart=&rangeStop=&dataId=[&instrument=<name>]`

Returns every parsed `LogLine` from that pod's JSONL file. The query
string routes to the right loaded state (`dataId` → exposure, `dayObs`
→ night, `rangeStart`+`rangeStop`+`dataId` → that dataId within a loaded
range, with offsets anchored at its shutter close). 400 if no key, 404
if the targeted state isn't loaded.

`instrument` (the explore view always sends its own, on both the dataId
and the range form) is the same guard `/api/summary` applies: the bare
id's — or the bare span's — in-memory slot may hold the *other*
instrument's exposures, another tab having opened the twin, out of a
different window entirely. A mismatch is a 404, never the loaded
state's lines; the next summary reload re-pins. 400 for an unknown
instrument name.

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

### `GET /api/night/traceback/<bodyKey>?dayObs=<int>[&nightView=aos|sfm]`

Drilldown for a single failure row. Returns the pod's log lines
spanning the dataId's full processing block when the traceback's expId
is carryover-attributable, or otherwise a window around the traceback
itself — deliberately lopsided, `_TB_NO_EXPID_LOOKBACK_S` (30 s) before
and `_TB_NO_EXPID_LOOKAHEAD_S` (5 s) after, because what explains a
traceback is what led up to it. `contextSource` is `"dataId-block"` or
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

### `GET /api/exposure-time/<dataId>[?instrument=<name>]`

dataId → curated ConsDB exposure record, used by the home form to
resolve a user-typed dataId (and show its properties) before kicking
off the fetch. Always resolved against the site this server serves; a
`site` query param is ignored if present.

`instrument` is the one thing here the caller *does* get to name, and
the reason is the distinction that runs through this whole API: `site`
is server configuration (which observatory this deployment explains),
whereas the instrument is part of the identity of the thing being asked
about. Omit it and the answer is the probe-order one — `lsstcam` first,
which is what every caller got before the parameter existed. Supply it
and only that instrument's table is queried, and only that instrument's
cache key can satisfy the lookup: falling back to the bare key would
hand back a *different exposure* that happens to share the id.

- 200 with `{"dataId", "tZero", "scale": "TAI", "fromCache": bool,
  "manual": bool, "site", "instrument", "exposure": {<curated record>}}`.
  `tZero` is the record's `obs_end`; `exposure` carries the rest (filter,
  exp time, image type, program, reason, group/index, …) for the
  explore-view info box. `manual: true` flags a hand-entered stand-in
  (see below) rather than a ConsDB value.
- 400 `"Unknown instrument '<x>'; known: [...]"` — not one of
  `INSTRUMENTS_BY_PROBE_ORDER`.
- 404 `"No exposure-time record for dataId=N"` — every instrument
  table searched, no row with an `obs_end` anywhere. With an
  `?instrument=`, `"... for dataId=N on <instrument>"` — only that one
  was searched.
- 502 `"ConsDB query failed: ..."` — typed ConsDB error (5xx, etc.).
- 503 `"ConsDB token file for site '<name>' not found at <path>. Get a
  token from the relevant RSP and drop it there."` — token missing.
- 503 `"Could not read ConsDB token file: ..."` — the file is there but
  unreadable.
- 503 `"ConsDB token file is empty: <path>"` — token file present but
  blank.
- 503 `"Could not reach ConsDB: ..."` — the request never got an
  answer (DNS, connection refused, timeout).

There is no site-related error, because there is no site parameter to
get wrong: one process serves one site, and a `?site=` is ignored.

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

### `GET /api/site`

Return the one site this server serves. Read-only: the UI renders it as a
badge in the top bar so nobody misreads summit data as BTS data. The
token-file path is *not* echoed — it's a server-side detail.

```jsonc
{
  "name": "bts",
  "cluster": "manke",
  "namespace": "rapid-analysis",
  "lokiAddr": "https://loki-query.ls.lsst.org",
  "consdbUrl": "http://consdb-pq.consdb.svc.cluster.local:8080/consdb/query"
}
```

### `GET /api/live`

Snapshot of the live night poller (see *Live mode*). `{"enabled":
false}` when live mode is off — the one unconditional call the home
page makes to decide whether to render the Tonight panel. When on:

```jsonc
{
  "enabled": true, "siteName": "summit", "dayObs": 20260711,
  "nightStart": "2026-07-11T12:00:00.000000Z",
  "nightEnd":   "2026-07-12T12:00:00.000000Z",
  "watermark":  "2026-07-12T03:14:00.000000Z",  // all logs ≤ this are on disk
  "updatedAt":  "...", "finalised": false,
  "catchingUp": false,                // true while a fresh deployment backfills
  "pollSeconds": 180.0, "lagSeconds": 60.0, "windowAfterSeconds": 300.0,
  "nPods": 576, "totalBytes": 9876543210, "totalLines": 24681357,
  "errors": {},                       // cumulative per-pod hard fetch failures
  "incompletePods": {},               // cumulative unreconcilable chunks
  "eventsError": null,
  "eventsUnfiled": 0,                 // cumulative k8s/events lines that carried no
                                      // `name` label and so could not be filed under
                                      // a pod; expected to stay 0 (see Live mode)
  "consdbError": null,
  "orphanError": null,                // an earlier night couldn't be finalised
  "lastTick": { "startedAt": "...", "elapsedS": 4.2,
                "newLines": 73200, "activePods": 431 },
  "lastError": null,                  // traceback if the last cycle blew up
  "exposures": [                      // newest first, whole night so far
    { "dataId": 2026071100542,
      "instrument": "lsstcam",        // part of the id's identity, not a property
      "obsEndUtc": "2026-07-12T03:10:12.500000+00:00",
      "readyAtUtc": "2026-07-12T03:15:12.500000+00:00",
      "ready": false,                 // readyAt vs. watermark
      "record": { "img_type": "science", "physical_filter": "r_03", ... } },
    ...
  ]
}
```

One row per **(instrument, dataId)**, newest first. The same 13-digit id
appears once per instrument that took an exposure with that sequence
number, which on a night where LSSTCam and LATISS both observe is most
of them — see *dataId / expId* in Key Concepts.

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
      "rangeInstrument": null,                       // and the instrument it recorded, so
                                                     // the row's link reopens this run and
                                                     // not the other instrument's twin span
      "exposures": [                                 // (exposure caches) the exposures that
        {"dataId": 2026051900722,                    // triggered fetches landing here, each
         "instrument": "lsstcam"}, ...               // with the pin it was fetched under so
      ],                                             // the row links to this run, not the twin
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

Every `_live.json` under the target is unlinked *before* the tree goes
(`_dropLiveSidecars`), and only then is `shutil.rmtree` called — with
one `ignore_errors` retry at the root, since the poller may legitimately
regrow a file into a directory mid-delete. The ordering is what matters:
a partial delete that took the pod files but left the sidecar would pass
the poller's intactness check, so it would resume appending to files
that now start mid-night, and every slice cut from them would be short
while claiming to be complete. Sidecar-first makes a partial delete
indistinguishable from a complete one — no sidecar, so the poller opens
the night afresh.

### `DELETE /api/cache/<cluster>/<namespace>/<slug>[/<pods=…>]`

Remove one cached window. The optional 4th segment targets the
nested night-mode `pods=<regex-slug>` subdir; it must start with
`pods=`. Each component is validated against `[A-Za-z0-9._=-]+` so
the URL can't escape `cache_root()`. If any loaded state's `cacheDir`
matches the directory being deleted, that state is evicted first
(the UI gets booted back to the home view on next summary fetch), and
any `_live.json` beneath it is unlinked before the tree, for the reason
above. Empty per-cluster / per-namespace parent directories are pruned.
Returns the same shape as `GET /api/cache`. 404 on a path mismatch.

### `POST /api/fetch`  (exposure)

Request body — only what is being asked, never how to ask it:

```jsonc
{
  "exposureId": 2026051900722,         // required, integer
  "tZero":      "2026-05-20T08:46:16.267",  // required, ISO-8601
  "instrument": "lsstcam",             // optional; default "lsstcam". Part of the
                                       // exposure's identity: pins the info-box
                                       // lookup, stamps the resulting state, and
                                       // scopes pod attribution. 400 if unknown.
  "tZeroUtc":   false,                 // optional; default false (treat as TAI)
  "tZeroManual": false,                // optional; true ⇒ tZero was hand-entered
  "windowBefore": 5.0, "windowAfter": 300.0   // optional; default from the environment
}
```

`site`, `username`, `password`, `workers`, `cluster`, `namespace` and
`lokiAddr` are **not** accepted. They are server configuration read from
the environment, and a body carrying them is ignored rather than
honoured. Two reasons, and the first is the serious one: the same
13-digit dataId exists on both BTS and the summit with different
`obs_end` values, so a server that could be talked into answering for the
other observatory would return results that looked plausible rather than
obviously wrong. And `LOKI_PASSWORD` is process-global, so one visitor's
mistyped password would break fetches for everyone sharing the
deployment.

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
  "dayObs": 20260521,                  // required, YYYYMMDD integer
  "view": "aos"                        // optional: "aos" (default) | "sfm"
}
```

Same rule as `/api/fetch`: nothing about *how* to reach Loki is accepted
from the body. An unknown `view` is a 400.

The window is the full 24-hour dayObs (noon UTC → noon UTC) either way.
Same response shape as `/api/fetch`, plus `nightView` on the status and
`done` payloads — a dayObs alone no longer names one loaded state, and
the client asks `/api/summary` for the finished night by name the moment
`done` lands.

#### The two night views

The night is served in two halves, which partition its pods:

| view | pods | how it is fetched |
|---|---|---|
| `aos` (default) | pod name contains `aos` | `pod=~".*aos.*"` pushed down to Loki; lands in the nested `pods=<slug>/` cache dir |
| `sfm` | everything else — SFM workers, head node, plotters, one-offs, redis … | **no** pod filter; the night is fetched whole and partitioned after parsing, so it lands in the window dir itself |

They are fetched differently because they have to be. LogQL uses RE2,
which has no negative lookahead, so there is no `pod=~"not aos"` to push
down. Enumerating the SFM-side names positively was the alternative and
is worse: "misc" is defined by exclusion, so a pod type nobody had
thought of would fall out of both halves silently. Fetching the night
whole costs a wider fetch and buys the guarantee that every pod is in
exactly one view — `parse.isAosPod` is pinned against
`NIGHT_AOS_POD_REGEX` by a test for exactly that reason.

Two consequences worth knowing:

- The SFM half is **much bigger**. On a summit night it is ~9 GiB and
  ~35M lines against the AOS half's few hundred MB, so it is a minutes-
  long fetch and parse where the AOS half is seconds. On a live instance
  it is usually free, because the poller already holds the whole night
  on disk and the request is served by handing that window over.
- On a night that is still in progress, the SFM half can be *fetched*
  but not *rebuilt from cache* on a reload: the live night dir has no
  `_meta.json` until the noon rollover finalises it, and that file is
  what the cache walkers use to decide a window is complete. Reloading
  such a tab lands on home with the form prefilled. Finalised nights —
  every past night — rebuild normally.

### `POST /api/fetch-range`  (start/stop)

```jsonc
{
  "rangeStart": 2026051900722,         // required, integer
  "rangeStop":  2026051900750,         // required, integer; > rangeStart
  "tZeroStart": "2026-05-20T08:46:16.267",  // required, ISO-8601 (start shutter close)
  "tZeroStop":  "2026-05-20T08:51:09.512",  // required, ISO-8601 (stop shutter close)
  "instrument": "lsstcam",             // optional; default "lsstcam". A range is a
                                       // run of ONE instrument's exposures; the
                                       // server-side shutter-close batch resolves
                                       // against that table only. 400 if unknown.
  "tZeroUtc":   false,                 // optional; default false (treat both as TAI)
  "windowBefore": 5.0, "windowAfter": 300.0   // optional; default from the environment
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
  "instrument": "lsstcam",             // the pin this fetch ran under
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
- `done`: `{ kind, expId, instrument, tZero, dayObs, startId, stopId,
  cacheDir, cacheReuse, podCount, totalBytes, elapsedS }` — after the
  server's keyed `ServerState` / `NightState` / `RangeState` slot has
  been populated (`startId`/`stopId` set for range jobs). **Always**
  fired after `onComplete` so SSE consumers can rely on the summary
  being ready when they see `done`. `instrument` is the pin the fetch
  ran under, and the client needs it rather than the topbar's current
  value: the user may have flipped the switch while the fetch ran, and
  by then the bare expId's slot may hold the other instrument's
  exposure.
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

`fetch.fetchAll` additionally takes a **per-window write lock**
(`fetch.windowWriteLock`, one re-entrant lock per requested window
directory) for the whole of its decision-and-write path. A cache window
is a directory of files with a `_meta.json` vouching for them, and two
threads asking for the same window would both find no cache and then
both write the same `pods/<pod>.jsonl` — interleaving bytes and clearing
each other's `.partial`. Two people clicking the same ready exposure in
the Tonight panel is enough. The loser simply waits and then takes the
winner's result as an ordinary cache hit, so the wait is never longer
than the fetch it would have duplicated (and its SSE progress stream
just stays quiet until then).

When live mode is on, one more daemon thread runs for the life of the
process: the `LiveNightManager` poller. It deliberately stays outside
the `stateLock` world — it mutates only its own night dir and
`_live.json` sidecar (single writer, atomic replace, only after the
tick's bytes are on disk), and its one cross-thread surface is
`snapshot()`, an immutable dict swapped under the manager's own lock
that `/api/live` returns without copying. Request threads meet the
poller's output purely through the filesystem: the night-slice path in
`fetchAll` reads the sidecar and byte ranges the sidecar vouches for,
so a slice can run concurrently with an *append* and never see a torn
line. Nothing else may write the *files the sidecar vouches for* —
`fetchAll` refuses to fetch into a live night dir, and serves the
night's own window by handing over the directory rather than copying it
onto itself.

One thing does write *inside* it, harmlessly: a night-mode request for
the night's own span materialises its AOS slice at
`<night dir>/pods=<slug>`, because that is where `windowCachePath` puts
a filtered view of that window. It is an ordinary cache dir that the
poller never looks at — recovery and re-open touch only `pods/` and
`pods_events/`, the listing and LRU eviction treat it as the separate
window it is, and parent pruning stops at the non-empty night dir. The
invariant that matters is about the pod files, not the directory.

The sidecar's byte counts are enough for appends because an append only
ever extends a file. One operation is not an append: end-of-night
verification replaces a whole pod file
(`live._refetchWholePod`), and during it the pod's recorded length
describes a file that is about to stop existing. So that operation
announces itself — `rewritingPod: "<pod>"` on the sidecar — and
`_tryNightSlice` declines to slice a night carrying it, falling back to
a real fetch until the swap completes. The flag is cleared even when the
refetch fails, since the pod's record then honestly reads zero and the
caller has recorded the error: slicing resumes against a night that is
*visibly* short rather than silently so.

The poller also notices when the directory disappears underneath it —
`DELETE /api/cache` is one button on the home page — and re-opens the
night on the next tick rather than failing every tick until the noon
rollover.

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

## Startup modes

Deployed, the container runs home mode with no eager fetch: `run --host
0.0.0.0 --port 8080 --no-browser`, with everything else supplied as
environment variables — including `RA_LOG_EXPLORER_LIVE_POLL_S`, which
starts the live poller alongside the server (see *Live mode*). The
other two modes exist for development and scripting on a laptop.

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

All three share `server.py`, `jobs.py`, the JSON API surface, and the
SPA. Only the first is used in the deployment.

## Non-goals

- Streaming / live tailing of logs. Snapshot-based; one window per
  fetch. Live mode does not change this: it is a *poller* that extends
  an on-disk snapshot every few minutes, not a `--tail` stream, and
  every view is still served from a bounded window of what is already
  on disk.
- Cross-night aggregation or trending. One dayObs at a time in
  night mode; one exposure at a time in exposure mode.
- Authentication *of its own*. Deployed, the app sits behind a
  GafaelfawrIngress with `loginRedirect: true` and a `read:image` scope,
  so it never sees an unauthenticated request and has no login code
  itself. Run from a laptop it binds `127.0.0.1` with no auth at all,
  which is one of the reasons that mode is for development only.
- Per-user state or preferences. One process serves everyone who opens
  it, and its configuration is the deployment's — there is deliberately
  nothing a visitor can set that another visitor would notice. Loaded
  exposures and the on-disk cache are shared, which is a feature: the
  expensive fetch someone else already did is one you don't repeat.
