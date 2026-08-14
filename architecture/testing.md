# Testing

The project has four test layers:

1. **Unit tests** under [tests/](../tests/) — pure-Python, no network
   or persistent filesystem state. Any test that touches the cache
   asks for the `tmpCacheRoot` fixture, which points `cache_root()` at
   a per-test directory so on-disk side effects don't leak between
   tests. Run with `pytest`.
2. **Browser tests** under [tests/ui/](../tests/ui/) — Playwright
   driving Chromium against the real server, the real cache on disk, and
   a real (cut-down) night of captured logs. They cover the UI *and* the
   integration behind it: a click goes through the actual HTTP handler,
   parser and renderer, so an assertion about what is on screen is an
   assertion about the whole stack. Run by `pytest` like everything
   else. See *Browser tests* below.
3. **Container smoke test** — building the image and exercising it under
   the conditions the deployment imposes (read-only root filesystem, a
   base path, configuration only from the environment). This is the one
   that covers *how the tool is actually run*, and nothing in the unit
   suite substitutes for it. See below.
4. **End-to-end smoke test** — running against the real Loki cluster
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

Three GitHub Actions workflows live under
[.github/workflows/](../.github/workflows/):

- **`ci.yaml`** — on every PR and push to `main`. Two jobs:
    - `pytest`: setup-python 3.13 → `pip install -e ".[ui-test]"` +
      pytest / pytest-cov → `playwright install --with-deps chromium`
      (cached on the Playwright version) → runs the whole suite,
      browser tests included, with `-n auto --cov=ra_log_explorer
      --cov-fail-under=85`. Current coverage is ~93%, so the floor
      leaves ~8% of headroom before CI fails. Coverage table piped into
      the run's `$GITHUB_STEP_SUMMARY`.
    - `mypy`: same setup → bare `mypy` (picks up `mypy.ini`'s
      `files = ra_log_explorer/, tests/` automatically). Pytest is
      installed in this job too — without it mypy can't resolve the
      `import pytest` in the test files.
- **`build.yaml`** — on pushes to `main`, `tickets/**` branches and
  `v*` tags. It gates the image on the same checks before anything is
  pushed: a `checks` job running `mypy` + `pytest -q -n auto` (with the
  `ui-test` extra and Chromium installed — the browser tests fail rather
  than skip without them, so omitting either makes the job error at
  collection instead of gating anything), then the
  container smoke test below run in CI, then a `chart-contract` job
  that clones Phalanx and renders `applications/log-explorer/` through
  `tests/test_phalanx_chart.py` (with `RA_LOG_EXPLORER_REQUIRE_CHART=1`,
  so "no chart found" fails rather than skips — the skip is the exact
  breakage those tests exist to catch). Only if all three pass does the
  `build` job push to GHCR. It repeats CI's work deliberately: CI is
  scoped to PRs and `main`, this fires on every ticket-branch push, and
  a broken commit should cost two minutes rather than an image somebody
  then deploys.
- **`mypy-coverage.yaml`** — runs `mfisherlevine/mypy_coverage` to
  drop inline body-coverage annotations on the PR diff and post a
  sticky markdown summary as a PR comment. Informational only
  today (the package + tests are both at 100% mypy body-coverage so
  any regression is already a deliberate change worth blocking on,
  but we haven't wired a threshold gate yet).

## Browser tests

[tests/ui/](../tests/ui/) drives Chromium at the real application with
[Playwright](https://playwright.dev/python/). They are ordinary pytest
tests, collected and run by a bare `pytest`; the `ui` marker exists only
so they can be deselected (`-m "not ui"`), never so they can be
forgotten.

```sh
pip install -e '.[ui-test]'     # pytest-playwright + pytest-xdist
playwright install chromium     # once per machine
pytest -n auto                  # everything, ~32 s
```

**They fail rather than skip when Playwright or the browser is
missing** — a UI suite that skips itself is indistinguishable from one
that passes, in a terminal and in CI alike. A missing package is an
import error at collection; a missing browser is a single `UsageError`
naming the install command. Both exit non-zero. CI installs both and
runs the whole suite in one job for the same reason.

`-n auto` is worth the habit: measured on an 8-core laptop, the unit
tests take 75 s serially and 31 s in parallel, and adding all ~89
browser tests to the parallel run costs **0.1 s** — they parallelise
almost perfectly, where the unit suite does not. Serially they would
double the suite. (~760 tests all told, ~31 s.)

### What they run against

One archive, [`tests/data/ui/july11.tar.gz`](../tests/data/ui/) (1.8 MB
packed, ~20 MB unpacked), holding a cut-down but **entirely real** night:
dayObs 20260711 on the summit, 32 pods across every pod group and both
instruments, ~93k captured log lines at their real timestamps. Its
[README](../tests/data/ui/README.md) explains how it was cut and how to
rebuild it.

It is laid out as a **live night dir**, which is the load-bearing
choice: every other window a test needs — one exposure's, the AOS
night's, a range's — is produced from it by the application's own
slicing code, so a test that opens an exposure is looking at exactly the
bytes a user would. A ~30-line fake `logcli` reads the same corpus for
the tests that drive a real fetch, deliberately re-implementing the
window filter so a bug in slicing cannot hide behind a fixture that
shares it. One deliberate gap: the fake answers `instant-query` with
nothing, so the count oracle is always dark and browser-driven fetches
exercise only the blind-bisection path of the chunker — the
count-presized path is covered by the unit tests' `_FakeLoki`, not
here.

Being real data pays off in places a synthetic fixture would not reach.
The night's AOS workers fail often enough to populate the failures table
and six distinct exception classes; a pod really does restart; and the
time cut genuinely leaves two visits with step1b activity and no step1a,
so the gather-only banner is tested against the exact condition it
exists to catch rather than an injected flag.

### The three ways a test drives the app

- **End to end** — real server, staged cache, real clicks. The default,
  and what makes these integration tests rather than DOM checks.
- **Stubbed** (`page.route`) — canned `/api/*` payloads for states the
  corpus cannot produce on demand: live mode catching up, a pod's fetch
  failing, a night that could not be finalised, a hostile ConsDB string.
- **In-browser evaluation** (`page.evaluate`) — reading computed
  geometry, which is the only honest way to assert that events land in
  the right place along a track.

### What they cover

| Area | File |
|---|---|
| URL → view routing, the base path (every asset and API call carrying the prefix, paths outside it 404ing, and a whole form → fetch → timeline run staying under the prefix end to end), the FAQ overlay | `test_shell.py` |
| The instrument pin: default, persistence, URL precedence, night mode hidden for LATISS, the *same real dataId* resolving to shutter closes an hour apart on the two instruments, a resolved t₀ dropped when the pin changes — including on re-entry to home, where the pin can have moved under the page — a pinned view excluding the other instrument's pods, a deliberate switch calling off a drilldown's `autoFetch` — both off the URL, so a reload can't fetch the twin, and off the clock, so the armed poller can't be redirected onto it — a pinned range deep link reopening its own span rather than the newer fetch of the same bounds, Tonight filtering and link-carrying | `test_instrument.py` |
| Timeline rendering and grouping, event placement in time order, re-anchoring t₀, the pod filter, the detail drawer (read back off disk) and its warn/error filter, collapse/expand, traceback flags, a pod death drawing a full-height lifecycle marker with its kind in the tooltip, the incomplete-fetch banner, the task legend | `test_explore.py` |
| Night stats against the real counts, errors by type and pod, histograms and the click-through from a bin to its dataIds, the failure drilldown fetching its traceback context, a real crash loop spelled out in the restarts table (`restart #6`, `ImagePullBackOff`) and attributed to the visit it interrupted, the gather-only banner, every dataId deep link carrying the night's instrument so it can't auto-fetch the other camera's exposure of the same id | `test_night.py` |
| dataId lookup and its debounce, a lookup answer that lands after the field moved on being discarded rather than adopted (the route is held open, so the race is deterministic), both no-token and no-row failures, a hand-typed shutter close driving a fetch, the full fetch flow (form → job → SSE → parse → timeline) for exposure, night and range, the window pads reaching the fetch, ConsDB strings escaped | `test_home.py` |
| The Tonight panel: hidden when live mode is off, ready vs waiting rows, the viewable count, catching-up / finalised status, every kind of live problem reaching the banner, the row cap and its expander, escaping, surviving `/api/live` failing — plus one end-to-end run of the real poller where an exposure crosses from "wait" to "view" and opening it slices the night | `test_tonight.py` |
| The cache table's contents and links — including that an exposure row's link carries the instrument its window was fetched under, so it cannot open the same-numbered twin, and that both instruments' twins cached at once get a labelled link each — single-window and whole-cache deletion (checked on disk, not just in the table), the confirmation being declinable, a deleted window booting its loaded view home, and the live night dir being absent from the listing | `test_admin.py` |
| The range navigator: a chip per exposure, stepping by button and arrow key re-anchoring the timeline to each exposure's own shutter close, jumping by chip, failure flags | `test_range.py` |

The corpus's pinned facts — the shared dataId, its two shutter closes,
the range bounds — live in [`tests/ui/corpus.py`](../tests/ui/corpus.py)
rather than in the tests, so rebuilding the archive has one place to
re-check.


## Unit-test scope

The unit tests target the deterministic pieces of the codebase:

| Area               | What's tested                                                              | File                              |
|--------------------|----------------------------------------------------------------------------|------------------------------------|
| Log line parsing   | `parseLogLine` against the rapid-analysis Python log format and fallbacks (Z suffix, naive UTC, explicit offset, nano-precision trim, label-level priority, malformed JSON); `_normalizeLevel` warn/error alias buckets | `tests/test_parse.py`             |
| Event classification | `classify` for every kind in [parsing.md](parsing.md), including `HEAD_INCOMING`, the `WORKER_REPORT_FAILED` variant, and the calibrate-quantum visit→expId fallback | `tests/test_parse.py`             |
| Pod lifecycle       | `classifyK8sEvent` per kind (hand-written lines), the noise and non-Pod drops, and a whole **real** crash loop end to end: the classified sequence, the restart counter (`restart #6`) and node, the k8s reason and text surviving into the event (`ErrImagePull`, `ImagePullBackOff`, `pull QPS exceeded`), and the markers reaching `summarizePod` in time order with no dataId | `tests/test_parse.py`             |
| Pod classification | `podGroup` (longest-prefix-match, order-independence regression, full real-pod fixture parametrisation), `podOrdinal`, `podInstrument`, `groupLabels` (defensive-copy contract) | `tests/test_parse.py`             |
| Per-pod summary    | `summarizePod` against JSONL fixtures, including carryover (worker vs head), traceback capture (single, multi, chained, back-to-back, truncated body, no-class-line split into `<unclassified>` vs `<truncated>`, blank-line termination), per-dataId first/last/wait stats; `tagLinesWithExpId` empty-input edge case; `podsTouchingExp` no-match base case | `tests/test_parse.py`             |
| Cache paths        | `windowCachePath` determinism + slug-cleaning + per-`podRegex` nesting; `ensureWindowCacheDir` is the only I/O side; `NIGHT_AOS_POD_REGEX` constant pin; `dayObsStartUtc`/`dayObsEndUtc` (UTC-12 rollover + 24 h invariant + year-boundary alignment) | `tests/test_config.py`            |
| Cache reuse        | `findSupersetCache` exact / superset / no-meta / partial-flag / smallest-wins / cross-mode isolation / filtered-to-filtered nesting | `tests/test_fetch.py`             |
| Cache eviction     | `evictToFit` over flat exposure caches, nested night caches, exempt-set respect, unviewed-as-oldest, empty-parent-dir pruning (both layouts) | `tests/test_fetch.py`             |
| Cache sidecars     | `addExposureToCache` / `getCacheExposureIds` (instrument-qualified round-trip, both instruments of a shared id kept, an entry naming no instrument skipped rather than guessed at), `markCacheViewed` / `getCacheLastViewed` round-trips and best-effort no-ops; the schema-version flush unlinking every `_live.json` before a tree it may only partly remove | `tests/test_fetch.py`             |
| Misc helpers       | `humanBytes`, `fetch._parseIso` (timezone handling)                        | `tests/test_fetch.py`             |
| `logcli` wrapping  | `_run_logcli` cmd construction, missing-binary / failed-RC / timeout error paths, the `LOKI_PASSWORD` requirement (a blank or whitespace value counting as missing, since the VaultSecret is optional), `_matcher` (default vs pod-pinned vs podRegex) | `tests/test_fetch.py`             |
| fetchAll happy path | listPods + per-pod fetch with `_fetchOnePod` mocked; per-pod hard-error capture; soft `incomplete_pods` capture (unreconciled chunk, no exception); `pod_lines` / `pod_expected` in meta; `fetchComplete` iff both fall-short maps empty; progress-callback firing; exact + superset cache hits; refetch when window is in the future / schema outdated / the cached `_meta.json` won't parse; .partial flag while running | `tests/test_fetch.py`             |
| Chunked fetch (#17270) | `_countOverTime` instant-query construction (ms range, `--now`) + None-on-error; `_parseCountOutput` for the pretty-printed JSON array, line-by-line fallback, per-stream sum, garbage→None; `_queryWindowToFile` `--batch`==cap + line count; `_fetchOnePod` single-shot-under-cap, split-loses-nothing (presized), blind-bisect when oracle dark, empty-window-skips-query, floor→`incomplete` flag. Cluster calls modelled by an in-memory `_FakeLoki` stream with `SERVER_QUERY_CAP` shrunk | `tests/test_fetch.py`             |
| Schema-version flush | `ensureCacheSchemaCurrent` no-op on empty/current cache (+ sentinel write), flush on sentinel mismatch, flush when sentinel absent, current-schema cache survives | `tests/test_fetch.py`             |
| Task palette       | `_assignTaskColors` collision-freeness up to palette size + on real pipeline labels, stable across input reorders, pinned tasks honoured | `tests/test_server.py`            |
| JSON serialisation | `_toJsonable` for `datetime`, `set`, dataclass, `Path`, nested containers | `tests/test_server.py`            |
| `_summaryToDict`   | Target-expId filter, untagged-WARN windowing, per-pod summary stats (start/duration/QG-build/wait/looksTruncatedEnd), truncation flag scoped to worker groups | `tests/test_server.py`            |
| `_buildSummaryPayload` | Head-define-visit reference point derivation, taskColors collision-freeness, `other`-group surfacing, `groupLabels` shipped to UI | `tests/test_server.py`            |
| ServerContext      | Keyed `put`/`get`/LRU eviction for both exposure and night states; `evictByCacheDir` drops only matching entries (and is a no-op when nothing matches); two states coexist | `tests/test_server.py`            |
| Night helpers      | `_taiIsoToUtc`, `_buildNightPayload` (histograms + stats + failures), `_podDetailForNight` (offset from night-start), `_tracebackContextForNight` None on unknown key | `tests/test_server.py`            |
| Configuration      | `_envInt` / `_envFloat` (default only when the variable is *absent*; `ConfigError` on garbage **and on a present-but-blank value**, rather than a silent fallback); the module constants actually reading their variables (via `importlib.reload`); the defaults adding up to a working local run with no environment at all; `normalizeBasePath` canonicalisation; `cache_root` ignoring a settings file left over from an older version | `tests/test_config.py`            |
| Deployment contract | The two places this repo hands a promise to the Phalanx chart, which lives in *another repository* and would otherwise only break at deploy time: the **set of environment variable names** (adding one without adding it to the chart means production silently runs on the default), and the **Dockerfile invariants** the chart hard-codes — port 8080, `--host 0.0.0.0`, UID 1000, a pinned checksum-verified logcli, and no baked-in `ENV`. Also the exact `sites.toml` the chart's ConfigMap renders, parsed through `loadSites`. From the other side, `test_phalanx_chart.py` renders the real chart and parses every numeric value the way the application will — the variable list read out of `config.py` rather than written down, since a hand-kept list leaves each newly added knob unchecked, and a blank value (what a mistyped Helm reference renders to) is a crash-looping pod | `tests/test_config.py`, `tests/test_image_contract.py`, `tests/test_sites.py`, `tests/test_phalanx_chart.py` |
| Superseded night windows | A night fetch drops the `[nightStart, watermark]` windows its own window strictly contains (and evicts the loaded state sitting on one, which would otherwise serve a summary with empty pod drilldowns), while keeping a *wider* window, another night, another site, another pod filter, and the unfiltered exposure window inside the same span; the drop happens *before* the LRU sweep, which is the point of doing it here at all; a meta that can't say which window was written returns nothing rather than raising on the job thread. Then all of it again end to end — real clamped slices at three watermarks, one of them regressed, through the real callback — which is the only place the meta-carries-the-written-window seam is observable | `tests/test_server.py`, `tests/test_live.py` |
| Server-side helpers | `_resolveCacheWindow` path-component allowlist + `pods=`-prefix gate; `_resolveStaticFile` refusing an absolute `rel` and anything resolving outside `STATIC_DIR`; `_buildNightSpecFromRequest` happy path + every validation error + that a body-supplied site / credentials / worker count are ignored; `_parseClientIso` + `_isoForLogcli` parsing / UTC conversion | `tests/test_server.py`            |
| `night.py` rollups | `computeTopStats`, `errorsByType` (sort + sample-message), `errorsByPod`, `firstTaskStartByDataId` (cross-pod min, None-expId skip), `calcZernikesEndByDataId` (substring match, ignores non-DONE), `buildHistogram` (binning, drops, dataId attribution, single-value, parallel-list validation), `computeDeltaShutterOffsets`, `failureRows` (offsetS / sort / unique bodyKey), `tracebackBody` round-trip, `gatherOnlyDataIds` (step1b-without-step1a flag, pipeline pairing, no-gather and all-paired empty cases); all rollups exercise their empty-input degenerate paths | `tests/test_night.py`             |
| Instrument identity | An exposure id is unique only within an instrument, so: records are stamped with the `cdb_<instrument>` table they were read from; `queryExposureRecordsForDayObs` asks every instrument and returns a list, keeping both halves of a colliding id; `probeOrderWinners` decides the bare-id answer the same way `queryExposureRecord` does; `storeCachedRecordList` writes per-instrument keys plus the probe-order winner under the bare key; an instrument-scoped lookup never falls back to the bare key. Endpoint side: `/api/exposure-time/<id>?instrument=` queries only that table, echoes the instrument, caches without clobbering the bare key, and 400s an unknown instrument | `tests/test_exposure_times.py`, `tests/test_server_endpoints.py` |
| Exposure-time lookup | `queryExposureRecord` happy path (curated projection, SELECT * wire shape, caller-supplied URL, Bearer-header-only token), instrument fallthrough, all-empty None, 500 propagation, 500-UndefinedTable fallthrough. `queryExposureRecordBatch` single-call resolution, pinned-never-falls-through, multi-instrument fallthrough, chunking, UndefinedTable, malformed rows, missing-column-skip, empty-input short-circuit. `readToken` strip + missing file. `lookupCachedRecord` / `storeCachedRecord(s)` round-trip + corrupt-recovery + non-record entries ignored (one shape only; no compatibility readers) + eighty threads storing at once all surviving, since a lost write here can take a `_manual` stand-in that exists nowhere else. `manualRecord` tagging + instrument stamping. `_postQuery` 400-as-empty + typed 5xx errors. | `tests/test_exposure_times.py` |
| Job manager        | `FetchJob` event ordering, status transitions (pending→running→parsing→done), error path captures terminal `error` event, `onComplete` fires before `done` (verified by snapshotting `len(events)` from inside the callback), the `done` event carrying the pin the fetch ran under (the client asks `/api/summary` for it by name straight after), `startJob` runs in background, condvar wake. `createNightJob` distinct shape. `runJob` populates `cacheDir`/`meta`. `stateLock` is a real Lock (not RLock). | `tests/test_jobs.py`              |
| HTTP endpoints     | Spins up the real server on an ephemeral port and hits it with `http.client`. Covers: `/api/summary` (empty / by-dataId / by-dayObs / 400-on-bad-int / mode-discriminator / LRU touch on hit), `/api/cache` (lists exposure + night, partial skipping, sidecar fields surfaced, each exposure row's triggering exposures carrying the instrument they were fetched under), `/api/fetch` + `/api/fetch-night` (body validation, 202 + status polling to done, NightState populated, podRegex on night spec), `/api/pod` (400 no key, 404 not loaded, valid-name allowlist), `/api/exposure-time/<>` (200 / 404 / 503-no-token / 503-empty-token / cache short-circuit / cache write / a `?site=` query param ignored), `/api/night/traceback/<key>` (dataId-block context, time-window fallback, 404 unknown bodyKey, 400 bad-int dayObs), `DELETE /api/cache` (all + single + path-traversal-rejection + state-cleared-when-matching + every `_live.json` unlinked before the tree + still answering with the fresh listing when the tree regrows a file mid-`rmtree` rather than dropping the connection), the static route refusing `/static//proc/self/environ` and friends rather than reading an absolute path, the range endpoints refusing a span pinned to the other instrument (summary rebuilds, pod 404s) and the range-exposure payload carrying its pin into `podDetailQuery`, the job status and SSE `done` event both carrying `instrument`, `/api/site` (the served site, no token path echoed) and `/api/sites` gone, the base-path routing (probe + API + static assets + SSE under the prefix, 404 outside it and for partial prefixes, multi-segment prefixes, query strings surviving the strip, index substitution, window fields tracking the configured defaults, no configuration fields in the HTML), `/healthz` still answering while a fetch worker is parked mid-`fetchAll`, SSE `/api/fetch/<id>/progress` (history replay + terminal close + 404), `_buildSpecFromRequest` (TAI/UTC, body-supplied site / credentials ignored, validation errors), `_prefetchNightShutterCloses` (no-token, consdb-error, short-circuit-when-empty) | `tests/test_server_endpoints.py` |
| CLI parsing        | `_parseIsoUtc` for Z / no-offset / explicit-offset (positive and negative) / microseconds; `_isoForLogcli` Z suffix + UTC conversion; TAI constant pin; subparser arg parsing + `--t-zero-utc` flag; partial-args rejection; eager-fetch TAI→UTC conversion and `--t-zero-utc` opt-out; `--force-refresh` reaches fetchAll; `_warnIfIncompleteFetch` silent-when-clean / shouts on hard `errors` / shouts on soft `incomplete_pods` / caps the list; `cache info` / `cache flush` behaviour (with-yes / decline-prompt / empty-cache-root / night-mode `pods=<slug>` row surfacing) | `tests/test_cli.py`               |
| Live night cache   | `currentDayObs` (noon-UTC rollover, inverse of `dayObsStartUtc`); `firstOffsetAtOrAfter` byte-bisect (exact line start, between lines, before-all, after-all, torn-write-beyond-limit invisibility); `findNightDirCovering` watermark/cluster gating; `materializeNightSlice` (refusing a night whose sidecar gained a `rewritingPod` after `_tryNightSlice`'s own check — the two are separated by acquiring the destination lock; half-open boundary exactness, first-class result that exact-hits on repeat, `podRegex` filtering into the nested `pods=` dir, fall-short-map inheritance scoped to the filter, lifecycle-event slicing by its own byte count and only for pods with app logs, pruning files a failed attempt stranded, `.partial` surviving a failure); `fetchAll` integration (slice served with no `_run_logcli` call, in-progress night's own window clamped to the watermark + exact reuse at an unchanged watermark, arbitrary past-watermark windows falling through to a real fetch, an exact cache beating the slice path, the night's own window handed over rather than sliced into itself, a fresh fetch into a live dir refused, a night whose sidecar carries `rewritingPod` not sliced at all while the swap is in flight, four concurrent `fetchAll`s for one window serialised to one writer); `_sliceFileByTime` refusing `src == dst`; `findSupersetCache` refusing live-built dirs | `tests/test_live.py`              |
| Instrument threading | `_instrumentFromBody` default/normalise/validate; `_podBelongsToInstrument` rules; exposure payload filters `pods` + `podsAll` by the state's instrument (and filters nothing when unpinned); `queryExposureRecordBatch` pinned never falls through to another table; `_resolveShutterClosesInto` pin reaches both the cache lookups (colliding id anchors to the pinned instrument's t₀) and the ConsDB batch; night prefetch hard-pins `lsstcam`, range prefetch pins the job's instrument; `_loadExposureFromCache` refuses a window that doesn't contain the pinned t₀ and stamps the rebuilt state; range per-exposure payload filters by the range's instrument; `manualRecord` instrument stamping round-trips through the cache. Endpoint level: POST bodies validate the name (400 unknown), the fetch's instrument lands on the state and payload, defaults to `lsstcam`, a manual t₀ is stamped, and `/api/summary?instrument=` refuses a same-id state pinned to the other instrument while unpinned requests still serve | `tests/test_server.py`, `tests/test_server_endpoints.py`, `tests/test_exposure_times.py` |
| Browser (UI)       | Chromium against the real app over a real cut-down night — routing, the base path, the instrument pin, the timeline, the night view, the forms and fetch flow, the Tonight panel, the cache admin table, and the range navigator. See *Browser tests* above for the breakdown | `tests/ui/` |
| Live poller        | `LiveNightManager.tick` driven with stubbed fetch edges: increments tile across ticks — asserted on the *windows requested*, not just the bytes, since the stub applies half-open semantics itself and would mask an off-by-one; a failed pod pins the global watermark and self-heals by refetching its whole missed span (including the pod-fails-on-first-fetch case); pods absent from the listing advance for free (a dead pod can't pin the night); readiness follows the watermark and persists records to the per-site exposure-time cache; colliding ids from two instruments both appear and cache separately; restart recovery truncates unrecorded (torn) bytes and clears stranded temp files; a failed app-log append rolls the file back to its pre-append size so the retried span can't land after stranded bytes; noon rollover finalises (`_meta.json`, `liveBuilt`, sidecar `finalised`) and moves to the new night; the verification pass refetches a pod beyond the dedup-slack tolerance, leaves one within it alone, keeps an incompleteness flag its own refetch set, and publishes zero durable bytes before swapping the file; a night left unfinalised by a restart across noon is swept up on a later tick (and a failure to do so is reported, not fatal); a cache wipe under the poller re-opens the night instead of wedging it; `_runLoop` survives a failing tick; `--live-day-obs` pins the night and adopts an already-finalised one without re-fetching | `tests/test_live.py`              |
| k8s/events demux   | The namespace-wide events stream is demuxed by `name` into `pods_events/`, deliberately including non-Pod objects: a new name must not drag the global watermark back to night start, it is filed in `eventPods` with no watermark of its own, it promotes into `pods` (carrying its counters) when it first emits app logs, and `pods.txt` stays the app-log pod list. Failure handling: a failed fetch leaves the events watermark alone without blocking app logs, and a failed *append* rolls every file back so the retry can't duplicate lines. Plus the tolerance the breadth relies on — `summarizeAll` never opens a lifecycle file with no app-log sibling, and `classifyK8sEvent` declines a non-Pod event | `tests/test_live.py`              |

### What we don't unit-test

- The actual `logcli` subprocess invocation — the wrapper is
  thoroughly mocked but a real Loki round-trip only happens during
  the smoke test. CI doesn't have a Loki instance.
- The full end-to-end fetch+UI flow with **real Loki traffic**. The
  browser tests stand `logcli` in for a cluster; the subprocess boundary
  itself is only exercised by the smoke test.
- **Scale.** The browser tests' corpus has ~6 SFM pods where a real
  LSSTCam visit fans out to ~189. Correctness is covered; how a 435-row
  timeline renders and performs is not.
- **Browsers other than Chromium**, pixel-level appearance, responsive
  layouts, and accessibility beyond what the DOM assertions incidentally
  touch. Adding Firefox/WebKit is a one-line config change if it ever
  matters; screenshot diffing was considered and rejected — font and
  platform differences make it flaky enough to get ignored, which is
  worse than not having it.
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
- `pod_crash_events.jsonl` — one pod's whole `k8s/events` stream through
  a real crash loop: five in-place restarts, a reschedule, then
  `ImagePullBackOff`. Captured from BTS (dayObs 20260622) because the
  nights we have pulled from the summit are healthy ones — theirs hold
  `Started` and `Killing` and nothing else, and a crash is precisely what
  these markers exist to explain. Only the timestamps are edited, onto
  the night the browser tests' corpus covers, so the same file serves the
  parser tests and gets planted into the corpus by
  `StagedCorpus.plantPodCrash`. See
  [parsing.md](parsing.md#pod-lifecycle-events-from-the-k8sevents-stream-not-the-app-log)
  for which lifecycle kinds still have no real capture behind them.

These are hand-trimmed from real cluster logs to keep the fixtures
small and to make the test assertions specific. When you extend the
parser to recognise a new event kind, add the minimal triggering
line to the relevant fixture and a test assertion that pins the
parsed event.

## Capturing a night to work against

The fixtures above are lines; the browser corpus is a night with most of
it cut away. Some work needs the opposite — a **whole real night**, every
pod, at full scale: the night-summary rollups, the histograms, anything
about lifecycle events or restarts, live mode, and any question of the
form "how does this behave with 435 pods rather than 32".

[`tools/captureNight.py`](../tools/README.md) is how you get one, and
[`tools/stageNight.py`](../tools/README.md) is how you serve it:

```sh
export LOKI_PASSWORD=...      # VPN up, logcli on $PATH

.venv/bin/python tools/captureNight.py \
    --site bts --day-obs 20260811 20260812 --out ~/temp/log_explorer_data/master

.venv/bin/python tools/stageNight.py \
    --master ~/temp/log_explorer_data/master/aug11-night-bts \
    --master ~/temp/log_explorer_data/master/aug12-night-bts \
    --cache ~/temp/log_explorer_data/app-cache

RA_LOG_EXPLORER_CACHE=~/temp/log_explorer_data/app-cache \
RA_LOG_EXPLORER_MAX_CACHE_BYTES=32212254720 \
    .venv/bin/python -m ra_log_explorer.cli run --no-browser
```

### Masters, and why they're separate from the cache

A **master** is a finalised night directory kept somewhere the
application never looks: `pods/`, `pods_events/`, `pods.txt`, a
`_live.json` sidecar and a `_meta.json`, named for what it holds
(`aug11-night-bts` — month, day, `night`, site, because the same dayObs
exists on both clusters and they are different data). The ConsDB records
for every captured night accumulate beside them in
`<out>/exposure-times/<site>.json`, mirroring the cache root's own
layout so staging is a straight copy — without them the Tonight panel
has no shutter closes to resolve against.

Masters are never served directly. Staging clones them with `cp -c`
(APFS copy-on-write: instant, and the app cannot corrupt the master no
matter what it evicts, slices or deletes), so restaging is how you throw
away whatever a session did to the cache. Point the server at a master
and one LRU pass or one *delete window* click takes hours of fetching
with it.

### Why capture goes through the live poller

`captureNight.py` drives a real `LiveNightManager` pinned to the target
dayObs (`fixedDayObs`) rather than calling `fetchAll`, for three
reasons that all show up on a capture measured in hours:

- **It produces what live mode produces.** Namespace-wide `k8s/events`
  demuxed per name — including the ReplicaSet/StatefulSet/Deployment
  names an all-pods batch fetch never asks for — plus the sidecar. A
  master captured this way *is* a live night, so `--live-day-obs` can
  replay it with no conversion.
- **Resume is free.** The sidecar carries a per-pod watermark, so a
  capture that dies half way through is resumed by re-running the same
  command: `_recoverNight` truncates torn bytes and only the missing
  spans are refetched. Before this tool existed, resuming meant a
  bespoke script that recovered the pod list by parsing the previous
  run's progress log.
- **Verification is free.** Finalisation runs the `count_over_time`
  oracle for every pod and refetches any that fall short beyond
  `live.VERIFY_TOLERANCE_*` — the same audit the deployed poller applies
  at the noon rollover, and the thing that turns "we fetched a night"
  into "we fetched all of a night".

One tick covers the whole 24 h, because a night that has already ended
puts the increment target at night end. A night that *hasn't* ended is
refused unless you pass `--allow-partial`: dayObs 20260813 runs to
12:00 UTC on the 14th, and a capture started at 11:00 would silently be
an hour short with a `_meta.json` claiming otherwise.

### What it costs

Measured, so you can size a capture before starting one. The summit and
BTS differ by two orders of magnitude, and the difference is the whole
reason the browser tests run against a cut-down corpus:

| Night | Pods | Log lines | On disk | Wall clock |
|---|---|---|---|---|
| 20260711, summit (`yagan`) | 576 | 35.7M | 9.25 GiB | ~65 min |
| 20260811, BTS (`manke`) | 265 | 185k | 49 MiB | 1.8 min |
| 20260812, BTS (`manke`) | 182 | 598k | 135 MiB | 2.9 min |

### Replaying a night as "tonight"

`--unfinalise <dayObs>` stages one night as an in-progress live night:
`_meta.json` removed and the sidecar's `finalised` flag cleared, which is
the state the poller maintains while observing. Run the server with
`--live-poll-s 60 --live-day-obs <dayObs>` and the historical night plays
the part of tonight — the Tonight panel, readiness, slicing, the whole
live path against real data, with the noon rollover never firing.

Without the flag the night stages finalised, which is what you want for
everything else: the poller adopts an already-finalised night without
re-fetching it, and every view is served by slicing.

## Running the suite

From the repo root, with the venv active or its tools on `$PATH`:

```bash
.venv/bin/pytest -q -n auto
.venv/bin/mypy
.venv/bin/mypy-coverage     # body-coverage target: 100%
.venv/bin/pre-commit run --all-files
```

The runtime itself is stdlib-only; pytest / mypy / black / isort /
flake8 are dev-only dependencies that live in the venv, as are
Playwright and pytest-xdist (the `ui-test` extra).

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

# The catalog the chart supplies as a ConfigMap has no equivalent in the
# repo — the image bakes in no environment — so write the same shape
# once. Exactly one site, and no `consdbTokenFile` at all: in-cluster
# ConsDB is reached without passing through Gafaelfawr, so there is no
# token to mount. Without this the container exits on a SitesConfigError
# before it serves anything.
mkdir -p sites
cat > sites/sites.toml <<'TOML'
default_site = "bts"

[[site]]
name = "bts"
cluster = "manke"
namespace = "rapid-analysis"
lokiAddr = "https://loki-query.ls.lsst.org"
consdbUrl = "http://consdb-pq.consdb.svc.cluster.local:8080/consdb/query"
TOML

# Run it the way the deployment does: read-only root filesystem, every
# writable path a mounted volume, and that one-site catalog. If it works
# here it will work in the pod.
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
