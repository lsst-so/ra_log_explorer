---
name: ra-log-explorer-loki
description: Apply the rapid analysis Loki / logcli conventions when editing or extending the fetch path in `ra_log_explorer/fetch.py`, or when writing a one-off script that calls `logcli` against the rapid-analysis namespace. Captures the gotchas the existing code already encodes (jsonl output, --forward order, --quiet, the password env var, the timestamp-trim quirk, why we list pods first instead of one big query) and points back at the canonical implementation so you don't reinvent it. Use this skill when a task touches Loki query construction, when a `logcli` invocation is involved, when discussing parallelism / window sizing for cluster log retrieval, or when investigating a fetch-path bug. **Not** for parsing log content (that's the `ra-log-explorer-code-style` + `parse.py` regex taxonomy in `architecture/parsing.md`).
---

# ra_log_explorer: Loki / logcli conventions

The shape of how this project talks to Loki is captured in
[ra_log_explorer/fetch.py](../../../ra_log_explorer/fetch.py); use that as
the canonical reference for query construction, parallelism, and caching.
This skill exists to surface the tribal knowledge sitting *behind* the
code — the gotchas you need to know before adding a new logcli call or
debugging a misbehaving fetch.

## Canonical helpers (use these — don't reinvent)

| Need                                  | Use                                                         |
|----------------------------------------|--------------------------------------------------------------|
| Run any logcli command                | `fetch._run_logcli(spec, [...])`                            |
| List pods in a window                  | `fetch.listPods(spec)`                                       |
| Fetch one pod's JSONL (chunked, safe)  | `fetch._fetchOnePod(spec, pod, outPath)`                     |
| Count lines in a window (exact oracle) | `fetch._countOverTime(spec, pod, fromT, toT)`               |
| Fetch one time-chunk single-batch      | `fetch._queryWindowToFile(spec, pod, fromT, toT, outPath)`  |
| Whole-window fetch with caching        | `fetch.fetchAll(spec)`                                       |
| Find a reusable wider cache            | `fetch.findSupersetCache(cluster, ns, fromIso, toIso)`       |
| Parse Loki's timestamp string          | `parse._parseTimestamp(s)` (in `parse.py`, not `fetch.py`)   |

If you're tempted to call `subprocess.run(["logcli", ...])` directly, ask
why `_run_logcli` doesn't already do what you need.

## Connection / auth

- The Loki endpoint is `https://loki-query.ls.lsst.org` for both sites —
  BTS and the summit share one Loki and are distinguished by the
  `cluster` label (`manke` / `yagan`), not by the address. It comes from
  the site catalog rather than being hard-coded, but there is no second
  address in scope; don't parameterise further until there is.
- HTTP basic auth: `--username=<user>` on the command line,
  `LOKI_PASSWORD` in the environment. `_run_logcli` refuses to spawn
  logcli if `LOKI_PASSWORD` is unset — surface that error to the user
  rather than guessing. Deployed, both come from the environment: the
  username from `$LOKI_USERNAME` (a service account, not a person) and
  the password from the environment's Vault secret. On a laptop the
  developer is expected to have the password in their shell rc.
- **Credentials never come from a request.** There is no credentials
  panel and the fetch endpoints ignore a `username` / `password` in the
  body. `LOKI_PASSWORD` is process-global and one process serves every
  user of a deployment, so honouring a browser-supplied password would
  let one person's typo break fetching for everybody. Don't reintroduce
  a path that sets it per request. We also do **not** support keyring
  prompts or other interactive paths.

## Query construction

### Always select by `cluster` *and* `namespace`

```
{cluster="yagan",namespace="rapid-analysis"}
```

A bare `{namespace="rapid-analysis"}` selector matches across all
clusters and is dramatically slower. The current code always includes
both, even when only one cluster is in scope today.

### `series` vs `query` — different jobs

- `logcli series '{...}' --from --to` returns the stream label sets in
  the window. We use it once per fetch (`listPods`) to discover which
  pods to enumerate.
- `logcli query '{... pod="..."}' --from --to -o jsonl --forward` fetches
  log lines for one pod. We use it once *per pod* in parallel.

Issuing a single broad `query` against the whole namespace and
post-splitting the lines by pod would be simpler, but it has two
problems:
1. We fetch *every* line per pod (see *The #17270 line-loss bug* below).
   A single broad query over the whole namespace would be far larger,
   slower, and harder to bound; per-pod queries keep each download
   independently sized and isolate one pod's failure (recorded in
   `_meta.json`'s `errors`) instead of letting it take down the window.
2. The on-disk cache is naturally indexed by pod (one `.jsonl` per pod);
   per-pod queries map straight onto that layout with no post-processing.

If you ever change this, document the trade-off in
[architecture/caching.md](../../../architecture/caching.md).

### The #17270 line-loss bug (read before touching the fetch path)

`logcli query --limit=0` does **not** reliably fetch every line. On wide,
busy windows it silently drops entries (grafana/loki#17270): when a query
response spans multiple Loki streams, logcli advances the next batch's
cursor by the *global* max timestamp across streams, so a busier stream's
tail falls into a 1 ns dedup gap and vanishes. A single `{pod=…}` is
already ≥2 streams (by `detected_level`: info/unknown/warn), so a 24 h AOS
night came back ~9 % short while still reporting itself complete. A finite
`--limit` cap is *also* lossy (it tail-drops the busiest pods). So neither
"limit=0" nor "limit=N" is safe on its own.

The loss only happens when a query **paginates** — when some batch comes
back full and logcli asks for another. `_fetchOnePod` exploits that:

- **`_countOverTime`** uses `sum(count_over_time({pod}[range]))` as an
  exact oracle. It's a *server-side aggregation* (no entry pagination), so
  it's immune to the bug — `1717 == 1717` on a complete window. Use it to
  presize chunks (target `CHUNK_TARGET_LINES`, below the batch size).
  One caveat when using it to *audit* a fetch: the metric path counts
  duplicate entries sitting in overlapping storage chunks that the
  log-query path deduplicates, so on real nights the oracle runs a
  stable ~0.03% (tens of lines per pod) *high* against a byte-perfect
  fetch — verified by refetching (identical bytes) and by fetching the
  padded sliver (empty). Never treat a small `expected > got` as data
  loss; `live.VERIFY_TOLERANCE_*` is the tolerance the finalisation
  audit uses. (It can never run *low*, which is what chunk presizing
  and the zero-count short-circuit rely on.)
  Mind the interval mismatch: a `[range]` selector at `--now=to` covers
  `(to - range, to]`, but the fetch covers `[from, to)`. Round the range
  **up** and pad it (we add 1 ms) so the count is a strict superset —
  a zero count short-circuits the chunk's fetch entirely, so an
  under-count there is a silent dropped line.
- **`_queryWindowToFile`** fetches one chunk with
  `--batch=SERVER_QUERY_CAP`. A chunk is trusted **only** when
  `got < SERVER_QUERY_CAP` — proof it completed in one un-paginated
  request, so the bug couldn't fire. The count is just an accelerator;
  this structural check is what guarantees correctness (if the oracle is
  unavailable the chunker blind-bisects and is still correct).
- A chunk that fills a batch is discarded and re-fetched as two half-open
  time halves, recursing until each fits. At the `MIN_SPLIT_S` floor an
  unsplittable burst is flagged in `_meta.json`'s `incomplete_pods`.

`SERVER_QUERY_CAP` **must equal** the cluster's `max_entries_limit_per_query`
(≈5000). Setting `--batch` *above* the real cap breaks the trust check —
a full server response would look non-full and logcli would stop early,
silently truncating. Lowering it only costs extra (still-correct) chunking.

### Required logcli flags for the per-pod `query`

```
--quiet                  drop the API URL + common-labels preamble
-o jsonl                 one JSON object per line, with labels preserved
--forward                ascending time order; otherwise we'd need to
                         reverse the file before parsing
--limit=0                no client-side line cap…
--batch=SERVER_QUERY_CAP …but bounded per request to the server cap, so a
                         trusted (got < cap) chunk is provably un-paginated
--from --to              RFC3339Nano UTC strings ending in 'Z'
```

Drop any of these at your peril; tests assume the file is in `--forward`
order, that `labels.detected_level` is present, and that the batch size
equals the cap. Each chunk is streamed straight to a temp file
(`_run_logcli(spec, [...], stdoutPath=…)`) so a chatty pod can't blow up
memory, then appended to the pod's `.jsonl` once trusted. The whole
per-pod tree runs under `PER_POD_TIMEOUT_S` (1800 s); the oracle gets the
shorter `COUNT_TIMEOUT_S`.

### `-o jsonl` strips labels common to the response

Each entry's `labels` object holds only the labels that **vary** within
the response; logcli factors out the common ones (they are what
`--quiet` suppresses from the preamble). So `container` appears on a
pod's lines only when that response happened to span two containers, and
its *absence* means "one container in this chunk", not "no container
label". Presence is therefore an artefact of chunk boundaries.

Consequence, learned the hard way: do not build logic on a label being
there. A rule of the form "the lines before this event came from a
different container" looked exact and was measured to work on four
nights, but only because the init container's one line and the main
container's lines happened to land in the same request every time. If
you need per-container attribution, ask for it explicitly (a
`{...} | container="x"` selector) rather than reading it off the entries.

### Timestamps

- `--from` / `--to` / `--now` want RFC3339-ish strings; we format them as
  `YYYY-MM-DDTHH:MM:SS.uuuuuuZ` via `cli._isoForLogcli` (window edges) and
  `fetch._fmtLogcliTime` (split-chunk boundaries — same format, so chunk
  edges stay consistent with the requested window). Always pass UTC with a
  trailing `Z`; don't supply a `+HH:MM` offset.
- Loki's response timestamps look like
  `"2026-05-20T09:45:46.216887282+01:00"` — i.e. **nanosecond**
  precision and **local-cluster offset** (the cluster runs in
  `Europe/London` summer time, so `+01:00`). `parse._parseTimestamp`
  trims the sub-microsecond digits before handing the string to
  `datetime.fromisoformat`. Don't strip the offset, don't lose precision.

## Parallelism

`FetchSpec.workers` controls a `ThreadPoolExecutor`. It comes from
`config.DEFAULT_WORKERS` (`$RA_LOG_EXPLORER_WORKERS`, default 8) and is
*not* settable per request — it is deployment configuration, tuned per
environment in the Helm chart. 8–16 is the useful range; beyond that you
start hitting Loki ingestion-side backpressure that manifests as
occasional logcli timeouts (`_run_logcli` catches and reports those
per-pod, so other pods keep going).

Remember that a deployment is shared: several people can trigger fetches
at once, so the effective concurrency against Loki is workers × the
number of in-flight jobs, not just `workers`.

A chatty pod over a full night fans out into many count-presized chunks
(plus the re-splits when a chunk overflows), each its own logcli process.
That's why the per-pod budget is the generous `PER_POD_TIMEOUT_S` (1800 s)
covering the whole tree, not the series default — bump it further if real
nights start hitting it. The chunk count is more logcli invocations than
the old one-query-per-pod, but each is bounded and the result is correct;
the cache makes the cost a one-time hit per window.

## Error modes you should expect

- **`logcli binary not found on PATH`** — surfaced as `FetchError`.
  Locally, `brew install grafana/grafana/logcli` or equivalent. In the
  deployed image logcli is baked in at a **pinned** version (see the
  `Dockerfile`); the pin is deliberate, because the whole #17270
  workaround below reasons about exactly when logcli paginates. Bumping
  it needs the same end-to-end verification a fetch-path change does.
- **`logcli failed (rc=N): ...`** — we keep the first 500 chars of
  stderr; the most common non-fatal reason is a transient 502/504
  through nginx, retryable by re-running.
- **`logcli timed out`** — happens when a pod's `.jsonl` is huge or the
  cluster is busy. Drop `--workers` or narrow the window. Any per-pod
  failure (timeout / 5xx) lands in `_meta.json`'s `errors` map and flips
  `fetchComplete` to `false`; that's surfaced loudly (red banner in the
  UI, `INCOMPLETE FETCH` on the CLI) because a short window otherwise
  passes for the whole night. Don't swallow these.
- **Unreconcilable chunk (soft data-loss)** — a pod whose lines couldn't
  be proven complete (a >cap burst inside `MIN_SPLIT_S` that can't be
  split finer) lands in `_meta.json`'s **`incomplete_pods`** map (distinct
  from `errors`: no exception was raised) and *also* flips `fetchComplete`
  to `false`. Same loud surfacing. Implausible in practice, but it means
  "missing data" not "fetch failed" — keep the two maps distinct.
- **Empty per-pod output but the pod is in `listPods`** — Loki has a
  silent disagreement between the `series` index and the underlying
  blocks (rare but real). Treat as fetched and let the parser see an
  empty file.

## What you should *not* add

- **Streaming / `--tail`** support. The whole tool is snapshot-based;
  see "Non-goals" in [architecture/architecture.md](../../../architecture/architecture.md).
- **A content LogQL pipeline filter** like `|~ "..."` baked into the
  listPods or per-pod fetch. We deliberately fetch every pod's lines and
  filter *after* parsing — pre-filtering would bind the cache to a
  specific search and prevent superset reuse. (Note: `count_over_time`
  in `_countOverTime` is a *metric* aggregation, not a content filter —
  it doesn't fetch lines, so it doesn't bind the cache.) If you really
  want a content filter, build a *second* code path that uses uncacheable
  streaming queries, and document the divergence.
- **Per-stream splitting** via `{pod} | detected_level="X"` to dodge
  #17270 (each single response-stream paginates correctly). It's a valid
  *alternative* complete fix and uses fewer queries for huge pods, but it
  needs structured-metadata filtering plus a merge-sort across levels, so
  it was deferred in favour of the assumption-free time-chunker. Mentioned
  so you don't think it was overlooked — revisit only if chunk fan-out
  becomes a real performance problem.
- **`--retries`** on logcli. We rely on the user re-running on transient
  failures; the per-pod error map in `_meta.json` is enough signal.

## When working on parse-side timestamp / format issues

`parse._parseTimestamp` is the only function that knows the Loki
timestamp shape. If you spot a parse failure on a real fixture:

1. Capture the offending JSON line into [tests/data/](../../../tests/data/).
2. Add a regression test in
   [tests/test_parse.py](../../../tests/test_parse.py) — see the existing
   `test_parseTimestamp_*` tests for the pattern.
3. Update [architecture/parsing.md](../../../architecture/parsing.md) if
   the shape itself has shifted (e.g. Loki upgrade).
