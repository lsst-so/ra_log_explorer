# Caching

Loki queries are slow (~60–90 s for one rapid-analysis namespace × 5 min
window with 8 parallel workers; ~minutes for a full dayObs); the same
exposure / night is often re-examined many times. The cache turns
repeat runs into instant loads.

## On-disk layout

```
~/.cache/ra_log_explorer/                          ← $RA_LOG_EXPLORER_CACHE; deployed, this is
│                                                    the mount point of the cache volume
├── _cache_schema_version.txt                      ← schema the cache was built with; a
│                                                    mismatch flushes the whole tree at startup
├── exposure-times/                                ← persistent dataId → ConsDB exposure record
│   ├── summit.json                                  (obs_end + filter/exp time/img type/…), split
│   └── bts.json                                     per site so a colliding dataId between scopes
│                                                    can't return the wrong record
└── <cluster>/<namespace>/
    └── <fromSlug>__<toSlug>/                      ← one directory per window
        ├── _meta.json                             ← FetchSpec + fetchSchemaVersion + per-pod
        │                                            byte/line counts + count_over_time oracle +
        │                                            fetchComplete + errors + incomplete_pods +
        │                                            pod_event_lines + event_errors
        ├── _live.json                             ← (live night dirs only) watermark + per-pod
        │                                            byte counts + event-only names + any
        │                                            in-flight pod rewrite; marks the dir as
        │                                            poller-built — sliced on demand, never
        │                                            superset-parsed, never fetched into
        ├── pods.txt                               ← pods that emitted in the window
        ├── _last_viewed.txt                       ← ISO timestamp; sidecar for LRU eviction
        ├── _exposure_ids.txt                      ← (exposure caches only) ascending dataIds
        │                                            that triggered fetches landing here
        ├── _range.txt                             ← (range caches only) startId, stopId,
        │                                            instrument — one per line
        ├── pods/<pod>.jsonl                       ← raw Loki JSONL (app logs), --forward order
        ├── pods_events/<pod>.jsonl                ← raw Loki JSONL (k8s/events lifecycle
        │                                            stream); auxiliary, never gates
        │                                            fetchComplete; absent for pods with no events
        ├── .partial                               ← present only while a fetch is in progress
        └── pods=<regex-slug>/                     ← (night caches only) one subdir per Loki
                                                     podRegex; mirrors the layout above and
            ├── _meta.json                           gets its own _meta.json + pods/.
            ├── pods.txt
            ├── _last_viewed.txt
            ├── pods/<pod>.jsonl
            ├── pods_events/<pod>.jsonl
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

`exposure-times/` lives at the root, not under the cluster/namespace
tree, so it survives `rm -rf ~/.cache/ra_log_explorer/<cluster>` but
disappears with a full root wipe.

Deployed, the whole tree sits on a PersistentVolumeClaim. That is
deliberate rather than incidental: fetching a night out of Loki takes
minutes, so an emptyDir would throw the cache away on every pod restart —
worst exactly when someone is restarting things in order to investigate
something. It also means the cache is *shared*: a window one person
fetched is instant for the next person to ask for it.

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
(see *Completeness* below); v4 additionally fetches each pod's
`k8s/events` lifecycle stream into `pods_events/` — a v3 cache has no such
tree, so the bump forces a re-fetch to pick up the new markers; v5
dropped every compatibility reader (see *No backwards compatibility*
below) — the flush is what makes that safe.

## No backwards compatibility

**Nothing written by an older build is ever interpreted.** This is app
code, not a library, and the cache is a temporary convenience, not a
data store anyone supports: every on-disk shape here — `_meta.json`,
`_live.json`, `_range.txt`, the exposure-time records — has exactly one
current format, readers treat anything else as absent or malformed, and
a deploy invalidates everything on purpose (that is what the
schema-version flush is *for*). When a format changes, bump
`CACHE_SCHEMA_VERSION` in the same commit and delete the old reader —
never write a tolerant one, never migrate in place. The cost is one
re-fetch per window after a deploy; the alternative is a permanent tax
of dual-format readers whose legacy halves are exercised by nothing and
rot silently.

## Live night dirs

Live mode (see [architecture.md](architecture.md#live-mode)) maintains
one additional kind of window dir: the **live night dir**, the ordinary
all-pods window path for the current dayObs's noon→noon span, grown
incrementally by the poller rather than written once by a fetch. It is
marked by a `_live.json` sidecar (full schema in `fetch.py`) recording
the *watermark* — the time up to which every pod's lines are durably on
disk — per-pod byte/line counts, and the cumulative fall-short maps.
The sidecar is the coordination point between the poller (single
writer, atomic replace once per tick, only after the tick's bytes hit
disk) and readers; bytes beyond the recorded counts are treated as not
yet there. That contract holds because an append only ever *extends* a
file, and because appends are all-or-nothing: a partial one — app log
or events stream alike — is truncated back to the pre-append size, so
the span the poller retries next tick lands after clean bytes rather
than after a torn line the counts will later be extended over.

One operation is not an append, and so has to announce itself:
finalisation's whole-pod refetch swaps the file wholesale, and while it
does the sidecar carries `rewritingPod: "<pod>"`. `_tryNightSlice`
refuses to slice a night carrying it and falls back to a real fetch.
Zeroing the pod's counts is not enough on its own — a slicer reading a
zeroed record doesn't wait, it omits the pod, and the resulting slice
would be written `fetchComplete: true` with a pod missing and then
exact-hit forever. The key is cleared on failure as well as success, so
a failed refetch leaves a night that is visibly short rather than
unsliceable.

Three rules keep it coherent with everything else here:

- While active it has **no `_meta.json`**, so nothing mistakes it for a
  completed fetch and LRU eviction leaves it alone. Finalisation (at
  noon-UTC rollover: final top-up, then a per-pod `count_over_time`
  audit that refetches any pod falling short by more than the oracle's
  dedup slack — `max(100, 0.2%)`, `live.VERIFY_TOLERANCE_*`; a pod
  within tolerance is left alone) writes a normal `_meta.json`, after
  which it is listed, evictable, and deletable like any window.
- It is **excluded from superset reuse** forever (the sidecar stays
  after finalisation as the marker): handing a whole night to the
  parser to answer a five-minute window takes minutes.
- Contained windows are instead served by **slicing**
  (`materializeNightSlice`): each per-pod JSONL is time-ascending, so
  the request's boundary offsets are found by binary-searching
  timestamps and the byte range is copied into an ordinary exposure
  cache dir at the requested window's own path, with a synthesized
  `_meta.json` (`cacheReuse: "night-slice"`, `sliceSource` pointing
  back at the night dir, the night's fall-short maps inherited
  wholesale). The result is a first-class window: later identical
  requests exact-hit it, nearby ones superset-reuse it, LRU eviction
  reclaims it.

## Cache hit policy

`fetchAll(spec, ...)` runs the whole of the following under a
**per-window write lock** (`windowWriteLock`, keyed on the requested
window directory). The clamped night slice (step 2½) additionally
locks the slice's *destination* — the clamped `[nightStart, watermark]`
window is not the requested path, and a direct fetch of that same
window locks it as its own; the locks are re-entrant so the common
target-is-the-requested-dir case costs nothing. A cache window is a directory of files plus a
`_meta.json` vouching for them; two threads asking for the same window
would otherwise both find no cache and both write the same
`pods/<pod>.jsonl`. The second caller waits and then takes the first's
result as an ordinary hit — never longer than the fetch it would have
duplicated. It then decides in order:

1. **Skip the cache entirely** if either `forceRefresh=True` or the
   requested window's `to` is in the future. (The cluster might still
   produce new logs inside the window, so the cache would be stale.)
   This is the safety net for an in-progress dayObs in night mode.

2. **Exact hit** if `<requestedDir>/_meta.json` exists, parses, carries
   the current `fetchSchemaVersion`, and there's no `.partial` flag.
   Returns the requested directory and the saved meta with
   `cacheReuse = "exact"`. A meta that won't parse means exactly what a
   missing one means — no usable cache, fall through — rather than an
   exception: one truncated file (a full disk, a killed writer) would
   otherwise 500 every future request for that window, with no way out
   but finding and deleting the directory by hand. The superset step
   below reads the same way, since the directory it picked can be
   deleted or rewritten between the search and the read.

2½. **Night slice** if the request falls inside a live/finalised night
   dir. Two windows qualify: one the watermark fully covers (its `to`
   at or before the watermark — all-pods *and* `podRegex` requests
   alike; a filtered request slices only matching pods into the nested
   `pods=` dir a real filtered fetch would use), and the night's *own*
   window while the night is in progress (night mode on the current
   dayObs), which is served clamped to the watermark — "the night so
   far". This step deliberately sits **outside** the window-in-the-past
   gate so the in-progress night qualifies; only `forceRefresh`
   disables it. The window is materialized by slicing (see *Live night
   dirs*) and returned with `cacheReuse = "night-slice"`; a repeat
   against an unchanged watermark reuses the previous slice as an exact
   hit. When the window to slice *is* the night's own — the watermark
   has reached night end but finalisation hasn't run, which is where
   `--live-day-obs` parks permanently — the night dir is returned
   directly instead, since it already is that window. Any slice failure
   falls through to the steps below (leaving `.partial` in place, so a
   half-copied window can never pass for a hit) — worst case is the
   fetch that would have happened anyway.

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
   if set), drop any pod files a previous attempt on this window left
   behind (`summarizeAll` reads the directory, not `pods.txt`, so a
   stale file would be parsed as part of the window), fetch each pod in
   parallel in count-presized single-batch chunks (every line, verified
   — see *Completeness* below), write `_meta.json`, remove `.partial`.
   Returns the requested directory with `cacheReuse = "none"`. A
   requested directory carrying a `_live.json` is refused outright: it
   belongs to the poller, and a fresh fetch would truncate files it is
   appending to.

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
  `CHUNK_TARGET_LINES` (< the batch size) each. A window the oracle says
  is **empty** is skipped without a query at all, so the range selector
  is deliberately padded (rounded up, plus 1 ms) to be a strict superset
  of the fetched window: the selector covers `(to - range, to]` while the
  fetch covers `[from, to)`, and an exactly-sized range would sit
  narrower at the left edge. Over-counting only ever costs an empty
  query; under-counting would silently skip a live chunk.
- **Fetch one batch and verify structurally.** Each chunk is fetched with
  `--batch=SERVER_QUERY_CAP` (set equal to the cluster's
  `max_entries_limit_per_query`). A chunk is trusted **only** when
  `got < SERVER_QUERY_CAP`: that proves it completed in a single
  un-paginated request, so the cursor-advance bug never fired. The
  oracle is just an accelerator — correctness rests on this check, not on
  the count being right (if the count is unavailable the chunker falls
  back to blind time-bisection and is still correct).
- **Split and retry.** A chunk that comes back *full* (`got ≥` the cap)
  is discarded untrusted and re-fetched as equal-time half-open
  sub-windows, which tile it exactly — no gap, no dup. How many is
  again the oracle's call: `min(MAX_SPLIT_PARTS, max(2,
  ceil(expected / CHUNK_TARGET_LINES)))`, i.e. enough parts to aim at
  `CHUNK_TARGET_LINES` each, capped at `MAX_SPLIT_PARTS` (60) so a
  pathological count can't fan out into hundreds of children at once.
  Two halves is the floor, and what a dark oracle always gives. This
  recurses until every piece fits one batch, keeping the output
  globally time-ascending so the parser's ordering assumption holds.
  (A count that already predicts an overflow skips the doomed fetch and
  splits up front.)
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
sanity-check lines-written against what Loki says was there.

The `k8s/events` lifecycle pass is deliberately **outside** this
completeness contract: it's a low-volume single-batch query per pod (no
chunking — restart/kill/OOM events number in the handful, never thousands),
its line counts land in `_meta.json`'s `pod_event_lines`, and any per-pod
failure lands in `event_errors` — but neither flips `fetchComplete`. A gap
in the auxiliary lifecycle stream is "no markers for that pod", not
"missing data for the window". Consumers
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
a cache hit even if `_meta.json` is somehow present. Nothing sweeps
stale flags, but nothing needs to: the next request for that same
window finds no usable cache and re-fetches over it, clearing the flag.
`--force-refresh` is only needed when a *superset* window would be
reused instead, so the flagged directory is never revisited.

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
  window without needing the in-memory state to already exist. The
  ids are *bare* (no instrument), so on a colliding id the file can
  name the other instrument's window — the rebuild path guards this
  by requiring the found window to contain the instrument-pinned t₀
  (see *dataId / expId* in
  [architecture.md](architecture.md#key-concepts)).

- **`_range.txt`** — range caches only. Three lines: `startId`,
  `stopId`, then the `instrument` the range was fetched under. Written
  by `markCacheRange` when a range fetch completes. This is the sole
  marker that tells the `/api/cache` listing to label the window
  `kind: "range"` (rather than `"exposure"`) and deep-link it back to
  `/?rangeStart=…&rangeStop=…`; `_loadRangeFromCache` uses it to
  rehydrate a `RangeState` from disk on a reload / deep link, and the
  recorded instrument re-pins the rebuilt state's shutter-close
  lookups — a range is a run of *one* instrument's exposures, and a
  bare lookup on a colliding id would anchor that exposure to the
  other instrument's t₀. Three lines is the *only* format: a shorter
  file is malformed and the dir simply isn't a range cache (see *No
  backwards compatibility* below).

## LRU eviction (size cap)

`fetch.evictToFit(maxBytes, exempt=…)` removes the least-recently-
viewed caches until the on-disk total is at or below `maxBytes`,
exempting whichever caches the caller passes in (the just-fetched
one, typically).

- `maxBytes` is `config.MAX_CACHE_BYTES`, from
  `$RA_LOG_EXPLORER_MAX_CACHE_BYTES` (5 GiB if unset). Deployed, the
  chart derives it from the size of the volume provisioned for the cache
  rather than setting it separately, so the app cannot come to believe it
  has more room than the PVC actually gives it. It is not settable at
  runtime and there is no UI for it — see
  [architecture.md](architecture.md#configuration).
- Order: `(last-viewed ASC, dir name)`. Caches without a
  `_last_viewed.txt` sidecar are treated as oldest — they've never
  been opened, so they're the safest to drop.
- The eviction pass runs as part of the post-fetch callback
  (`_onFetchComplete`), exempting the just-fetched cache. Brief
  over-cap states during a fetch are acceptable.
- Empty per-cluster, per-namespace, and (for night mode) per-window
  parent directories are pruned as their child windows go away.
- **Not everything on the volume is evictable.** The total is measured
  over the whole tree, but only directories carrying a `_meta.json` are
  candidates — so the in-progress live night (which has none until
  finalisation) and `exposure-times/` count against the cap without
  ever being reclaimable. Eviction makes one pass and stops, so a cap
  set below that floor doesn't spin; it just evicts every *other*
  window and stays over. With live mode on, leave the cap comfortably
  above one night's ~9 GiB, which is what deriving it from the volume
  size already does.

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
  window is roughly 40–50 MiB; an AOS night cache is GiB-scale; a
  live-built all-pods night is ~9 GiB (measured: 35.7M lines / 576
  pods for dayObs 20260711).

Cache management lives in both the CLI (`python3 -m
ra_log_explorer.cli cache info|flush`) and the browser's admin view at
`/?admin=1` (the cached-windows table, with a per-row ✕ delete and a
"flush entire cache" button).

Every path that removes cache trees — both browser deletes *and* LRU
eviction — unlinks every `_live.json` under the target *before*
removing the tree. A partial delete is a real possibility —
the poller may be creating files in there as `rmtree` walks it — and
one that took the pod files but left the sidecar is worse than either
clean outcome: the poller's intactness check would pass, it would
resume appending to files that now begin mid-night, and every slice cut
from them would be short while claiming to be whole. Without the
sidecar a half-deleted night is simply not a live night dir, and the
poller opens it again from scratch.

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

Within a site it is keyed **twice**, because an id isn't unique there
either — `SSSSS` is a per-instrument sequence number, so LSSTCam and
LATISS share ids on any night both observe:

- `"<instrument>:<id>"` — the unambiguous entry. An instrument-scoped
  lookup accepts only this key; falling back to the bare one would
  return a different exposure that happens to share the id.
- `"<id>"` — the *probe-order* entry: what an unqualified lookup
  resolves to, i.e. whichever of `INSTRUMENTS_BY_PROBE_ORDER` has the
  row first. Only writers that resolved the id that way may write it
  (`storeCachedRecord[s]`'s `bareKey`, `storeCachedRecordList`'s
  `probeOrderWinners`), so the same dataId can't answer differently
  depending on who wrote last.

The lookup is best-effort: a corrupt JSON file, an unexpected schema,
or an unusable value all return `None` from `lookupCachedRecord` and
fall through to a fresh ConsDB query (which then overwrites the bad
record). There is exactly one entry shape — a record object; anything
else is a miss, never interpreted (see *No backwards compatibility*
below). The store path is the same on every cache hit, miss, and
batch-resolve, so `rm -r <cache_root>/exposure-times/` is the nuclear
reset.

## Future: cleaner subset semantics

The current superset reuse picks the smallest cached superset. We
could go further:

- **Patch reuse**: if a partial superset exists (some files missing
  or truncated), top it up rather than re-fetching from scratch.
- **Extension reuse**: re-use an existing cache and only fetch the
  new time slice when widening the window slightly.

Neither is implemented; both are easy if they become necessary.
Don't build them until there's a concrete use case.
