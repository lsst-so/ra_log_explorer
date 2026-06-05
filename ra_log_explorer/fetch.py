"""Loki/logcli wrapper: list pods and download per-pod logs with caching.

Caching strategy
----------------
Logs are keyed by the (cluster, namespace, fromIso, toIso) tuple. The cache
directory contains:

  _meta.json          mandatory; records the spec, fetched_at, byte totals,
                      and per-pod file sizes
  pods/<pod>.jsonl    one Loki JSONL file per pod that had any output
  pods.txt            cached list of pod names

A cache hit re-uses the existing files iff:
  * _meta.json is present and well-formed
  * _meta.json carries the current ``fetchSchemaVersion`` (so caches
    written by an older, possibly-truncating version of the tool are
    never silently re-served — they re-fetch instead)
  * the requested window's end time is in the past at fetch time (so future
    re-runs would not pick up new logs anyway)
  * the cache was completed (no .partial flag file)

If the window extends into the future, we always re-fetch — otherwise we
would silently return a snapshot from before the window finished.

Schema-version flush
--------------------
On top of the per-cache ``fetchSchemaVersion`` guard, bumping
``CACHE_SCHEMA_VERSION`` triggers a one-shot flush of the *entire* cache
tree (see :func:`ensureCacheSchemaCurrent`, called once at process
start). The per-cache guard alone would re-fetch stale windows lazily,
one surprise-slow load at a time; the flush makes the cost explicit and
up front, and guarantees no data written by an older (possibly
line-dropping) fetch path can survive anywhere on disk. A sentinel file
at the cache root records the version the cache was built with.

Completeness
------------
``logcli`` silently drops log lines on wide, busy windows (Loki bug
grafana/loki#17270: across a multi-stream response it advances the next
batch's cursor by the *global* max timestamp, so a busier stream's tail
falls into a 1 ns dedup gap and vanishes). The loss only happens when a
query *paginates* — i.e. when some batch comes back full and logcli asks
for another. So each per-pod fetch is built entirely out of single-batch
queries: we size each time-chunk (presized by ``count_over_time``, the
exact server-side oracle) so it returns fewer than one ``--batch`` worth
of lines, and trust a chunk only when ``got < BATCH`` proves it completed
in a single, un-paginated request. Chunks that still fill a batch are
discarded and re-fetched as time-split halves until every piece fits, or
— at the :data:`MIN_SPLIT_S` floor — flagged. See :func:`_fetchOnePod`.

A fetch is therefore incomplete in two distinct ways, both recorded in
``_meta.json`` and surfaced loudly downstream: a hard per-pod ``logcli``
failure (``errors``), or a pod whose chunks could not be reconciled to a
single-batch guarantee (``incomplete_pods``). ``fetchComplete`` is
``True`` iff both maps are empty.

Superset reuse: when the requested window has no exact match but is fully
contained within some other cached window for the same (cluster, namespace),
that wider cache is reused instead of triggering a fetch. The cached series
query for [C, D] catches every pod that emitted in [C, D], so anything that
emitted in [A, B] ⊆ [C, D] is captured too. We deliberately pick the
*smallest* superset to minimize unrelated content.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from .config import FetchSpec, cache_root, ensureWindowCacheDir, windowCachePath

# Bumped whenever a change to how we fetch makes older caches untrustworthy.
# v1 (implicit, no field) capped each pod at 50_000 lines and so silently
# tail-dropped the busiest pods on a full night; v2 fetched with logcli
# --limit=0 but still silently dropped lines on wide busy windows (Loki bug
# grafana/loki#17270); v3 fetches in count-presized single-batch chunks and
# verifies completeness structurally. Bumping this value flushes the whole
# cache (see ``ensureCacheSchemaCurrent``) and, as a second line of defence,
# any individual cache lacking this exact value is re-fetched, not re-served.
CACHE_SCHEMA_VERSION = 3

# Sentinel at the cache root recording the schema version its contents were
# built with. A mismatch (or its absence) means a version bump happened, so
# the whole tree is flushed before anything is read. Lives at the root (not
# per-cache) precisely so the flush can be a single cheap check.
CACHE_SCHEMA_SENTINEL = "_cache_schema_version.txt"

# logcli's per-request batch size, set equal to the cluster's Loki
# ``max_entries_limit_per_query`` (≈5000 on yagan/manke). Two things hinge
# on this number, and both break if it *exceeds* the true server cap:
#   * logcli paginates in batches of this size. A batch that comes back
#     *full* (== this many entries) is the only thing that makes logcli ask
#     for another batch — and it's that next-batch cursor advance where
#     grafana/loki#17270 drops lines. So a fetch whose total ``got`` is
#     *fewer* than this completed in one un-paginated request and cannot
#     have lost anything.
#   * if we asked for a batch larger than the server cap, a full server
#     response (cap entries < our batch) would look non-full and logcli
#     would stop early — silently truncating. So never raise this above the
#     real cap; lowering it only costs extra (still-correct) chunking.
SERVER_QUERY_CAP = 5000

# Target lines per presized chunk. Comfortably below SERVER_QUERY_CAP so the
# single-batch fetch has headroom for log-rate non-uniformity within the
# chunk and still comes back ``got < SERVER_QUERY_CAP`` (i.e. trusted).
CHUNK_TARGET_LINES = 4000

# Floor on how finely we split a window in time. >SERVER_QUERY_CAP lines from
# one pod inside this span is an implausible burst we cannot fetch in a
# single batch; rather than recurse forever we keep what we got and flag the
# pod incomplete. Generous: real pods average single-digit lines/second.
MIN_SPLIT_S = 1.0

# Cap on how many sub-windows one split produces, so a pathological count
# can't fan out into hundreds of children at once; deeper recursion handles
# the rest.
MAX_SPLIT_PARTS = 60

# Per-pod query timeout. A presized chunk is bounded (≤ SERVER_QUERY_CAP
# lines) so it returns quickly, but a very chatty pod over a full night
# fans out into many chunks; this budget covers the whole per-pod tree. The
# series listing and the count_over_time oracle keep shorter timeouts.
PER_POD_TIMEOUT_S = 1800.0
COUNT_TIMEOUT_S = 180.0

PARTIAL_FLAG = ".partial"
META_NAME = "_meta.json"
PODS_LIST_NAME = "pods.txt"
PODS_DIR_NAME = "pods"
# Per-cache sidecar holding the ISO timestamp of when this window was
# last opened by the user. Used to LRU-evict old caches when the
# total on-disk size exceeds the configured max. Living alongside
# the cache (rather than in a central index) means deleting the
# directory takes the bookkeeping with it.
LAST_VIEWED_NAME = "_last_viewed.txt"
# Per-cache sidecar holding the dataIds (one per line, ascending) that
# have been the *trigger* for a fetch landing on this cache. One cache
# can serve multiple dataIds via superset reuse — the user-facing cache
# table surfaces all of them as clickable shortcuts back to each
# exposure's per-visit view. See ``addExposureToCache`` for the writer.
EXPOSURE_IDS_NAME = "_exposure_ids.txt"
# Per-cache sidecar marking a range-mode fetch: two lines, ``startId``
# then ``stopId``. A range cache is structurally an ordinary exposure
# cache (all pods, one wide window) — this sidecar is what lets the
# cache listing label it "range" and deep-link back to /?rangeStart=…&
# rangeStop=… instead of the single-exposure view. See ``markCacheRange``.
RANGE_NAME = "_range.txt"


class FetchError(RuntimeError):
    pass


def ensureCacheSchemaCurrent() -> int:
    """Flush the entire cache tree iff it was built by a different schema.

    Call this once at process start. A sentinel file at the cache root
    records the ``CACHE_SCHEMA_VERSION`` its contents were written with;
    when that doesn't match (including the first run after an upgrade,
    where the sentinel is absent), every cached window is removed and the
    sentinel rewritten. Returns the number of top-level entries deleted
    (0 when already current or empty), so the caller can tell the user why
    the next load is slow.

    This is deliberately heavier-handed than the per-cache
    ``fetchSchemaVersion`` guard: that guard re-fetches stale windows
    lazily, one surprise-slow load at a time, and leaves untrusted bytes
    on disk in the meantime. The flush makes the cost explicit and up
    front and guarantees nothing written by an older (possibly
    line-dropping) fetch path survives anywhere.
    """
    root = cache_root()
    sentinel = root / CACHE_SCHEMA_SENTINEL
    current = str(CACHE_SCHEMA_VERSION)
    try:
        recorded = sentinel.read_text().strip() if sentinel.exists() else None
    except OSError:
        recorded = None
    if recorded == current:
        return 0
    removed = 0
    for child in root.iterdir():
        if child.name == CACHE_SCHEMA_SENTINEL:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError:
            pass  # best-effort; the per-cache guard still refuses stale meta
    try:
        sentinel.write_text(current + "\n")
    except OSError:
        pass
    if removed:
        print(
            f"Cache schema changed (was {recorded or 'unversioned'}, now {current}); "
            f"flushed {removed} cached window(s). They will re-fetch on demand.",
            file=sys.stderr,
        )
    return removed


def _run_logcli(
    spec: FetchSpec,
    extraArgs: list[str],
    timeout: float = 300.0,
    stdoutPath: Path | None = None,
) -> bytes:
    """Run logcli with the spec's connection args plus extras.

    When ``stdoutPath`` is given, logcli's stdout is streamed straight to
    that file (bounded memory — used for the potentially huge per-pod
    query) and ``b""`` is returned. Otherwise stdout is captured in full
    and returned (used for the small ``series`` listing).

    On failure the partial file, if any, is left in place: the caller
    records the pod as errored and a partial download is still better
    than nothing, as long as we flag it.
    """
    cmd = [
        "logcli",
        f"--username={spec.username}",
        f"--addr={spec.lokiAddr}",
        "--quiet",
        *extraArgs,
    ]
    env = os.environ.copy()
    if "LOKI_PASSWORD" not in env:
        raise FetchError(
            "LOKI_PASSWORD is not set in the environment. "
            "Export it (or source the shell rc that does) before running."
        )
    try:
        if stdoutPath is not None:
            with open(stdoutPath, "wb") as fh:
                subprocess.run(cmd, check=True, stdout=fh, stderr=subprocess.PIPE, env=env, timeout=timeout)
            return b""
        result = subprocess.run(cmd, check=True, capture_output=True, env=env, timeout=timeout)
        return result.stdout
    except FileNotFoundError as e:
        raise FetchError("logcli binary not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise FetchError(
            f"logcli failed (rc={e.returncode}): {e.stderr.decode('utf-8', 'replace').strip()[:500]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"logcli timed out after {timeout}s") from e


def _matcher(spec: FetchSpec, pod: str | None = None) -> str:
    """Build the LogQL ``{cluster=…, namespace=…, ...}`` matcher.

    If ``pod`` is given we pin the matcher to that exact pod (used for
    per-pod queries). Otherwise we honour ``spec.podRegex`` if set, so
    night-mode fetches can restrict the series listing to just the
    pod-name patterns we care about (e.g. ``.*aos.*``).
    """
    parts = [f'cluster="{spec.cluster}"', f'namespace="{spec.namespace}"']
    if pod is not None:
        parts.append(f'pod="{pod}"')
    elif spec.podRegex:
        parts.append(f'pod=~"{spec.podRegex}"')
    return "{" + ",".join(parts) + "}"


def listPods(spec: FetchSpec) -> list[str]:
    """List unique pod names that emitted logs in the requested window."""
    matcher = _matcher(spec)
    out = _run_logcli(
        spec,
        ["series", matcher, f"--from={spec.fromIso}", f"--to={spec.toIso}"],
    )
    pods: set[str] = set()
    for line in out.decode("utf-8", "replace").splitlines():
        # series output prints {label="value", ...} per stream; extract pod=
        idx = line.find('pod="')
        if idx == -1:
            continue
        end = line.find('"', idx + 5)
        if end == -1:
            continue
        pods.add(line[idx + 5 : end])
    return sorted(pods)


def _fmtLogcliTime(t: dt.datetime) -> str:
    """Format a datetime for logcli ``--from``/``--to``/``--now``.

    RFC3339 with a ``Z`` suffix (microsecond precision). logcli honours the
    explicit ``Z`` as UTC — matching how ``spec.fromIso``/``toIso`` are
    produced upstream, so split-window boundaries stay consistent with the
    requested window.
    """
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _countOverTime(spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime) -> int | None:
    """Return how many lines this pod emitted in ``[fromT, toT)``, or None.

    Uses ``sum(count_over_time(...))`` evaluated at ``--now=toT`` over a
    range selector spanning the window. count_over_time is a *server-side*
    aggregation — it never paginates entries, so unlike a log query it is
    exact and immune to grafana/loki#17270. It is the oracle we presize
    chunks by (and record for transparency).

    Best-effort: any failure (logcli error, unparseable output) returns
    ``None``. Correctness never depends on this — the single-batch
    ``got < SERVER_QUERY_CAP`` check in :func:`_fetchWindowInto` is what
    actually guarantees no lines were dropped; the count only makes the
    chunking efficient and gives humans a number to sanity-check against.
    """
    rangeNs = int((toT - fromT).total_seconds() * 1e9)
    if rangeNs <= 0:
        return 0
    matcher = _matcher(spec, pod=pod)
    query = f"sum(count_over_time({matcher}[{rangeNs}ns]))"
    try:
        out = _run_logcli(
            spec,
            ["instant-query", query, f"--now={_fmtLogcliTime(toT)}", "-o", "jsonl"],
            timeout=COUNT_TIMEOUT_S,
        )
    except FetchError:
        return None
    return _parseCountOutput(out)


def _parseCountOutput(out: bytes) -> int | None:
    """Pull the integer sample value out of an instant-query ``-o jsonl``
    result, tolerant of the exact shape logcli emits.

    A ``sum(...)`` instant query yields a single vector sample whose value
    is ``[<ts>, "<count>"]``. We sum any such samples we can find (a bare
    ``count_over_time`` without ``sum`` would emit one per stream). Returns
    ``None`` if nothing parses — see :func:`_countOverTime` on why that is
    safe.
    """
    total = 0
    found = False
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = obj.get("value") if isinstance(obj, dict) else None
        # Loki vector sample: value == [<unixSeconds>, "<count>"].
        if isinstance(value, list) and len(value) == 2:
            try:
                total += int(float(value[1]))
                found = True
            except (TypeError, ValueError):
                continue
    return total if found else None


def _queryWindowToFile(spec: FetchSpec, pod: str, fromT: dt.datetime, toT: dt.datetime, outPath: Path) -> int:
    """Fetch ``[fromT, toT)`` for one pod into ``outPath``; return line count.

    A single ``query`` with ``--batch=SERVER_QUERY_CAP``. ``--forward`` =>
    ascending output; ``-o jsonl`` => one Loki API JSON object per line
    (keeps labels, esp. detected_level, intact). The caller decides whether
    to trust the result based on the returned count vs the batch size.
    """
    matcher = _matcher(spec, pod=pod)
    _run_logcli(
        spec,
        [
            "query",
            matcher,
            f"--from={_fmtLogcliTime(fromT)}",
            f"--to={_fmtLogcliTime(toT)}",
            "--limit=0",
            f"--batch={SERVER_QUERY_CAP}",
            "-o",
            "jsonl",
            "--forward",
        ],
        timeout=PER_POD_TIMEOUT_S,
        stdoutPath=outPath,
    )
    return _countLines(outPath)


def _countLines(path: Path) -> int:
    """Count newline-terminated lines in a file without loading it whole."""
    n = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b"\n")
    return n


def _appendFileInto(src: Path, dst: BinaryIO) -> None:
    """Append ``src``'s bytes to the already-open binary file ``dst``."""
    with open(src, "rb") as fh:
        shutil.copyfileobj(fh, dst)


@dataclass
class _PodFetch:
    """Outcome of fetching one pod's logs across all its chunks."""

    pod: str
    nbytes: int
    lines: int
    expected: int | None  # count_over_time oracle for the whole pod window
    complete: bool
    reason: str  # "" when complete; else why a chunk could not be reconciled


def _fetchOnePod(spec: FetchSpec, pod: str, outPath: Path) -> _PodFetch:
    """Fetch *all* of one pod's logs into ``outPath`` without dropping lines.

    logcli silently loses entries on wide, busy windows (grafana/loki#17270,
    a cross-stream cursor-advance bug that only bites when a query
    paginates). We sidestep it by building the output entirely from
    *single-batch* queries: presize each time-chunk by the count_over_time
    oracle, fetch it in one ``--batch`` request, and trust it only when
    ``got < SERVER_QUERY_CAP`` proves it didn't paginate. Anything bigger is
    split in time and retried; see :func:`_fetchWindowInto`.

    Output stays globally time-ascending (chunks are processed left→right,
    each ``--forward``) so the parser's ordering assumption still holds.
    """
    fromT = _parseIso(spec.fromIso)
    toT = _parseIso(spec.toIso)
    expected = _countOverTime(spec, pod, fromT, toT)
    with open(outPath, "wb") as fh:
        lines, complete, reason = _fetchWindowInto(spec, pod, fromT, toT, fh, expected)
    return _PodFetch(
        pod=pod,
        nbytes=outPath.stat().st_size,
        lines=lines,
        expected=expected,
        complete=complete,
        reason=reason,
    )


def _fetchWindowInto(
    spec: FetchSpec,
    pod: str,
    fromT: dt.datetime,
    toT: dt.datetime,
    fh: BinaryIO,
    expected: int | None,
) -> tuple[int, bool, str]:
    """Fetch ``[fromT, toT)`` for one pod, appending to open file ``fh``.

    Returns ``(linesWritten, complete, reason)``. ``expected`` is the
    count_over_time oracle for *this* window (``None`` => look it up). The
    invariant that makes this lossless: a window is only trusted when its
    single fetch returns fewer than one full batch of lines, which means
    logcli never advanced a batch cursor and so #17270 never fired.
    """
    if expected is None:
        expected = _countOverTime(spec, pod, fromT, toT)
    if expected == 0:
        return 0, True, ""  # oracle says empty — no query needed
    span = (toT - fromT).total_seconds()
    # Oracle already proves this won't fit one batch: split up front rather
    # than waste a fetch we know will paginate (and be discarded).
    if expected is not None and expected >= SERVER_QUERY_CAP and span > MIN_SPLIT_S:
        return _splitWindowInto(spec, pod, fromT, toT, fh, expected)

    # Chunk temps go in the system temp dir, NOT the pods dir: a crash mid-
    # fetch must never strand a ".chunk-*.jsonl" beside the real pod files,
    # where the parser (which globs pods/*.jsonl) would mistake it for a pod.
    # We append via copyfileobj, so the temp needn't share a filesystem.
    fd, tmpName = tempfile.mkstemp(prefix="ra-log-chunk-", suffix=".jsonl")
    os.close(fd)  # mkstemp opens it; _queryWindowToFile reopens to write
    tmpPath = Path(tmpName)
    try:
        got = _queryWindowToFile(spec, pod, fromT, toT, tmpPath)
        if got < SERVER_QUERY_CAP:
            _appendFileInto(tmpPath, fh)  # one un-paginated batch — trusted
            return got, True, ""
        if span <= MIN_SPLIT_S:
            # >SERVER_QUERY_CAP lines from one pod in ≤MIN_SPLIT_S: an
            # implausible burst we can't fetch losslessly in one batch. Keep
            # what we got but flag it — never silently pretend it's whole.
            _appendFileInto(tmpPath, fh)
            return got, False, f"{got}+ lines in {span:.3f}s exceeds one batch and can't be split finer"
    finally:
        tmpPath.unlink(missing_ok=True)
    # got >= cap and we have room to split: the count under-counted (or was
    # unavailable). Discard the untrusted fetch and recurse on halves.
    return _splitWindowInto(spec, pod, fromT, toT, fh, max(expected or 0, got))


def _splitWindowInto(
    spec: FetchSpec,
    pod: str,
    fromT: dt.datetime,
    toT: dt.datetime,
    fh: BinaryIO,
    expected: int,
) -> tuple[int, bool, str]:
    """Split ``[fromT, toT)`` into equal-time sub-windows and fetch each.

    The half-open split tiles the window exactly (``[a, mid) ∪ [mid, b)``),
    so no entry is dropped or duplicated at a boundary. The number of parts
    is sized from the oracle to aim for ~``CHUNK_TARGET_LINES`` each; any
    sub-window that still overflows gets split again by :func:`_fetchWindowInto`.
    """
    span = (toT - fromT).total_seconds()
    nParts = 2
    if expected > 0:
        nParts = min(MAX_SPLIT_PARTS, max(2, math.ceil(expected / CHUNK_TARGET_LINES)))
    edges = [fromT + dt.timedelta(seconds=span * i / nParts) for i in range(nParts + 1)]
    total = 0
    complete = True
    reason = ""
    for a, b in zip(edges[:-1], edges[1:]):
        ln, ok, why = _fetchWindowInto(spec, pod, a, b, fh, expected=None)
        total += ln
        if not ok:
            complete = False
            reason = reason or why
    return total, complete, reason


def _parseIso(s: str) -> dt.datetime:
    """Parse an ISO-8601 string into an aware UTC datetime."""
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def findSupersetCache(
    cluster: str,
    namespace: str,
    fromIso: str,
    toIso: str,
    podRegex: str | None = None,
) -> Path | None:
    """Return the *smallest* cached window that fully contains [fromIso, toIso].

    A cache directory is considered usable only if it has a valid ``_meta.json``
    and no ``.partial`` flag. Returns ``None`` if no superset is on disk.

    Pod-filter compatibility: only cache entries whose stored
    ``spec.podRegex`` matches the requested one are considered. An
    exposure-mode (all-pods) cache is NOT a valid superset for a
    night-mode (AOS-only) request, because the on-disk pod sets differ.
    """
    fromT = _parseIso(fromIso)
    toT = _parseIso(toIso)
    base = cache_root() / cluster / namespace
    if not base.exists():
        return None
    candidates: list[tuple[dt.timedelta, Path]] = []
    # When night-mode (podRegex set), candidates live under base/<window>/pods=<slug>/.
    # When exposure-mode (no podRegex), candidates live under base/<window>/.
    # Walk both depths so each mode finds its own kind.
    windowDirs: list[Path] = []
    if podRegex is None:
        windowDirs = [d for d in base.iterdir() if d.is_dir()]
    else:
        for d in base.iterdir():
            if not d.is_dir():
                continue
            for inner in d.iterdir():
                if inner.is_dir() and inner.name.startswith("pods="):
                    windowDirs.append(inner)
    for window in windowDirs:
        metaP = window / META_NAME
        if not metaP.exists() or (window / PARTIAL_FLAG).exists():
            continue
        try:
            meta = json.loads(metaP.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("fetchSchemaVersion") != CACHE_SCHEMA_VERSION:
            continue  # older (possibly truncated) cache — re-fetch, don't reuse
        specMeta = meta.get("spec") or {}
        if specMeta.get("podRegex") != podRegex:
            continue  # different filter scope; on-disk pod set is different
        cFromIso = specMeta.get("fromIso")
        cToIso = specMeta.get("toIso")
        if not cFromIso or not cToIso:
            continue
        try:
            cFrom = _parseIso(cFromIso)
            cTo = _parseIso(cToIso)
        except ValueError:
            continue
        if cFrom <= fromT and toT <= cTo:
            candidates.append((cTo - cFrom, window))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def fetchAll(
    spec: FetchSpec,
    progress: Callable[[str, int, int], None] | None = None,
    forceRefresh: bool = False,
) -> tuple[Path, dict]:
    """Fetch (or load from cache) the full log set for `spec`.

    Returns a ``(cacheDir, meta)`` tuple. ``cacheDir`` is the directory the
    caller should read pod files from — typically the exact-spec dir, but on
    a superset cache hit it points to whichever wider window we found.
    """
    requestedDir = windowCachePath(spec.cluster, spec.namespace, spec.fromIso, spec.toIso, spec.podRegex)
    metaPath = requestedDir / META_NAME
    partialPath = requestedDir / PARTIAL_FLAG

    windowEnd = _parseIso(spec.toIso)
    now = dt.datetime.now(dt.timezone.utc)
    windowInPast = now > windowEnd

    # Cache eligibility is the same for both modes: only honour a cache
    # if the window is fully in the past. For night mode this means the
    # current/in-progress dayObs always triggers a fresh fetch — a
    # cached snapshot from earlier in the same night would miss every
    # exposure logged since then. For exposure mode the window-must-end-
    # in-the-past rule already covered this; we just no longer make an
    # exception for the podRegex case.
    cacheEligible = (not forceRefresh) and windowInPast
    if cacheEligible:
        # Exact-spec cache hit? Only honour it if the cache was written by
        # the current fetch schema — an older (v1) cache may be truncated,
        # so we fall through and re-fetch rather than re-serve it.
        if requestedDir.exists() and metaPath.exists() and not partialPath.exists():
            meta = json.loads(metaPath.read_text())
            if meta.get("fetchSchemaVersion") == CACHE_SCHEMA_VERSION:
                meta["fromCache"] = True
                meta["cacheReuse"] = "exact"
                return requestedDir, meta
        # Otherwise, look for a wider cached window that contains us.
        # Superset reuse is only honoured when the pod-filter matches;
        # otherwise an exposure-mode (all-pods) window could pretend to
        # contain a night-mode (AOS-only) window and vice-versa, even
        # though their on-disk contents differ.
        superset = findSupersetCache(spec.cluster, spec.namespace, spec.fromIso, spec.toIso, spec.podRegex)
        if superset is not None:
            meta = loadCacheMeta(superset)
            meta["fromCache"] = True
            meta["cacheReuse"] = "superset"
            meta["cacheReusePath"] = str(superset)
            return superset, meta

    # No usable cache — fetch fresh into the requested dir.
    ensureWindowCacheDir(spec.cluster, spec.namespace, spec.fromIso, spec.toIso, spec.podRegex)
    podsDir = requestedDir / PODS_DIR_NAME
    podsListPath = requestedDir / PODS_LIST_NAME
    partialPath.write_text("")
    podsDir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    pods = listPods(spec)
    podsListPath.write_text("\n".join(pods) + "\n")

    perPodBytes: dict[str, int] = {}
    perPodLines: dict[str, int] = {}
    perPodExpected: dict[str, int] = {}
    totalBytes = 0
    # Two distinct ways a fetch falls short, both surfaced loudly downstream:
    #   errors          — a hard logcli failure (timeout, transient 5xx)
    #   incompletePods  — chunks that couldn't be reconciled to a single-batch
    #                     guarantee, i.e. data #17270 may have eaten
    errors: dict[str, str] = {}
    incompletePods: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=spec.workers) as ex:
        futures = {ex.submit(_fetchOnePod, spec, pod, podsDir / f"{pod}.jsonl"): pod for pod in pods}
        for i, fut in enumerate(as_completed(futures), 1):
            pod = futures[fut]
            try:
                res = fut.result()
                perPodBytes[pod] = res.nbytes
                perPodLines[pod] = res.lines
                if res.expected is not None:
                    perPodExpected[pod] = res.expected
                totalBytes += res.nbytes
                if not res.complete:
                    incompletePods[pod] = res.reason or "chunks could not be reconciled"
            except Exception as e:  # noqa: BLE001 - collect, don't bail
                # Hard per-pod failure. A streamed query may have left a
                # partial file behind; count whatever bytes landed.
                errors[pod] = str(e)
                partialFile = podsDir / f"{pod}.jsonl"
                partialBytes = partialFile.stat().st_size if partialFile.exists() else 0
                perPodBytes[pod] = partialBytes
                totalBytes += partialBytes
            if progress is not None:
                progress(pod, i, len(pods))

    elapsed = time.time() - t0
    meta = {
        "spec": asdict(spec),
        "fetchSchemaVersion": CACHE_SCHEMA_VERSION,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "pod_count": len(pods),
        "total_bytes": totalBytes,
        "pod_bytes": perPodBytes,
        "pod_lines": perPodLines,
        # count_over_time oracle per pod (when available) — lets a human (or
        # test) sanity-check lines-written against what Loki says was there.
        "pod_expected": perPodExpected,
        "errors": errors,
        "incomplete_pods": incompletePods,
        # True iff every pod's logs were fetched in full: no hard failure and
        # every chunk proved lossless. Either map non-empty => missing data.
        "fetchComplete": not errors and not incompletePods,
        "window_in_past": windowInPast,
        "fromCache": False,
        "cacheReuse": "none",
    }
    metaPath.write_text(json.dumps(meta, indent=2))
    partialPath.unlink(missing_ok=True)
    return requestedDir, meta


def loadPodLogPath(cacheDir: Path, pod: str) -> Path:
    return cacheDir / PODS_DIR_NAME / f"{pod}.jsonl"


def loadCacheMeta(cacheDir: Path) -> dict:
    metaPath = cacheDir / META_NAME
    if not metaPath.exists():
        raise FileNotFoundError(metaPath)
    return json.loads(metaPath.read_text())


def cacheDuSizeBytes(root: Path) -> int:
    """Return on-disk size of the entire cache tree under `root`."""
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def humanBytes(n: int) -> str:
    """Format ``n`` bytes as a short human-readable string (e.g. "1.5 KiB")."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    x = float(n)
    # The ``or u == units[-1]`` branch guarantees the loop always
    # returns on its final iteration, so mypy treats the function as
    # exhaustively returning a str.
    for u in units:
        if x < 1024.0 or u == units[-1]:
            return f"{x:6.1f} {u}"
        x /= 1024.0
    raise AssertionError("unreachable")  # pragma: no cover


def stderrProgress(pod: str, i: int, total: int) -> None:
    bar = f"[{i:4d}/{total}]"
    sys.stderr.write(f"\r{bar} {pod[:80]:<80}")
    sys.stderr.flush()
    if i == total:
        sys.stderr.write("\n")


# ----- per-cache last-viewed tracking + LRU eviction -----------------------


def markCacheViewed(cacheDir: Path, when: dt.datetime | None = None) -> None:
    """Write the ISO timestamp the user last opened this cache window.

    Best-effort: a write failure here is not worth interrupting an
    otherwise-successful request. The sidecar file just becomes
    out-of-date.
    """
    if not cacheDir.exists():
        return
    ts = (when or dt.datetime.now(dt.timezone.utc)).isoformat()
    try:
        (cacheDir / LAST_VIEWED_NAME).write_text(ts)
    except OSError:
        pass


def addExposureToCache(cacheDir: Path, expId: int) -> None:
    """Record that ``expId`` was a trigger for the contents of this
    cache window.

    A given window can be reused by multiple dataIds (the default
    fetch window is ~5 minutes wide, so consecutive exposures often
    land on the same superset cache). We append rather than overwrite,
    de-duplicate, and keep the file sorted so the UI can render a
    stable list. Best-effort: a write failure does not interrupt the
    request — the sidecar just won't carry that id.
    """
    if not cacheDir.exists():
        return
    existing = set(getCacheExposureIds(cacheDir))
    existing.add(int(expId))
    body = "\n".join(str(i) for i in sorted(existing)) + "\n"
    try:
        (cacheDir / EXPOSURE_IDS_NAME).write_text(body)
    except OSError:
        pass


def getCacheExposureIds(cacheDir: Path) -> list[int]:
    """Return the dataIds previously recorded as triggers for this
    cache, sorted ascending. ``[]`` if the sidecar is missing or
    unparseable — same best-effort contract as the writer."""
    p = cacheDir / EXPOSURE_IDS_NAME
    if not p.exists():
        return []
    out: list[int] = []
    try:
        for line in p.read_text().splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                out.append(int(s))
            except ValueError:
                continue
    except OSError:
        return []
    return sorted(set(out))


def markCacheRange(cacheDir: Path, startId: int, stopId: int) -> None:
    """Record that this cache is a range-mode fetch over ``[startId, stopId]``.

    Best-effort, same contract as :func:`addExposureToCache`: a write
    failure does not interrupt the request — the cache just won't be
    recognised as a range in the listing (it falls back to "exposure").
    """
    if not cacheDir.exists():
        return
    try:
        (cacheDir / RANGE_NAME).write_text(f"{int(startId)}\n{int(stopId)}\n")
    except OSError:
        pass


def getCacheRange(cacheDir: Path) -> tuple[int, int] | None:
    """Return the ``(startId, stopId)`` recorded for a range cache, or
    ``None`` if the sidecar is missing or unparseable — same best-effort
    contract as the writer."""
    p = cacheDir / RANGE_NAME
    if not p.exists():
        return None
    try:
        lines = [s.strip() for s in p.read_text().splitlines() if s.strip()]
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        return int(lines[0]), int(lines[1])
    except ValueError:
        return None


def getCacheLastViewed(cacheDir: Path) -> dt.datetime | None:
    """Return the timestamp from a cache's ``_last_viewed.txt`` sidecar,
    or ``None`` if the file is missing or unparseable."""
    p = cacheDir / LAST_VIEWED_NAME
    if not p.exists():
        return None
    try:
        return _parseIso(p.read_text().strip())
    except (OSError, ValueError):
        return None


def _iterCacheDirs(root: Path) -> list[Path]:
    """Walk the cache root and return every dir that contains a valid
    ``_meta.json``. Includes both top-level exposure caches and nested
    night ``pods=…`` subdirs.
    """
    out: list[Path] = []
    if not root.exists():
        return out
    for cluster in root.iterdir():
        if not cluster.is_dir():
            continue
        for ns in cluster.iterdir():
            if not ns.is_dir():
                continue
            for window in ns.iterdir():
                if not window.is_dir():
                    continue
                if (window / META_NAME).exists():
                    out.append(window)
                # Night-mode nests one level deeper under pods=<slug>/.
                for inner in window.iterdir():
                    if inner.is_dir() and inner.name.startswith("pods=") and (inner / META_NAME).exists():
                        out.append(inner)
    return out


def evictToFit(maxBytes: int, exempt: Iterable[Path] = ()) -> list[Path]:
    """Evict the least-recently-viewed cache windows until the on-disk
    total is at or below ``maxBytes``.

    Caches in ``exempt`` are never removed — used to spare the cache
    that was just fetched. (It would just be re-fetched on the next
    request, which defeats the point of having any limit at all.)

    Caches without a ``_last_viewed.txt`` sidecar are treated as
    oldest — they've never been opened, so they're the safest to drop.

    Returns the list of directories that were removed, for logging
    / debugging.
    """
    root = cache_root()
    exemptR = {p.resolve() for p in exempt}
    total = cacheDuSizeBytes(root)
    if total <= maxBytes:
        return []
    # Sort by (last-viewed ASC, dir name) so untouched caches go first
    # and ties are broken stably. Untouched caches get an epoch-zero
    # sentinel so they precede everything.
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    candidates: list[tuple[dt.datetime, Path]] = []
    for d in _iterCacheDirs(root):
        if d.resolve() in exemptR:
            continue
        lv = getCacheLastViewed(d) or epoch
        candidates.append((lv, d))
    candidates.sort(key=lambda x: (x[0], x[1].name))
    removed: list[Path] = []
    for _, d in candidates:
        if total <= maxBytes:
            break
        sz = cacheDuSizeBytes(d)
        try:
            shutil.rmtree(d)
        except OSError:
            continue
        removed.append(d)
        total -= sz
        # Tidy up newly-empty parents (cluster/, namespace/, and the
        # window dir for night caches whose pods= subdir we just
        # removed).
        parent = d.parent
        while parent != root and parent.exists() and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return removed
