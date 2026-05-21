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

from .config import FetchSpec, window_cache_dir

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


def listPods(spec: FetchSpec) -> list[str]:
    """List unique pod names that emitted logs in the requested window."""
    matcher = '{cluster="' + spec.cluster + '",namespace="' + spec.namespace + '"}'
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
    matcher = '{cluster="' + spec.cluster + '",namespace="' + spec.namespace + '",pod="' + pod + '"}'
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


def fetchAll(
    spec: FetchSpec,
    progress: Callable[[str, int, int], None] | None = None,
    forceRefresh: bool = False,
) -> dict:
    """Fetch (or load from cache) the full log set for `spec`.

    Returns the meta dict describing the cache directory.
    """
    cacheDir = window_cache_dir(spec.cluster, spec.namespace, spec.fromIso, spec.toIso)
    metaPath = cacheDir / META_NAME
    podsDir = cacheDir / PODS_DIR_NAME
    podsListPath = cacheDir / PODS_LIST_NAME
    partialPath = cacheDir / PARTIAL_FLAG

    windowEnd = dt.datetime.fromisoformat(spec.toIso.replace("Z", "+00:00"))
    if windowEnd.tzinfo is None:
        windowEnd = windowEnd.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    windowInPast = now > windowEnd

    if not forceRefresh and windowInPast and metaPath.exists() and not partialPath.exists():
        meta = json.loads(metaPath.read_text())
        meta["fromCache"] = True
        return meta

    # Otherwise (re-)fetch. Mark partial.
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
    }
    metaPath.write_text(json.dumps(meta, indent=2))
    partialPath.unlink(missing_ok=True)
    return meta


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
