# Caching

Loki queries are slow (~60–90 s for one rapid-analysis namespace × 5 min
window with 8 parallel workers; ~minutes for a full dayObs); the same
exposure / night is often re-examined many times. The cache turns
repeat runs into instant loads.

## On-disk layout

```
~/.cache/ra_log_explorer/                          ← override with $RA_LOG_EXPLORER_CACHE
├── _cache_schema_version.txt                      ← schema the cache was built with; a
│                                                    mismatch flushes the whole tree at startup
├── settings.json                                  ← server-side settings (maxCacheBytes)
├── exposure-times/                                ← persistent dataId → ConsDB exposure record
│   ├── summit.json                                  (obs_end + filter/exp time/img type/…), split
│   └── bts.json                                     per site so a colliding dataId between scopes
│                                                    can't return the wrong record
└── <cluster>/<namespace>/
    └── <fromSlug>__<toSlug>/                      ← one directory per window
        ├── _meta.json                             ← FetchSpec + fetchSchemaVersion + per-pod
        │                                            byte/line counts + count_over_time oracle +
        │                                            fetchComplete + errors + incomplete_pods
        ├── pods.txt                               ← pods that emitted in the window
        ├── _last_viewed.txt                       ← ISO timestamp; sidecar for LRU eviction
        ├── _exposure_ids.txt                      ← (exposure caches only) ascending dataIds
        │                                            that triggered fetches landing here
        ├── _range.txt                             ← (range caches only) two lines: startId, stopId
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

Range-mode caches are structurally identical to exposure caches — one
wide all-pods window, no `pods=` subdir — and are distinguished only by
the presence of a `_range.txt` sidecar recording their `[startId,
stopId]` bounds. Because they're plain all-pods windows, they also
participate in superset reuse: a later single-exposure fetch whose
window falls inside the range gets served from the range cache for free.

`exposure-times/` and `settings.json` live at the root, not under
the cluster/namespace tree, so they survive `rm -rf
~/.cache/ra_log_explorer/<cluster>` but disappear with a full root
wipe.

## Schema-version flush

`CACHE_SCHEMA_VERSION` (in `fetch.py`) is bumped whenever a change to
*how* we fetch makes older caches untrustworthy. On top of the per-cache
`fetchSchemaVersion` guard (below), bumping it triggers a one-shot flush
of the **entire** cache tree:

- `_cache_schema_version.txt` at the cache root records the version the
  contents were written with.
- `ensureCacheSchemaCurrent()` runs once at process start (from
  `cli.main()`). If the sentinel is missing or doesn't match the current
  version, it deletes every top-level entry under the root (the
  cluster trees, plus `exposure-times/`) and rewrites the sentinel,
  printing a one-line notice so the user understands why the next loads
  re-fetch.

This is deliberately heavier than the per-cache guard, which only
re-fetches stale windows lazily — one surprise-slow load at a time —
and leaves untrusted bytes on disk in between. The flush makes the cost
explicit and up front and guarantees nothing written by an older
(possibly line-dropping) fetch path survives anywhere. The version
history: v1 (implicit) capped each pod at 50 000 lines; v2 used
`--limit=0` but still silently dropped lines on wide busy windows
(grafana/loki#17270); v3 fetches in count-presized single-batch chunks
(see *Completeness* below).

## Cache hit policy

`fetchAll(spec, ...)` decides in order:

1. **Skip the cache entirely** if either `forceRefresh=True` or the
   requested window's `to` is in the future. (The cluster might still
   produce new logs inside the window, so the cache would be stale.)
   This is the safety net for an in-progress dayObs in night mode.

2. **Exact hit** if `<requestedDir>/_meta.json` exists, carries the
   current `fetchSchemaVersion`, and there's no `.partial` flag.
   Returns the requested directory and the saved meta with
   `cacheReuse = "exact"`.

3. **Superset hit** if any other completed cache directory under
   `<cluster>/<namespace>/` (or under `<cluster>/<namespace>/<window>/
   pods=<slug>/` for night mode) carries the current
   `fetchSchemaVersion`, has the same `podRegex`, and a window that
   fully contains the requested one. The *smallest* such superset wins.
   Returns that directory and meta with `cacheReuse = "superset"` plus
   `cacheReusePath`.

   Cross-mode reuse is forbidden: an exposure-mode (`podRegex=None`)
   cache is NEVER a valid superset for a night-mode
   (`podRegex=".*aos.*"`) request, and vice-versa. Their on-disk pod
   sets are different — pretending they aren't would silently serve
   incomplete data.

4. Otherwise **fetch fresh**: create the requested directory, write
   `.partial`, list pods via `logcli series` (honouring `podRegex`
   if set), fetch each pod in parallel in count-presized single-batch
   chunks (every line, verified — see *Completeness* below), write
   `_meta.json`, remove `.partial`. Returns the requested directory
   with `cacheReuse = "none"`.

Steps 2 and 3 ignore any cache whose `fetchSchemaVersion` doesn't match
the current `CACHE_SCHEMA_VERSION`. That's what keeps a stale snapshot
written by an older, possibly-truncating version of the tool from being
silently re-served — it re-fetches instead.

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

## Completeness (never silently truncate)

A fetch must return the **whole** window — for a full night, every line
of every pod. A naïve `logcli query --limit=0` does **not** achieve this:
it silently drops lines on wide, busy windows (grafana/loki#17270 — when
a response spans multiple Loki streams, logcli advances the next batch's
cursor by the *global* max timestamp, so a busier stream's tail falls
into a 1 ns dedup gap and vanishes). A single `{pod=…}` is already ≥2
streams by `detected_level`, so a 24 h AOS night came back ~9 % short
while still reporting itself complete. (The even-older behaviour capped
each pod at 50 000 lines, tail-dropping the busiest pods outright.)

The fix turns on one structural fact: **the loss only happens when a
query paginates** — i.e. when some batch comes back *full* and logcli
asks for another. So `_fetchOnePod` builds each pod's output entirely out
of *single-batch* queries:

- **Presize by the oracle.** `count_over_time` is a server-side
  aggregation — it never paginates entries, so it is exact and immune to
  the bug. `_countOverTime` uses it to learn how many lines a window
  holds and split the time range into chunks targeting
  `CHUNK_TARGET_LINES` (< the batch size) each.
- **Fetch one batch and verify structurally.** Each chunk is fetched with
  `--batch=SERVER_QUERY_CAP` (set equal to the cluster's
  `max_entries_limit_per_query`). A chunk is trusted **only** when
  `got < SERVER_QUERY_CAP`: that proves it completed in a single
  un-paginated request, so the cursor-advance bug never fired. The
  oracle is just an accelerator — correctness rests on this check, not on
  the count being right (if the count is unavailable the chunker falls
  back to blind time-bisection and is still correct).
- **Split and retry.** A chunk that comes back *full* (`got ≥` the cap)
  is discarded untrusted and re-fetched as two half-open time halves
  (`[a, mid) ∪ [mid, b)` tiles exactly — no gap, no dup). This recurses
  until every piece fits one batch, keeping the output globally
  time-ascending so the parser's ordering assumption holds.
- **Floor.** If a window is already `≤ MIN_SPLIT_S` wide and *still*
  overflows a batch (an implausible >5000-line burst in ≤1 s), it can't
  be fetched losslessly; we keep what we got and flag the pod rather than
  lie.

A fetch can therefore come up short in two distinct ways, both recorded
in `_meta.json` and both setting `fetchComplete=False`:

- `errors` (`{pod: message}`) — a hard logcli failure (timeout,
  transient 5xx). A partial file left by a query that died mid-flight is
  kept (bytes counted) but the pod is flagged.
- `incomplete_pods` (`{pod: reason}`) — a pod whose chunks couldn't be
  reconciled to the single-batch guarantee (the floor case above).

`fetchComplete` is `True` iff **both** maps are empty. `_meta.json` also
carries `pod_lines` (lines written per pod) and `pod_expected` (the
`count_over_time` oracle per pod, when available) so a human or test can
sanity-check lines-written against what Loki says was there. Consumers
surface an incomplete fetch loudly rather than letting a partial window
pass for the whole night:

- **Browser** — a red banner at the top of the explore and night views
  (`renderFetchBanner` in `app.js`) merges both maps and lists the pods.
- **CLI** — `_warnIfIncompleteFetch` prints an unmissable stderr warning
  after any fetch (cache hit included).

When the *shape* of what makes a cache trustworthy changes (as the
`--limit=0` → chunked-fetch switch did), bump `CACHE_SCHEMA_VERSION`:
older caches re-fetch instead of being re-served (per-cache guard), and
the whole tree is flushed once at startup (see *Schema-version flush*).

## The `.partial` flag

`.partial` is written before pods are enumerated and removed after
the last pod file is on disk. A crashed/Ctrl-C'd fetch leaves the
flag in place, and `fetchAll` then refuses to treat the directory as
a cache hit even if `_meta.json` is somehow present. There's no
automatic recovery — re-run with `--force-refresh` to overwrite.

## Per-cache sidecars

- **`_last_viewed.txt`** — ISO timestamp of the most-recent time
  the user opened this cache (either via a fresh fetch, via
  `/api/summary?dataId=` / `?dayObs=` against an already-loaded
  state, or via the on-demand cache rebuild path). Written
  best-effort by `markCacheViewed`; absent caches are treated as
  "never opened" (epoch zero) for LRU purposes.

- **`_exposure_ids.txt`** — exposure caches only. Ascending list of
  the dataIds that have ever triggered a fetch landing on this
  cache. One window can serve many dataIds via superset reuse, and
  the `/api/cache` listing surfaces all of them as clickable
  shortcuts. Written by `addExposureToCache`; deduped and sorted on
  every write. The on-demand cache rebuild path in `/api/summary`
  uses this file to map a deep-linked dataId back to its cache
  window without needing the in-memory state to already exist.

- **`_range.txt`** — range caches only. Two lines, `startId` then
  `stopId`. Written by `markCacheRange` when a range fetch completes.
  This is the sole marker that tells the `/api/cache` listing to label
  the window `kind: "range"` (rather than `"exposure"`) and deep-link
  it back to `/?rangeStart=…&rangeStop=…`; `_loadRangeFromCache` uses it
  to rehydrate a `RangeState` from disk on a reload / deep link.

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

- **Automatic** — bumping `CACHE_SCHEMA_VERSION` flushes the whole
  tree at the next startup (see *Schema-version flush*). You don't
  flush by hand for a fetch-method change; you bump the version.

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

`<cache_root>/exposure-times/<siteName>.json` maps `dataId → curated
ConsDB exposure record` across runs, split per site. Each record is the
`EXPOSURE_RECORD_COLUMNS` projection of the row — `obs_end` (the
shutter-close TAI t-zero) plus the human-facing properties (filter, exp
time, image type, program, reason, group/index, pointing, seeing) that
feed the explore-view info box and the dataId-link tooltips. It lives
outside the cluster/namespace tree because:

- exposure properties are immutable once `cdb_<instrument>.exposure`
  has a row, so the cache has zero staleness concerns;
- one machine should not be re-querying ConsDB for the same dataId
  ever, even across log-explorer sessions;
- a cached dataId resolves with no token and no network, which is
  what makes the home page work for users who don't have an RSP
  token configured.

The cache is split per site (`exposure-times/summit.json`,
`exposure-times/bts.json`) because a 13-digit dataId is meaningful
*within* a site, not globally: BTS simulated exposures can carry an
id that also exists on the summit, with a completely different record.
`lookupCachedRecord(..., siteName=...)` and `storeCachedRecord[s](...,
siteName=...)` enforce the split at every call site, so the wrong-site
value can never leak in.

The lookup is best-effort: a corrupt JSON file, an unexpected schema,
or an unusable value all return `None` from `lookupCachedRecord` and
fall through to a fresh ConsDB query (which then overwrites the bad
record). A legacy entry written by the pre-record format (a bare
`obs_end` string per dataId) is read back as a 1-field record, so an
existing cache keeps resolving t-zeros across the upgrade — the richer
columns just backfill on the next fresh query. The store path is the
same on every cache hit, miss, and batch-resolve, so `rm -r
<cache_root>/exposure-times/` is the nuclear reset.

## Future: cleaner subset semantics

The current superset reuse picks the smallest cached superset. We
could go further:

- **Patch reuse**: if a partial superset exists (some files missing
  or truncated), top it up rather than re-fetching from scratch.
- **Extension reuse**: re-use an existing cache and only fetch the
  new time slice when widening the window slightly.

Neither is implemented; both are easy if they become necessary.
Don't build them until there's a concrete use case.
