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
  * the requested window's end time is in the past at fetch time (so future
    re-runs would not pick up new logs anyway)
  * the cache was completed (no .partial flag file)

If the window extends into the future, we always re-fetch — otherwise we
would silently return a snapshot from before the window finished.

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
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import FetchSpec, cache_root, ensureWindowCacheDir, windowCachePath

PARTIAL_FLAG = ".partial"
META_NAME = "_meta.json"
PODS_LIST_NAME = "pods.txt"
PODS_DIR_NAME = "pods"


class FetchError(RuntimeError):
    pass


def _run_logcli(spec: FetchSpec, extraArgs: list[str], timeout: float = 300.0) -> bytes:
    """Run logcli with the spec's connection args plus extras; return stdout."""
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
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            env=env,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise FetchError("logcli binary not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise FetchError(
            f"logcli failed (rc={e.returncode}): " f"{e.stderr.decode('utf-8', 'replace').strip()[:500]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"logcli timed out after {timeout}s") from e
    return result.stdout


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


def _fetchOnePod(
    spec: FetchSpec,
    pod: str,
    outPath: Path,
) -> tuple[str, int]:
    """Fetch logs for a single pod; return (pod, bytes_written)."""
    matcher = _matcher(spec, pod=pod)
    # `--forward` => time-ordered ascending output; `-o jsonl` => one Loki API
    # JSON object per line which keeps labels (esp. detected_level) intact.
    out = _run_logcli(
        spec,
        [
            "query",
            matcher,
            f"--from={spec.fromIso}",
            f"--to={spec.toIso}",
            f"--limit={spec.lineLimit}",
            "-o",
            "jsonl",
            "--forward",
        ],
        timeout=600.0,
    )
    outPath.write_bytes(out)
    return pod, len(out)


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

    # Night-mode (filtered) fetches always trust an existing cache. The
    # 24h dayObs window is post-hoc analysis, and there's no benefit to
    # re-pulling 100MB of logs just because the window's nominal end
    # (noon UTC tomorrow) is technically still in the future. Exposure
    # mode keeps the conservative "window must be in the past before we
    # cache-hit" rule because a fresh fetch a few seconds after a
    # 5-minute window's end could still pick up late log lines.
    cacheEligible = (not forceRefresh) and (windowInPast or spec.podRegex is not None)
    if cacheEligible:
        # Exact-spec cache hit?
        if requestedDir.exists() and metaPath.exists() and not partialPath.exists():
            meta = json.loads(metaPath.read_text())
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
    totalBytes = 0
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=spec.workers) as ex:
        futures = {ex.submit(_fetchOnePod, spec, pod, podsDir / f"{pod}.jsonl"): pod for pod in pods}
        for i, fut in enumerate(as_completed(futures), 1):
            pod = futures[fut]
            try:
                _, nbytes = fut.result()
                perPodBytes[pod] = nbytes
                totalBytes += nbytes
            except Exception as e:  # noqa: BLE001 - collect, don't bail
                errors[pod] = str(e)
                perPodBytes[pod] = 0
            if progress is not None:
                progress(pod, i, len(pods))

    elapsed = time.time() - t0
    meta = {
        "spec": asdict(spec),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "pod_count": len(pods),
        "total_bytes": totalBytes,
        "pod_bytes": perPodBytes,
        "errors": errors,
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
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            return f"{x:6.1f} {u}"
        x /= 1024.0
    return f"{n} B"


def stderrProgress(pod: str, i: int, total: int) -> None:
    bar = f"[{i:4d}/{total}]"
    sys.stderr.write(f"\r{bar} {pod[:80]:<80}")
    sys.stderr.flush()
    if i == total:
        sys.stderr.write("\n")
