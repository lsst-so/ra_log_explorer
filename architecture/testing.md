# Testing

The project has three test layers:

1. **Unit tests** under [tests/](../tests/) — pure-Python, no network
   or persistent filesystem state. Each test gets a per-test cache
   root via the `tmpCacheRoot` fixture so on-disk side effects don't
   leak between tests. Run with `pytest`.
2. **Container smoke test** — building the image and exercising it under
   the conditions the deployment imposes (read-only root filesystem, a
   base path, configuration only from the environment). This is the one
   that covers *how the tool is actually run*, and nothing in the unit
   suite substitutes for it. See below.
3. **End-to-end smoke test** — running against the real Loki cluster
   (and ConsDB for the shutter-close lookup) for a known dataId or
   dayObs, by hand, before declaring fetch-path work done.

Layer 2 matters because the deployed service is the product: the tool
runs as the Phalanx `log-explorer` application on BTS and the summit, and
a laptop run is a development convenience. A change that works locally
and breaks in the container has broken the only mode anyone uses.

Pre-commit, `mypy`, `mypy-coverage`, and the unit tests together are
the supported validation loop; see the
[ra-log-explorer-validation](../.claude/skills/ra-log-explorer-validation/SKILL.md)
skill for the exact commands.

Two GitHub Actions workflows under
[.github/workflows/](../.github/workflows/) run on every PR and on
pushes to `main`:

- **`ci.yaml`** — two jobs:
    - `pytest`: setup-python 3.13 → `pip install -e .` + pytest /
      pytest-cov → runs the suite with
      `--cov=ra_log_explorer --cov-fail-under=85`. Current coverage is
      ~93%, so the floor leaves ~8% of headroom before CI fails.
      Coverage table piped into the run's `$GITHUB_STEP_SUMMARY`.
    - `mypy`: same setup → bare `mypy` (picks up `mypy.ini`'s
      `files = ra_log_explorer/, tests/` automatically). Pytest is
      installed in this job too — without it mypy can't resolve the
      `import pytest` in the test files.
- **`mypy-coverage.yaml`** — runs `mfisherlevine/mypy_coverage` to
  drop inline body-coverage annotations on the PR diff and post a
  sticky markdown summary as a PR comment. Informational only
  today (the package + tests are both at 100% mypy body-coverage so
  any regression is already a deliberate change worth blocking on,
  but we haven't wired a threshold gate yet).

## Unit-test scope

The unit tests target the deterministic pieces of the codebase:

| Area               | What's tested                                                              | File                              |
|--------------------|----------------------------------------------------------------------------|------------------------------------|
| Log line parsing   | `parseLogLine` against the rapid-analysis Python log format and fallbacks (Z suffix, naive UTC, explicit offset, nano-precision trim, label-level priority, malformed JSON); `_normalizeLevel` warn/error alias buckets | `tests/test_parse.py`             |
| Event classification | `classify` for every kind in [parsing.md](parsing.md), including `HEAD_INCOMING`, the `WORKER_REPORT_FAILED` variant, and the calibrate-quantum visit→expId fallback | `tests/test_parse.py`             |
| Pod classification | `podGroup` (longest-prefix-match, order-independence regression, full real-pod fixture parametrisation), `podOrdinal`, `podInstrument`, `groupLabels` (defensive-copy contract) | `tests/test_parse.py`             |
| Per-pod summary    | `summarizePod` against JSONL fixtures, including carryover (worker vs head), traceback capture (single, multi, chained, back-to-back, truncated body, no-class-line split into `<unclassified>` vs `<truncated>`, blank-line termination), per-dataId first/last/wait stats; `tagLinesWithExpId` empty-input edge case; `podsTouchingExp` no-match base case | `tests/test_parse.py`             |
| Cache paths        | `windowCachePath` determinism + slug-cleaning + per-`podRegex` nesting; `ensureWindowCacheDir` is the only I/O side; `NIGHT_AOS_POD_REGEX` constant pin; `dayObsStartUtc`/`dayObsEndUtc` (UTC-12 rollover + 24 h invariant + year-boundary alignment) | `tests/test_config.py`            |
| Cache reuse        | `findSupersetCache` exact / superset / no-meta / partial-flag / smallest-wins / cross-mode isolation / filtered-to-filtered nesting | `tests/test_fetch.py`             |
| Cache eviction     | `evictToFit` over flat exposure caches, nested night caches, exempt-set respect, unviewed-as-oldest, empty-parent-dir pruning (both layouts) | `tests/test_fetch.py`             |
| Cache sidecars     | `addExposureToCache` / `getCacheExposureIds`, `markCacheViewed` / `getCacheLastViewed` round-trips and best-effort no-ops | `tests/test_fetch.py`             |
| Misc helpers       | `humanBytes`, `fetch._parseIso` (timezone handling)                        | `tests/test_fetch.py`             |
| `logcli` wrapping  | `_run_logcli` cmd construction, missing-binary / failed-RC / timeout error paths, `LOKI_PASSWORD` requirement, `_matcher` (default vs pod-pinned vs podRegex) | `tests/test_fetch.py`             |
| fetchAll happy path | listPods + per-pod fetch with `_fetchOnePod` mocked; per-pod hard-error capture; soft `incomplete_pods` capture (unreconciled chunk, no exception); `pod_lines` / `pod_expected` in meta; `fetchComplete` iff both fall-short maps empty; progress-callback firing; exact + superset cache hits; refetch when window is in the future / schema outdated; .partial flag while running | `tests/test_fetch.py`             |
| Chunked fetch (#17270) | `_countOverTime` instant-query construction (ms range, `--now`) + None-on-error; `_parseCountOutput` for the pretty-printed JSON array, line-by-line fallback, per-stream sum, garbage→None; `_queryWindowToFile` `--batch`==cap + line count; `_fetchOnePod` single-shot-under-cap, split-loses-nothing (presized), blind-bisect when oracle dark, empty-window-skips-query, floor→`incomplete` flag. Cluster calls modelled by an in-memory `_FakeLoki` stream with `SERVER_QUERY_CAP` shrunk | `tests/test_fetch.py`             |
| Schema-version flush | `ensureCacheSchemaCurrent` no-op on empty/current cache (+ sentinel write), flush on sentinel mismatch, flush when sentinel absent (legacy cache), current-schema cache survives | `tests/test_fetch.py`             |
| Task palette       | `_assignTaskColors` collision-freeness up to palette size + on real pipeline labels, stable across input reorders, pinned tasks honoured | `tests/test_server.py`            |
| JSON serialisation | `_toJsonable` for `datetime`, `set`, dataclass, `Path`, nested containers | `tests/test_server.py`            |
| `_summaryToDict`   | Target-expId filter, untagged-WARN windowing, per-pod summary stats (start/duration/QG-build/wait/looksTruncatedEnd), truncation flag scoped to worker groups | `tests/test_server.py`            |
| `_buildSummaryPayload` | Head-define-visit reference point derivation, taskColors collision-freeness, `other`-group surfacing, `groupLabels` shipped to UI | `tests/test_server.py`            |
| ServerContext      | Keyed `put`/`get`/LRU eviction for both exposure and night states; `evictByCacheDir` drops only matching entries (and is a no-op when nothing matches); two states coexist | `tests/test_server.py`            |
| Night helpers      | `_taiIsoToUtc`, `_buildNightPayload` (histograms + stats + failures), `_podDetailForNight` (offset from night-start), `_tracebackContextForNight` None on unknown key | `tests/test_server.py`            |
| Configuration      | `_envInt` / `_envFloat` (default when unset, empty means unset, `ConfigError` on garbage rather than a silent fallback); the module constants actually reading their variables (via `importlib.reload`); the defaults adding up to a working local run with no environment at all; `normalizeBasePath` canonicalisation; `cache_root` ignoring a settings file left over from an older version | `tests/test_config.py`            |
| Deployment contract | The two places this repo hands a promise to the Phalanx chart, which lives in *another repository* and would otherwise only break at deploy time: the **set of environment variable names** (adding one without adding it to the chart means production silently runs on the default), and the **Dockerfile invariants** the chart hard-codes — port 8080, `--host 0.0.0.0`, UID 1000, a pinned checksum-verified logcli, and no baked-in `ENV`. Also the exact `sites.toml` the chart's ConfigMap renders, parsed through `loadSites` | `tests/test_config.py`, `tests/test_image_contract.py`, `tests/test_sites.py` |
| Server-side helpers | `_resolveCacheWindow` path-component allowlist + `pods=`-prefix gate; `_buildNightSpecFromRequest` happy path + every validation error + that a body-supplied site / credentials / worker count are ignored; `_parseClientIso` + `_isoForLogcli` parsing / UTC conversion | `tests/test_server.py`            |
| `night.py` rollups | `computeTopStats`, `errorsByType` (sort + sample-message), `errorsByPod`, `firstTaskStartByDataId` (cross-pod min, None-expId skip), `calcZernikesEndByDataId` (substring match, ignores non-DONE), `buildHistogram` (binning, drops, dataId attribution, single-value, parallel-list validation), `computeDeltaShutterOffsets`, `failureRows` (offsetS / sort / unique bodyKey), `tracebackBody` round-trip, `gatherOnlyDataIds` (step1b-without-step1a flag, pipeline pairing, no-gather and all-paired empty cases); all rollups exercise their empty-input degenerate paths | `tests/test_night.py`             |
| Instrument identity | An exposure id is unique only within an instrument, so: records are stamped with the `cdb_<instrument>` table they were read from; `queryExposureRecordsForDayObs` asks every instrument and returns a list, keeping both halves of a colliding id; `probeOrderWinners` decides the bare-id answer the same way `queryExposureRecord` does; `storeCachedRecordList` writes per-instrument keys plus the probe-order winner under the bare key; an instrument-scoped lookup never falls back to the bare key. Endpoint side: `/api/exposure-time/<id>?instrument=` queries only that table, echoes the instrument, caches without clobbering the bare key, and 400s an unknown instrument | `tests/test_exposure_times.py`, `tests/test_server_endpoints.py` |
| Exposure-time lookup | `queryIsot` happy path, instrument fallthrough, no-row 404, 500 propagation, 500-UndefinedTable fallthrough, missing obs_end column. `queryIsotBatch` single-instrument-hit, multi-instrument fallthrough, chunking, UndefinedTable, malformed rows, missing-column-skip, empty-input short-circuit. `rspTokenFilePath` env-var + explicit-override + tilde-expand. `readRspToken` strip + missing file. `lookupCached` / `storeCached` round-trip + corrupt-recovery + non-string-value. `_sqlFor` wire format. `_postQuery` 400-as-empty + 503-as-error. | `tests/test_exposure_times.py` |
| Job manager        | `FetchJob` event ordering, status transitions (pending→running→parsing→done), error path captures terminal `error` event, `onComplete` fires before `done` (verified by snapshotting `len(events)` from inside the callback), `startJob` runs in background, condvar wake. `createNightJob` distinct shape. `runJob` populates `cacheDir`/`meta`. `stateLock` is a real Lock (not RLock). | `tests/test_jobs.py`              |
| HTTP endpoints     | Spins up the real server on an ephemeral port and hits it with `http.client`. Covers: `/api/summary` (empty / by-dataId / by-dayObs / 400-on-bad-int / mode-discriminator / LRU touch on hit), `/api/cache` (lists exposure + night, partial skipping, sidecar fields surfaced), `/api/fetch` + `/api/fetch-night` (body validation, 202 + status polling to done, NightState populated, podRegex on night spec), `/api/pod` (400 no key, 404 not loaded, valid-name allowlist), `/api/exposure-time/<>` (200 / 404 / 503-no-token / 503-empty-token / cache short-circuit / cache write / a `?site=` query param ignored), `/api/night/traceback/<key>` (dataId-block context, time-window fallback, 404 unknown bodyKey, 400 bad-int dayObs), `DELETE /api/cache` (all + single + path-traversal-rejection + state-cleared-when-matching), `/api/site` (the served site, no token path echoed) and `/api/sites` gone, the base-path routing (probe + API + static assets + SSE under the prefix, 404 outside it and for partial prefixes, multi-segment prefixes, query strings surviving the strip, index substitution, window fields tracking the configured defaults, no configuration fields in the HTML), `/healthz` still answering while a fetch worker is parked mid-`fetchAll`, SSE `/api/fetch/<id>/progress` (history replay + terminal close + 404), `_buildSpecFromRequest` (TAI/UTC, body-supplied site / credentials ignored, validation errors), `_prefetchNightShutterCloses` (no-token, consdb-error, short-circuit-when-empty) | `tests/test_server_endpoints.py` |
| CLI parsing        | `_parseIsoUtc` for Z / no-offset / explicit-offset (positive and negative) / microseconds; `_isoForLogcli` Z suffix + UTC conversion; TAI constant pin; subparser arg parsing + `--t-zero-utc` flag; partial-args rejection; eager-fetch TAI→UTC conversion and `--t-zero-utc` opt-out; `--force-refresh` reaches fetchAll; `_warnIfIncompleteFetch` silent-when-clean / shouts on hard `errors` / shouts on soft `incomplete_pods` / caps the list; `cache info` / `cache flush` behaviour (with-yes / decline-prompt / empty-cache-root / night-mode `pods=<slug>` row surfacing) | `tests/test_cli.py`               |
| Live night cache   | `currentDayObs` (noon-UTC rollover, inverse of `dayObsStartUtc`); `firstOffsetAtOrAfter` byte-bisect (exact line start, between lines, before-all, after-all, torn-write-beyond-limit invisibility); `findNightDirCovering` watermark/cluster gating; `materializeNightSlice` (half-open boundary exactness, first-class result that exact-hits on repeat, `podRegex` filtering into the nested `pods=` dir, fall-short-map inheritance scoped to the filter, lifecycle-event slicing by its own byte count and only for pods with app logs, pruning files a failed attempt stranded, `.partial` surviving a failure); `fetchAll` integration (slice served with no `_run_logcli` call, in-progress night's own window clamped to the watermark + exact reuse at an unchanged watermark, arbitrary past-watermark windows falling through to a real fetch, an exact cache beating the slice path, the night's own window handed over rather than sliced into itself, a fresh fetch into a live dir refused, four concurrent `fetchAll`s for one window serialised to one writer); `_sliceFileByTime` refusing `src == dst`; `findSupersetCache` refusing live-built dirs | `tests/test_live.py`              |
| Live poller        | `LiveNightManager.tick` driven with stubbed fetch edges: increments tile across ticks — asserted on the *windows requested*, not just the bytes, since the stub applies half-open semantics itself and would mask an off-by-one; a failed pod pins the global watermark and self-heals by refetching its whole missed span (including the pod-fails-on-first-fetch case); pods absent from the listing advance for free (a dead pod can't pin the night); readiness follows the watermark and persists records to the per-site exposure-time cache; colliding ids from two instruments both appear and cache separately; restart recovery truncates unrecorded (torn) bytes, clears stranded temp files, and migrates legacy event-only pods; noon rollover finalises (`_meta.json`, `liveBuilt`, sidecar `finalised`) and moves to the new night; the verification pass refetches a pod beyond the dedup-slack tolerance, leaves one within it alone, keeps an incompleteness flag its own refetch set, and publishes zero durable bytes before swapping the file; a night left unfinalised by a restart across noon is swept up on a later tick (and a failure to do so is reported, not fatal); a cache wipe under the poller re-opens the night instead of wedging it; `_runLoop` survives a failing tick; `--live-day-obs` pins the night and adopts an already-finalised one without re-fetching | `tests/test_live.py`              |
| k8s/events demux   | The namespace-wide events stream is demuxed by `name` into `pods_events/`, deliberately including non-Pod objects: a new name must not drag the global watermark back to night start, it is filed in `eventPods` with no watermark of its own, it promotes into `pods` (carrying its counters) when it first emits app logs, and `pods.txt` stays the app-log pod list. Failure handling: a failed fetch leaves the events watermark alone without blocking app logs, and a failed *append* rolls every file back so the retry can't duplicate lines. Plus the tolerance the breadth relies on — `summarizeAll` never opens a lifecycle file with no app-log sibling, and `classifyK8sEvent` declines a non-Pod event | `tests/test_live.py`              |

### What we don't unit-test

- The actual `logcli` subprocess invocation — the wrapper is
  thoroughly mocked but a real Loki round-trip only happens during
  the smoke test. CI doesn't have a Loki instance.
- The browser UI itself (including the Tonight panel). We rely on
  hand verification.
- The full end-to-end fetch+UI flow with real Loki traffic. That's
  the smoke test.
- The live poller against real Loki — its fetch edges are stubbed in
  the unit tests; real ticks are exercised by running the container
  with `RA_LOG_EXPLORER_LIVE_POLL_S` set (see the container smoke
  test below) and watching `/api/live`'s watermark advance.

## Fixtures

Sample Loki JSONL lines live under [tests/data/](../tests/data/):

- `head_node_sample.jsonl`  — representative head-node events
  covering `HEAD_DEFINE_VISIT`, `HEAD_ONEOFF`, `HEAD_FANOUT_DONE`,
  `HEAD_PIPELINE_DECIDED`, `HEAD_FANOUT_START`, `HEAD_LOOP_SLOW`,
  `HEAD_POSTISR_MOSAIC`, `HEAD_GATHER_DISPATCH`,
  `HEAD_VISITIMAGE_MOSAIC`, and one `WARN`. `HEAD_INCOMING` is
  exercised by a synthetic `LogLine` in `test_classify_head_incoming_extracts_expId`
  rather than the fixture.
- `sfm_worker_sample.jsonl` — an SFM worker doing one full step1a
  (pickup → QG build → ISR quantum → calibrateImage quantum → binned
  visit image written).
- `aos_worker_sample.jsonl` — the longer AOS quantum chain (ISR
  plus the donut tasks).
- `traceback_sample.jsonl` — a small synthetic traceback so the
  detail-drawer logic has something to grab.

These are hand-trimmed from real cluster logs to keep the fixtures
small and to make the test assertions specific. When you extend the
parser to recognise a new event kind, add the minimal triggering
line to the relevant fixture and a test assertion that pins the
parsed event.

## Running the suite

From the repo root, with the venv active or its tools on `$PATH`:

```bash
.venv/bin/pytest -q
.venv/bin/mypy
.venv/bin/mypy-coverage     # body-coverage target: 100%
.venv/bin/pre-commit run --all-files
```

The runtime itself is stdlib-only; pytest / mypy / black / isort /
flake8 are dev-only dependencies that live in the venv.

## End-to-end smoke test

Local, against real Loki. Do this for fetch-path work; the container
smoke test above is the one that covers deployment shape.

```bash
export LOKI_PASSWORD=...
# RSP token in ~/.lsst/log-browser-token.txt
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267 \
    --no-browser
```

Then `curl http://127.0.0.1:8780/api/summary?dataId=2026051900722`
and sanity-check:

- `referencePoints` includes both stable refs (shutter close,
  head-node first defined visit).
- `taskColors` has more unique values than tasks (i.e. no
  collisions).
- `len(pods)` is roughly what you expect for the exposure (~210 for
  a full LSSTCam science visit).
- Pod groups break down sensibly (1 head, ~189 sfm, 8 aos, …).
- `groupLabels` is shipped (`{"sfm": "sfm-runner", …}`).

For night mode, point a browser at the home page and click
"investigate night" with a known full-coverage dayObs; verify:

- `stats.nDataIdsWithTraceback` is non-negative and matches what
  you'd find with `grep -c 'Traceback' …`.
- `histograms.firstTaskStart.nValues` is close to the number of
  exposures that night.
- Clicking a failure row's drilldown returns the dataId's full
  processing block (or a fallback time window when carryover
  couldn't attribute it).
- No red banners: the incomplete-fetch banner (`meta.errors` /
  `meta.incomplete_pods` both empty) and the gather-only banner
  (`gatherOnly` empty) — a populated gather-only banner means step1a
  logs were dropped for those dataIds.

**Verifying fetch completeness** (the #17270 fix). The fetch is lossless
by construction (each chunk trusted only when `got < --batch`), but to
confirm against the cluster, compare a busy pod's cached line count to the
`count_over_time` oracle, which is server-side and immune to the bug:

```bash
# Lines we actually cached for one pod over the night window:
wc -l <cache>/.../pods=__aos__/pods/<busy-aos-pod>.jsonl
# What Loki says was there (ms range; -o jsonl is a pretty-printed array):
logcli --username=… --addr=… --quiet instant-query \
  'sum(count_over_time({cluster="yagan",namespace="rapid-analysis",pod="<busy-aos-pod>"}[86400000ms]))' \
  --now=<toIso> -o jsonl
```

They should agree to ~0.03% — the oracle runs a stable *hair high*
against a byte-perfect fetch because metric queries count duplicate
entries in overlapping storage chunks that the log path deduplicates
(verified on 20260711: refetching a "short" pod reproduces identical
bytes). A gap of percent-scale means a regression — that's exactly the
symptom #17270 produced before the chunker. (Live 2026-06-05, a 2 h busy
window: chunker 56212 vs oracle 56227; the old `--limit=0` got 54091.)
The live poller's finalisation audit codifies this as
`live.VERIFY_TOLERANCE_*`.

The first run for a given window takes 60–90 s (exposure) to
several minutes (night). A second run for the same — or any
overlapping — window is instant thanks to superset reuse.

## Container smoke test

The deployed form of the tool is a container image (see the
[Dockerfile](../Dockerfile) and the *Running as a deployed service*
section of the [README](../README.md)). Nothing in the unit suite
exercises the image, so build and poke it by hand before shipping a
change that touches the Dockerfile, the base path, or anything the
deployment configures through the environment:

```bash
docker build -t ra-log-explorer:local .

# Run it the way the deployment does: read-only root filesystem, every
# writable path a mounted volume, and a one-site catalog with no ConsDB
# token. If it works here it will work in the pod.
docker run --rm --read-only \
  --tmpfs /tmp --tmpfs /var/cache/ra-log-explorer \
  -v "$PWD/sites:/etc/ra-log-explorer:ro" -p 8080:8080 \
  -e RA_LOG_EXPLORER_BASE_PATH=/log-explorer \
  -e RA_LOG_EXPLORER_CACHE=/var/cache/ra-log-explorer \
  -e RA_LOG_EXPLORER_SITES_FILE=/etc/ra-log-explorer/sites.toml \
  -e LOKI_USERNAME=omega \
  ra-log-explorer:local
```

Worth checking, in this order — each one has failed for real:

- `curl localhost:8080/log-explorer/healthz` → `{"status": "ok"}`.
  This is the readiness probe; if it 404s the pod never joins the
  Service and the deployment wedges.
- `curl localhost:8080/` → 404. Paths outside the base path belong to
  other apps on the same hostname.
- `curl localhost:8080/log-explorer/ | grep static` → every asset URL
  carries the prefix and no `__BASE_PATH__` survives. A leftover
  placeholder is a blank page in the browser.
- `GET /log-explorer/api/cache` reports the mounted cache root — proves
  the app can write there despite the read-only root filesystem.
- The window fields in the served HTML carry whatever
  `RA_LOG_EXPLORER_WINDOW_*_S` was set to, and no `name="password"` /
  `name="workers"` field exists at all. A malformed numeric env var
  (`RA_LOG_EXPLORER_WORKERS=eight`) must stop the container with a
  `ConfigError` rather than start it on the default.
- `docker exec … logcli --version` reports the pinned version, and a
  query against the real Loki fails with a `401` rather than a TLS
  error — the latter would mean the image has no CA bundle.
- `GET /log-explorer/api/live` → `{"enabled": false}` when
  `RA_LOG_EXPLORER_LIVE_POLL_S` is unset. For live-path work, re-run
  with it set (plus a real `LOKI_PASSWORD`) and watch the snapshot: the
  watermark should advance by one poll interval per tick, `catchingUp`
  should clear once the backfill lands, and a quiet namespace should
  advance cleanly with zero pods rather than error.
