# Caching

Loki queries are slow (~60–90 s for one rapid-analysis namespace × 5 min
window with 8 parallel workers; ~minutes for a full dayObs); the same
exposure / night is often re-examined many times. The cache turns
repeat runs into instant loads.

## On-disk layout

```
~/.cache/ra_log_explorer/                          ← override with $RA_LOG_EXPLORER_CACHE
├── settings.json                                  ← server-side settings (maxCacheBytes)
├── exposure-times.json                            ← persistent dataId → shutter-close (TAI) cache
└── <cluster>/<namespace>/
    └── <fromSlug>__<toSlug>/                      ← one directory per window
        ├── _meta.json                             ← FetchSpec + per-pod byte counts
        ├── pods.txt                               ← pods that emitted in the window
        ├── _last_viewed.txt                       ← ISO timestamp; sidecar for LRU eviction
        ├── _exposure_ids.txt                      ← (exposure caches only) ascending dataIds
        │                                            that triggered fetches landing here
        ├── pods/<pod>.jsonl                       ← raw Loki JSONL, --forward order
        ├── .partial                               ← present only while a fetch is in progress
        └── pods=<regex-slug>/                     ← (night caches only) one subdir per Loki
                                                     podRegex; mirrors the layout above and
            ├── _meta.json                           gets its own _meta.json + pods/.
            ├── pods.txt
            ├── _last_viewed.txt
            ├── pods/<pod>.jsonl
            └── .partial
```

Slugs are the `--from`/`--to` ISO strings with `:` removed and `.` →
`_` (filesystem-friendly). The directory naming is therefore a stable
function of the window.

Night-mode caches nest one extra level (`pods=<regex-slug>`) below the
window dir, so a same-window exposure-mode (all pods) and night-mode
(AOS-only) cache can coexist on disk without clobbering each other.

`exposure-times.json` and `settings.json` live at the root, not under
the cluster/namespace tree, so they survive `rm -rf
~/.cache/ra_log_explorer/<cluster>` but disappear with a full root
wipe.

## Cache hit policy

`fetchAll(spec, ...)` decides in order:

1. **Skip the cache entirely** if either `forceRefresh=True` or the
   requested window's `to` is in the future. (The cluster might still
   produce new logs inside the window, so the cache would be stale.)
   This is the safety net for an in-progress dayObs in night mode.

2. **Exact hit** if `<requestedDir>/_meta.json` exists and there's no
   `.partial` flag. Returns the requested directory and the saved
   meta with `cacheReuse = "exact"`.

3. **Superset hit** if any other completed cache directory under
   `<cluster>/<namespace>/` (or under `<cluster>/<namespace>/<window>/
   pods=<slug>/` for night mode) has a window that fully contains the
   requested one **and the same `podRegex`**. The *smallest* such
   superset wins. Returns that directory and meta with
   `cacheReuse = "superset"` plus `cacheReusePath`.

   Cross-mode reuse is forbidden: an exposure-mode (`podRegex=None`)
   cache is NEVER a valid superset for a night-mode
   (`podRegex=".*aos.*"`) request, and vice-versa. Their on-disk pod
   sets are different — pretending they aren't would silently serve
   incomplete data.

4. Otherwise **fetch fresh**: create the requested directory, write
   `.partial`, list pods via `logcli series` (honouring `podRegex`
   if set), fetch each pod in parallel with
   `--limit=DEFAULT_LINE_LIMIT --forward`, write `_meta.json`, remove
   `.partial`. Returns the requested directory with
   `cacheReuse = "none"`.

## Why supersets are safe

When the cached series query was `{cluster=…,namespace=…,pod=~<regex>}`
over `[C, D]`, it returned every pod matching the (optional) regex
that emitted *any* log in that window. For any inner window
`[A, B] ⊆ [C, D]`, the set of matching pods emitting in `[A, B]` is a
subset of the cached set — so re-listing would return no new pods. The
per-pod JSONL files cover the full `[C, D]` window, so they contain
every line that would appear in `[A, B]` too.

The only price of superset reuse is extra context: parsing sees
events outside the user's nominal window. The timeline / detail UI
shows events at their true offset relative to t-zero (or
night-start), so this manifests simply as a wider visible range;
nothing is mis-attributed.

## The `.partial` flag

`.partial` is written before pods are enumerated and removed after
the last pod file is on disk. A crashed/Ctrl-C'd fetch leaves the
flag in place, and `fetchAll` then refuses to treat the directory as
a cache hit even if `_meta.json` is somehow present. There's no
automatic recovery — re-run with `--force-refresh` to overwrite.

## Per-cache sidecars

- **`_last_viewed.txt`** — ISO timestamp of the most-recent time
  the user opened this cache (either via a fresh fetch or via
  `/api/summary?dataId=` / `?dayObs=` against an already-loaded
  state). Written best-effort by `markCacheViewed`; absent caches
  are treated as "never opened" (epoch zero) for LRU purposes.

- **`_exposure_ids.txt`** — exposure caches only. Ascending list of
  the dataIds that have ever triggered a fetch landing on this
  cache. One window can serve many dataIds via superset reuse, and
  the `/api/cache` listing surfaces all of them as clickable
  shortcuts. Written by `addExposureToCache`; deduped and sorted on
  every write.

## LRU eviction (size cap)

`fetch.evictToFit(maxBytes, exempt=…)` removes the least-recently-
viewed caches until the on-disk total is at or below `maxBytes`,
exempting whichever caches the caller passes in (the just-fetched
one, typically).

- The default `maxBytes` lives in `appSettings.json`'s
  `maxCacheBytes` field (5 GiB by default). The user can change it
  via `PUT /api/settings`.
- Order: `(last-viewed ASC, dir name)`. Caches without a
  `_last_viewed.txt` sidecar are treated as oldest — they've never
  been opened, so they're the safest to drop.
- The eviction pass runs as part of the post-fetch callback
  (`_onFetchComplete`), exempting the just-fetched cache. Brief
  over-cap states during a fetch are acceptable.
- Empty per-cluster, per-namespace, and (for night mode) per-window
  parent directories are pruned as their child windows go away.

## What's stable across reruns

- The cache directory path is determined solely by
  `(cluster, namespace, fromIso, toIso, podRegex)`. Different
  t-zero values can share the same cache directory if they produce
  the same window.

- `_meta.json` records the original `FetchSpec` it was fetched
  with, so a superset cache directory can be inspected to see why
  it was created and which mode (exposure vs night) it serves.

- The per-pod JSONL files are byte-for-byte what Loki returned, so
  re-parsing always produces the same events.

## When to flush

- After a regex change in `parse.py` whose effect you can't
  reproduce with `--force-refresh` alone (rare — `parse.py` reads
  from cache, so fresh parsing happens on every fetch run).

- When testing fetch behaviour itself (e.g. the partial-flag
  handling).

- When disk pressure matters above the configured cap. (Below the
  cap, LRU eviction handles it automatically.) Each exposure
  window is roughly 40–50 MiB; a night cache is GiB-scale.

Cache management lives in both the CLI (`python3 -m
ra_log_explorer.cli cache info|flush`) and the home page in the
browser (cache table with per-row ✕ delete, plus a "delete all"
button).

## The exposure-time cache

`<cache_root>/exposure-times.json` maps `dataId → obs_end ISO (TAI)`
across runs. It's the only thing in the cache root that lives outside
the cluster/namespace tree, because:

- exposure end-times are immutable once `cdb_<instrument>.exposure`
  has a row, so the cache has zero staleness concerns;
- one machine should not be re-querying ConsDB for the same dataId
  ever, even across log-explorer sessions;
- a cached dataId resolves with no token and no network, which is
  what makes the home page work for users who don't have an RSP
  token configured.

The lookup is best-effort: a corrupt JSON file, an unexpected schema,
or a non-string value all return `None` from `lookupCached` and fall
through to a fresh ConsDB query (which then overwrites the bad
record). The store path is the same on every cache hit, miss, and
batch-resolve, so `rm <cache_root>/exposure-times.json` is the
nuclear reset.

## Future: cleaner subset semantics

The current superset reuse picks the smallest cached superset. We
could go further:

- **Patch reuse**: if a partial superset exists (some files missing
  or truncated), top it up rather than re-fetching from scratch.
- **Extension reuse**: re-use an existing cache and only fetch the
  new time slice when widening the window slightly.

Neither is implemented; both are easy if they become necessary.
Don't build them until there's a concrete use case.
