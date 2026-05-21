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
| Fetch one pod's JSONL                  | `fetch._fetchOnePod(spec, pod, outPath)`                     |
| Whole-window fetch with caching        | `fetch.fetchAll(spec)`                                       |
| Find a reusable wider cache            | `fetch.findSupersetCache(cluster, ns, fromIso, toIso)`       |
| Parse Loki's timestamp string          | `parse._parseTimestamp(s)` (in `parse.py`, not `fetch.py`)   |

If you're tempted to call `subprocess.run(["logcli", ...])` directly, ask
why `_run_logcli` doesn't already do what you need.

## Connection / auth

- The cluster's Loki endpoint is `https://loki-query.ls.lsst.org`. There
  is no other one in scope; don't parameterise away from this until there
  is a real second target.
- HTTP basic auth: `--username=<user>` on the command line,
  `LOKI_PASSWORD` in the environment. `_run_logcli` refuses to spawn
  logcli if `LOKI_PASSWORD` is unset — surface that error to the user
  rather than guessing.
- The user is expected to have their password in their shell rc and to
  have sourced it before running the CLI; we do **not** support keyring
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
1. The per-stream `--limit` cap (50 000 by default in our `FetchSpec`)
   is per-query, not per-stream; a broad query truncates noisy pods
   first and silently drops events from the ones we care about.
2. The on-disk cache is naturally indexed by pod (one `.jsonl` per pod);
   per-pod queries map straight onto that layout with no post-processing.

If you ever change this, document the trade-off in
[architecture/caching.md](../../../architecture/caching.md).

### Required logcli flags for `query`

```
--quiet                  drop the API URL + common-labels preamble
-o jsonl                 one JSON object per line, with labels preserved
--forward                ascending time order; otherwise we'd need to
                         reverse the file before parsing
--limit=<N>              FetchSpec.lineLimit; default 50_000
--from --to              RFC3339Nano UTC strings ending in 'Z'
```

Drop any of these at your peril; tests assume the file is in `--forward`
order and that `labels.detected_level` is present.

### Timestamps

- `--from` / `--to` want RFC3339-ish strings; we format them via
  `cli._isoForLogcli` as `YYYY-MM-DDTHH:MM:SS.uuuuuuZ`. Always pass UTC
  with a trailing `Z`; don't supply a `+HH:MM` offset.
- Loki's response timestamps look like
  `"2026-05-20T09:45:46.216887282+01:00"` — i.e. **nanosecond**
  precision and **local-cluster offset** (the cluster runs in
  `Europe/London` summer time, so `+01:00`). `parse._parseTimestamp`
  trims the sub-microsecond digits before handing the string to
  `datetime.fromisoformat`. Don't strip the offset, don't lose precision.

## Parallelism

`FetchSpec.workers` (default 8) controls a `ThreadPoolExecutor`. 8–16 is
the useful range; beyond that you start hitting Loki ingestion-side
backpressure that manifests as occasional logcli timeouts (`_run_logcli`
catches and reports those per-pod, so other pods keep going).

If you raise `lineLimit` significantly above 50 000, raise the per-query
timeout in `_run_logcli` proportionally — 600 s is the current cap.

## Error modes you should expect

- **`logcli binary not found on PATH`** — surfaced as `FetchError`.
  Tell the user to `brew install grafana/grafana/logcli` or equivalent.
- **`logcli failed (rc=N): ...`** — we keep the first 500 chars of
  stderr; the most common non-fatal reason is a transient 502/504
  through nginx, retryable by re-running.
- **`logcli timed out`** — happens when a pod's `.jsonl` is huge or the
  cluster is busy. Drop `--workers` or narrow the window.
- **Empty per-pod output but the pod is in `listPods`** — Loki has a
  silent disagreement between the `series` index and the underlying
  blocks (rare but real). Treat as fetched and let the parser see an
  empty file.

## What you should *not* add

- **Streaming / `--tail`** support. The whole tool is snapshot-based;
  see "Non-goals" in [architecture/architecture.md](../../../architecture/architecture.md).
- **A LogQL pipeline filter** like `|~ "..."` baked into the listPods
  or per-pod fetch. We deliberately fetch every pod's lines and filter
  *after* parsing — pre-filtering would bind the cache to a specific
  search and prevent superset reuse. If you really want a server-side
  filter, build a *second* code path that uses uncacheable streaming
  queries, and document the divergence.
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
