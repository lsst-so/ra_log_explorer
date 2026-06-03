# Testing

The project has two test layers:

1. **Unit tests** under [tests/](../tests/) — pure-Python, no network
   or persistent filesystem state. Each test gets a per-test cache
   root via the `tmpCacheRoot` fixture so on-disk side effects don't
   leak between tests. Run with `pytest`.
2. **End-to-end smoke test** — running the CLI against the real
   Loki cluster (and against ConsDB for the shutter-close lookup)
   for a known dataId or dayObs, which we do by hand before tagging
   a release-worthy state.

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
| App settings       | `loadAppSettings` defaults, save→load round-trip, corrupt-file fallback, unknown-field tolerance, cache-root creation | `tests/test_appSettings.py`       |
| Log line parsing   | `parseLogLine` against the rapid-analysis Python log format and fallbacks (Z suffix, naive UTC, explicit offset, nano-precision trim, label-level priority, malformed JSON); `_normalizeLevel` warn/error alias buckets | `tests/test_parse.py`             |
| Event classification | `classify` for every kind in [parsing.md](parsing.md), including `HEAD_INCOMING`, the `WORKER_REPORT_FAILED` variant, and the calibrate-quantum visit→expId fallback | `tests/test_parse.py`             |
| Pod classification | `podGroup` (longest-prefix-match, order-independence regression, full real-pod fixture parametrisation), `podOrdinal`, `podInstrument`, `groupLabels` (defensive-copy contract) | `tests/test_parse.py`             |
| Per-pod summary    | `summarizePod` against JSONL fixtures, including carryover (worker vs head), traceback capture (single, multi, chained, back-to-back, truncated body, no-class-line, blank-line termination), per-dataId first/last/wait stats; `tagLinesWithExpId` empty-input edge case; `podsTouchingExp` no-match base case | `tests/test_parse.py`             |
| Cache paths        | `windowCachePath` determinism + slug-cleaning + per-`podRegex` nesting; `ensureWindowCacheDir` is the only I/O side; `NIGHT_AOS_POD_REGEX` constant pin; `dayObsStartUtc`/`dayObsEndUtc` (UTC-12 rollover + 24 h invariant + year-boundary alignment) | `tests/test_config.py`            |
| Cache reuse        | `findSupersetCache` exact / superset / no-meta / partial-flag / smallest-wins / cross-mode isolation / filtered-to-filtered nesting | `tests/test_fetch.py`             |
| Cache eviction     | `evictToFit` over flat exposure caches, nested night caches, exempt-set respect, unviewed-as-oldest, empty-parent-dir pruning (both layouts) | `tests/test_fetch.py`             |
| Cache sidecars     | `addExposureToCache` / `getCacheExposureIds`, `markCacheViewed` / `getCacheLastViewed` round-trips and best-effort no-ops | `tests/test_fetch.py`             |
| Misc helpers       | `humanBytes`, `fetch._parseIso` (timezone handling)                        | `tests/test_fetch.py`             |
| `logcli` wrapping  | `_run_logcli` cmd construction, missing-binary / failed-RC / timeout error paths, `LOKI_PASSWORD` requirement, `_matcher` (default vs pod-pinned vs podRegex) | `tests/test_fetch.py`             |
| fetchAll happy path | listPods + per-pod fetch with subprocess mocked; per-pod error capture; progress-callback firing; exact + superset cache hits; refetch when window is in the future; .partial flag while running | `tests/test_fetch.py`             |
| Task palette       | `_assignTaskColors` collision-freeness up to palette size + on real pipeline labels, stable across input reorders, pinned tasks honoured | `tests/test_server.py`            |
| JSON serialisation | `_toJsonable` for `datetime`, `set`, dataclass, `Path`, nested containers | `tests/test_server.py`            |
| `_summaryToDict`   | Target-expId filter, untagged-WARN windowing, per-pod summary stats (start/duration/QG-build/wait/looksTruncatedEnd), truncation flag scoped to worker groups | `tests/test_server.py`            |
| `_buildSummaryPayload` | Head-define-visit reference point derivation, taskColors collision-freeness, `other`-group surfacing, `groupLabels` shipped to UI | `tests/test_server.py`            |
| ServerContext      | Keyed `put`/`get`/LRU eviction for both exposure and night states; `evictByCacheDir` drops only matching entries (and is a no-op when nothing matches); two states coexist | `tests/test_server.py`            |
| Night helpers      | `_taiIsoToUtc`, `_buildNightPayload` (histograms + stats + failures), `_podDetailForNight` (offset from night-start), `_tracebackContextForNight` None on unknown key | `tests/test_server.py`            |
| Server-side helpers | `_resolveCacheWindow` path-component allowlist + `pods=`-prefix gate; `_buildNightSpecFromRequest` happy path + every validation error + password passthrough; `_maybeSetLokiPassword` sets / doesn't clobber an existing env var; `_parseClientIso` + `_isoForLogcli` parsing / UTC conversion | `tests/test_server.py`            |
| `night.py` rollups | `computeTopStats`, `errorsByType` (sort + sample-message), `errorsByPod`, `firstTaskStartByDataId` (cross-pod min, None-expId skip), `calcZernikesEndByDataId` (substring match, ignores non-DONE), `buildHistogram` (binning, drops, dataId attribution, single-value, parallel-list validation), `computeDeltaShutterOffsets`, `failureRows` (offsetS / sort / unique bodyKey), `tracebackBody` round-trip; all rollups exercise their empty-input degenerate paths | `tests/test_night.py`             |
| Exposure-time lookup | `queryIsot` happy path, instrument fallthrough, no-row 404, 500 propagation, 500-UndefinedTable fallthrough, missing obs_end column. `queryIsotBatch` single-instrument-hit, multi-instrument fallthrough, chunking, UndefinedTable, malformed rows, missing-column-skip, empty-input short-circuit. `rspTokenFilePath` env-var + explicit-override + tilde-expand. `readRspToken` strip + missing file. `lookupCached` / `storeCached` round-trip + corrupt-recovery + non-string-value. `_sqlFor` wire format. `_postQuery` 400-as-empty + 503-as-error. | `tests/test_exposure_times.py` |
| Job manager        | `FetchJob` event ordering, status transitions (pending→running→parsing→done), error path captures terminal `error` event, `onComplete` fires before `done` (verified by snapshotting `len(events)` from inside the callback), `startJob` runs in background, condvar wake. `createNightJob` distinct shape. `runJob` populates `cacheDir`/`meta`. `stateLock` is a real Lock (not RLock). | `tests/test_jobs.py`              |
| HTTP endpoints     | Spins up the real server on an ephemeral port and hits it with `http.client`. Covers: `/api/summary` (empty / by-dataId / by-dayObs / 400-on-bad-int / mode-discriminator / LRU touch on hit), `/api/cache` (lists exposure + night, partial skipping, sidecar fields surfaced), `/api/fetch` + `/api/fetch-night` (body validation, 202 + status polling to done, NightState populated, podRegex on night spec), `/api/pod` (400 no key, 404 not loaded, valid-name allowlist), `/api/exposure-time/<>` (200 / 404 / 503-no-token / 503-empty-token / cache short-circuit / cache write / `?tokenFile=` override), `/api/night/traceback/<key>` (dataId-block context, time-window fallback, 404 unknown bodyKey, 400 bad-int dayObs), `DELETE /api/cache` (all + single + path-traversal-rejection + state-cleared-when-matching), `/api/settings` (GET + PUT round-trip + validation), SSE `/api/fetch/<id>/progress` (history replay + terminal close + 404), `_buildSpecFromRequest` (TAI/UTC, password passthrough, validation errors), `_prefetchNightShutterCloses` (no-token, consdb-error, short-circuit-when-empty) | `tests/test_server_endpoints.py` |
| CLI parsing        | `_parseIsoUtc` for Z / no-offset / explicit-offset (positive and negative) / microseconds; `_isoForLogcli` Z suffix + UTC conversion; TAI constant pin; subparser arg parsing + `--t-zero-utc` flag; partial-args rejection; eager-fetch TAI→UTC conversion and `--t-zero-utc` opt-out; `--force-refresh` reaches fetchAll; `cache info` / `cache flush` behaviour (with-yes / decline-prompt / empty-cache-root / night-mode `pods=<slug>` row surfacing) | `tests/test_cli.py`               |

### What we don't unit-test

- The actual `logcli` subprocess invocation — the wrapper is
  thoroughly mocked but a real Loki round-trip only happens during
  the smoke test. CI doesn't have a Loki instance.
- The browser UI itself. We rely on hand verification.
- The full end-to-end fetch+UI flow with real Loki traffic. That's
  the smoke test.

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

Before declaring work done, hand-verify against a known exposure:

```bash
export LOKI_PASSWORD=...
# RSP token in ~/.lsst/log-browser-token.txt
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267 \
    --no-browser
```

Then `curl http://127.0.0.1:8765/api/summary?dataId=2026051900722`
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

The first run for a given window takes 60–90 s (exposure) to
several minutes (night). A second run for the same — or any
overlapping — window is instant thanks to superset reuse.
