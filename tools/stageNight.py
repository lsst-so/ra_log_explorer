"""Stage captured master nights into a cache root the server can serve.

The masters produced by :mod:`tools.captureNight` are pristine: nothing
should ever fetch into them, slice them, or evict them. This tool clones
one or more of them into a throwaway cache root (APFS copy-on-write via
``cp -c``, so it is instant and the master cannot be corrupted), and
writes the cache-schema sentinel so the server doesn't flush the tree at
startup.

Usage::

    .venv/bin/python tools/stageNight.py \
        --master /path/to/master/aug11-night-bts \
        --master /path/to/master/aug12-night-bts \
        --cache /path/to/app-cache

    RA_LOG_EXPLORER_CACHE=/path/to/app-cache \
    RA_LOG_EXPLORER_MAX_CACHE_BYTES=32212254720 \
    .venv/bin/python -m ra_log_explorer.cli run --no-browser

Re-run it any time to throw away whatever the app did and restage.

``--unfinalise <dayObs>`` puts one night back into the in-progress state,
which is what ``--live-day-obs <dayObs>`` needs to replay it as tonight:
the sidecar stops claiming the night is finished and ``_meta.json`` goes
away, so views are served by slicing the live dir exactly as they would
be while observing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ra_log_explorer import fetch  # noqa: E402
from ra_log_explorer.config import windowCachePath  # noqa: E402

EXPOSURE_TIMES_DIR = "exposure-times"


def _readMasterSpec(master: Path) -> tuple[str, str, str, str, int]:
    """Return ``(cluster, namespace, fromIso, toIso, dayObs)`` for a master.

    Reads whichever of the two sidecars the master has: the ``_live.json``
    a captured night carries, or the ``_meta.json`` of an older
    hand-fetched one.
    """
    sidecar = fetch.readLiveSidecar(master)
    meta = json.loads((master / fetch.META_NAME).read_text()) if (master / fetch.META_NAME).exists() else {}
    spec = meta.get("spec") or {}
    if sidecar is not None:
        fromIso, toIso, dayObs = sidecar["fromIso"], sidecar["toIso"], int(sidecar["dayObs"])
    elif spec:
        fromIso, toIso = spec["fromIso"], spec["toIso"]
        dayObs = int(fetch._parseIso(fromIso).strftime("%Y%m%d"))
    else:
        raise SystemExit(f"{master} has neither {fetch.LIVE_SIDECAR_NAME} nor a readable {fetch.META_NAME}")
    if not spec:
        raise SystemExit(f"{master} has no {fetch.META_NAME}; can't tell which cluster it came from")
    return spec["cluster"], spec["namespace"], fromIso, toIso, dayObs


def _synthesiseSidecar(nightDir: Path, meta: dict, fromIso: str, toIso: str, dayObs: int) -> None:
    """Write the ``_live.json`` a master fetched before live mode never had.

    Every pod is declared covered to the end of the night, which is true
    — the master is a whole-night fetch — and any pod the fetch could not
    complete keeps its flag, so the incomplete-fetch banner still fires
    on the nights that earned it.
    """
    pods: dict[str, dict] = {}
    for f in sorted((nightDir / fetch.PODS_DIR_NAME).glob("*.jsonl")):
        evFile = nightDir / fetch.PODS_EVENTS_DIR_NAME / f"{f.stem}.jsonl"
        pods[f.stem] = {
            "watermarkIso": toIso,
            "bytes": f.stat().st_size,
            "lines": meta.get("pod_lines", {}).get(f.stem, 0),
            "eventBytes": evFile.stat().st_size if evFile.exists() else 0,
            "eventLines": meta.get("pod_event_lines", {}).get(f.stem, 0),
        }
    fetch.writeLiveSidecar(
        nightDir,
        {
            "version": fetch.LIVE_SIDECAR_VERSION,
            "dayObs": dayObs,
            "fromIso": fromIso,
            "toIso": toIso,
            "watermarkIso": toIso,
            "eventsWatermarkIso": toIso,
            "finalised": False,
            "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "pods": pods,
            "eventPods": {},
            "errors": dict(meta.get("errors") or {}),
            "incomplete_pods": dict(meta.get("incomplete_pods") or {}),
        },
    )


def stageOne(master: Path, spec: tuple[str, str, str, str, int], unfinalise: bool) -> None:
    """Clone one master into the cache root named by the environment."""
    cluster, namespace, fromIso, toIso, dayObs = spec
    dest = windowCachePath(cluster, namespace, fromIso, toIso)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # cp -c is an APFS clonefile: instant, copy-on-write, and the master
    # stays pristine no matter what the app does to the staged copy.
    subprocess.run(["cp", "-c", "-R", str(master), str(dest)], check=True)
    meta = json.loads((dest / fetch.META_NAME).read_text())
    if fetch.readLiveSidecar(dest) is None:
        _synthesiseSidecar(dest, meta, fromIso, toIso, dayObs)
    if unfinalise:
        # An in-progress night has no _meta.json — the poller writes it
        # only at the rollover — and its sidecar says so.
        (dest / fetch.META_NAME).unlink(missing_ok=True)
        sidecar = fetch.readLiveSidecar(dest) or {}
        sidecar["finalised"] = False
        fetch.writeLiveSidecar(dest, sidecar)
    nPods = len(list((dest / fetch.PODS_DIR_NAME).glob("*.jsonl")))
    nEvents = len(list((dest / fetch.PODS_EVENTS_DIR_NAME).glob("*.jsonl")))
    state = "live (in progress)" if unfinalise else "finalised"
    print(f"staged {dayObs} as {state}: {nPods} pods, {nEvents} lifecycle files -> {dest}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--master", type=Path, action="append", required=True, help="a master night dir (repeatable)"
    )
    p.add_argument("--cache", type=Path, required=True, help="cache root to stage into (wiped first)")
    p.add_argument("--unfinalise", type=int, help="dayObs to stage as an in-progress live night")
    p.add_argument("--keep", action="store_true", help="add to the cache root instead of wiping it")
    args = p.parse_args(argv)

    masters = [m.expanduser() for m in args.master]
    for master in masters:
        if not master.is_dir():
            p.error(f"no such master night dir: {master}")
    specs = [_readMasterSpec(m) for m in masters]
    staged = [s[4] for s in specs]
    if args.unfinalise is not None and args.unfinalise not in staged:
        p.error(f"--unfinalise {args.unfinalise} names a dayObs none of the masters holds: {staged}")

    cache: Path = args.cache.expanduser()
    if cache.exists() and not args.keep:
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)
    # cache_root() reads this on every call, so every path below lands here.
    os.environ["RA_LOG_EXPLORER_CACHE"] = str(cache)

    for master, spec in zip(masters, specs):
        stageOne(master, spec, unfinalise=args.unfinalise == spec[4])
        # The ConsDB records live beside the masters, in the cache root's
        # own layout — without them the Tonight panel has no shutter
        # closes and every exposure row stays unresolved.
        srcTimes = master.parent / EXPOSURE_TIMES_DIR
        if srcTimes.is_dir():
            shutil.copytree(srcTimes, cache / EXPOSURE_TIMES_DIR, dirs_exist_ok=True)
    (cache / fetch.CACHE_SCHEMA_SENTINEL).write_text(f"{fetch.CACHE_SCHEMA_VERSION}\n")
    print(f"cache root {cache} ready ({len(staged)} night(s): {sorted(staged)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
