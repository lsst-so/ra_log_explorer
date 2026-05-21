# Caching

Loki queries are slow (60–90 s for one rapid-analysis namespace × 5 min
window with 16 parallel workers); the same exposure is often re-examined
many times. The cache turns repeat runs into instant loads.

## On-disk layout

```
~/.cache/ra_log_explorer/                       ← override with $RA_LOG_EXPLORER_CACHE
└── <cluster>/<namespace>/
    └── <fromSlug>__<toSlug>/                   ← one directory per window
        ├── _meta.json                          ← FetchSpec + per-pod byte counts
        ├── pods.txt                            ← pods that emitted in the window
        ├── pods/<pod>.jsonl                    ← raw Loki JSONL, --forward order
        └── .partial                            ← present only while a fetch is in progress
```

Slugs are the `--from`/`--to` ISO strings with `:` removed and `.` →
`_` (filesystem-friendly). The directory naming is therefore a stable
function of the window.

## Cache hit policy

`fetchAll(spec, ...)` decides in order:

1. **Skip the cache entirely** if either `forceRefresh=True` or the
   requested window's `to` is in the future. (The cluster might still
   produce new logs inside the window, so the cache would be stale.)

2. **Exact hit** if `<requestedDir>/_meta.json` exists and there's no
   `.partial` flag. Returns the requested directory and the saved meta
   with `cacheReuse = "exact"`.

3. **Superset hit** if any other completed cache directory under
   `<cluster>/<namespace>/` has a window that fully contains the
   requested one. The *smallest* such superset wins. Returns that
   directory and meta with `cacheReuse = "superset"` plus
   `cacheReusePath`.

4. Otherwise **fetch fresh**: create the requested directory, write
   `.partial`, list pods via `logcli series`, fetch each pod in
   parallel with `--limit=DEFAULT_LINE_LIMIT --forward`, write
   `_meta.json`, remove `.partial`. Returns the requested directory
   with `cacheReuse = "none"`.

## Why supersets are safe

When the cached series query was `{cluster=…,namespace=…}` over
`[C, D]`, it returned every pod that emitted *any* log in that window.
For any inner window `[A, B] ⊆ [C, D]`, the set of pods emitting in
`[A, B]` is a subset of the cached set — so re-listing would return no
new pods. The per-pod JSONL files cover the full `[C, D]` window, so
they contain every line that would appear in `[A, B]` too.

The only price of superset reuse is extra context: parsing sees events
outside the user's nominal window. The timeline / detail UI shows
events at their true offset relative to t-zero, so this manifests
simply as a wider visible range; nothing is mis-attributed.

## The `.partial` flag

`.partial` is written before pods are enumerated and removed after the
last pod file is on disk. A crashed/Ctrl-C'd fetch leaves the flag in
place, and `fetchAll` then refuses to treat the directory as a cache
hit even if `_meta.json` is somehow present. There's no automatic
recovery — re-run with `--force-refresh` to overwrite.

## What's stable across reruns

- The cache directory path is determined solely by
  `(cluster, namespace, fromIso, toIso)`. Different t-zero values can
  share the same cache directory if they produce the same window.

- `_meta.json` records the original `FetchSpec` it was fetched with,
  so a superset cache directory can be inspected to see why it was
  created.

- The per-pod JSONL files are byte-for-byte what Loki returned, so
  re-parsing always produces the same events.

## When to flush

- After a regex change in `parse.py` whose effect you can't reproduce
  with `--force-refresh` alone (rare — `parse.py` reads from cache, so
  fresh parsing happens on every run).

- When testing fetch behaviour itself (e.g. the partial-flag handling).

- When disk pressure matters: each window is roughly 40–50 MiB; long
  observing runs accumulate.

`python3 -m ra_log_explorer.cli cache info|flush` does this from the
CLI today. **The cache management interface will move into the
browser UI** once we add an in-UI fetch path; the CLI surface should
be treated as a placeholder.

## Future: cleaner subset semantics

The current superset reuse picks the smallest cached superset. We could
go further:

- **Patch reuse**: if a partial superset exists (some files missing or
  truncated), top it up rather than re-fetching from scratch.
- **Extension reuse**: re-use an existing cache and only fetch the new
  time slice when widening the window slightly.

Neither is implemented; both are easy if they become necessary. Don't
build them until there's a concrete use case.
