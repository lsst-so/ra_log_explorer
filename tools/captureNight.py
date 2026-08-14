"""Capture whole nights of logs from a cluster into a reusable local corpus.

This is the tool that produces the *master* night directories development
work runs against: a full dayObs, every pod, app logs and the
``k8s/events`` lifecycle stream, laid out exactly as a deployed instance
would have left it. Point :mod:`tools.stageNight` at the result to serve
it locally.

It drives the real :class:`~ra_log_explorer.live.LiveNightManager` rather
than re-implementing a fetch loop, which buys three things that matter
for a capture measured in hours:

- **The artefact is what live mode produces**, not an approximation of
  it — namespace-wide lifecycle events demuxed per name (including the
  non-Pod ones), a ``_live.json`` sidecar, and a finalised ``_meta.json``.
- **Resume is free.** The sidecar records a per-pod watermark, so a
  capture killed half way through is resumed by re-running the same
  command: the poller's own restart recovery truncates torn bytes and
  refetches only what is missing. (This is the part that used to take a
  bespoke script parsing the previous run's log for pod names.)
- **Verification is free.** Finalisation runs the ``count_over_time``
  oracle for every pod and refetches any that fall short beyond the
  dedup-slack tolerance, which is the same audit the deployed poller
  applies at the noon rollover.

Usage::

    export LOKI_PASSWORD=...           # and be on the VPN
    .venv/bin/python tools/captureNight.py \
        --site bts --day-obs 20260811 20260812 \
        --out /path/to/master

Each night lands in ``<out>/<mon><dd>-night-<site>`` (e.g.
``aug11-night-bts``), with the ConsDB exposure records for all captured
nights accumulating in the shared ``<out>/exposure-times/<site>.json``,
mirroring the cache-root layout so staging is a straight copy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ra_log_explorer import live  # noqa: E402
from ra_log_explorer.config import (  # noqa: E402
    DEFAULT_USERNAME,
    DEFAULT_WORKERS,
    LIVE_LAG_S,
    dayObsEndUtc,
    dayObsStartUtc,
    windowCachePath,
)
from ra_log_explorer.fetch import (  # noqa: E402
    META_NAME,
    PODS_DIR_NAME,
    _fmtLogcliTime,
    humanBytes,
)
from ra_log_explorer.sites import Site, loadSites, siteByName  # noqa: E402

# How often the watcher thread reports progress. A night is fetched
# inside a single tick with no callback of its own, so without this the
# terminal is silent for the hours a busy night takes.
HEARTBEAT_S = 30.0
EXPOSURE_TIMES_DIR = "exposure-times"


def nightName(dayObs: int, siteName: str) -> str:
    """Directory name for a captured night — e.g. ``aug11-night-bts``."""
    day = dt.datetime.strptime(str(dayObs), "%Y%m%d")
    return f"{day.strftime('%b%d').lower()}-night-{siteName}"


def _heartbeat(nightDir: Path, label: str, stop: threading.Event) -> None:
    """Report bytes and pod files on disk until ``stop`` is set."""
    t0 = time.time()
    podsDir = nightDir / PODS_DIR_NAME
    while not stop.wait(HEARTBEAT_S):
        try:
            files = list(podsDir.glob("*.jsonl"))
            nbytes = sum(f.stat().st_size for f in files)
        except OSError:
            continue
        mins = (time.time() - t0) / 60.0
        print(f"  [{mins:6.1f}m] {label}: {len(files):4d} pod files, {humanBytes(nbytes)}", flush=True)


def captureOneNight(
    site: Site,
    dayObs: int,
    workRoot: Path,
    username: str,
    workers: int,
    lagS: float,
) -> tuple[Path, dict]:
    """Fetch and finalise one whole dayObs. Returns ``(nightDir, meta)``.

    Safe to re-run: an interrupted capture resumes from the sidecar's
    per-pod watermarks rather than starting over.
    """
    os.environ["RA_LOG_EXPLORER_CACHE"] = str(workRoot)
    manager = live.LiveNightManager(
        site=site,
        username=username,
        workers=workers,
        pollS=60.0,
        lagS=lagS,
        fixedDayObs=dayObs,
    )
    nightDir = windowCachePath(
        site.cluster,
        site.namespace,
        _fmtLogcliTime(dayObsStartUtc(dayObs)),
        _fmtLogcliTime(dayObsEndUtc(dayObs)),
    )
    stop = threading.Event()
    watcher = threading.Thread(target=_heartbeat, args=(nightDir, str(dayObs), stop), daemon=True)
    watcher.start()
    t0 = time.time()
    try:
        # One tick against a night that has already ended catches up the
        # whole 24 h in a single increment per pod, plus the namespace's
        # events stream, plus the ConsDB exposure records for the night.
        manager.tick()
        night = manager._night
        if night is None:
            raise RuntimeError(f"poller did not open a night dir for {dayObs}")
        # Finalisation is the poller's noon-rollover work: top up to night
        # end, audit every pod against the count oracle, refetch the ones
        # that fall short, write _meta.json. Reached directly because the
        # rollover that would normally trigger it never fires with the
        # dayObs pinned.
        manager._finaliseNight(night)
    finally:
        stop.set()
    elapsed = time.time() - t0
    meta = json.loads((night.dir / META_NAME).read_text())
    print(f"  fetched in {elapsed / 60:.1f}m", flush=True)
    return night.dir, meta


def describe(meta: dict) -> str:
    """One-line summary of a captured night's completeness."""
    lines = sum(meta.get("pod_lines", {}).values())
    events = sum(meta.get("pod_event_lines", {}).values())
    return (
        f"{meta['pod_count']} pods, {lines:,} log lines, {events:,} lifecycle lines, "
        f"{humanBytes(meta['total_bytes'])}, complete={meta['fetchComplete']} "
        f"(errors={len(meta.get('errors') or {})}, incomplete={len(meta.get('incomplete_pods') or {})})"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", required=True, help="site name from sites.toml (e.g. bts, summit)")
    p.add_argument("--day-obs", type=int, nargs="+", required=True, help="one or more dayObs to capture")
    p.add_argument("--out", type=Path, required=True, help="directory the master night dirs land in")
    p.add_argument("--name", help="override the derived directory name (single --day-obs only)")
    p.add_argument("--work-root", type=Path, help="cache root to fetch into (default <out>/_capture-work)")
    p.add_argument("--username", default=DEFAULT_USERNAME, help="Loki basic-auth user")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="parallel per-pod fetches")
    p.add_argument("--lag-s", type=float, default=LIVE_LAG_S, help="how far behind now a capture may reach")
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help="capture a night that hasn't ended yet (it will be short, and finalisation will "
        "mark pods complete against a window Loki is still filling)",
    )
    args = p.parse_args(argv)

    if args.name and len(args.day_obs) != 1:
        p.error("--name only makes sense with a single --day-obs")
    if not (os.environ.get("LOKI_PASSWORD") or "").strip():
        p.error("LOKI_PASSWORD is not set (and check the VPN is up before a long capture)")

    sites, _ = loadSites()
    site = siteByName(sites, args.site)
    out: Path = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    workRoot: Path = (args.work_root or out / "_capture-work").expanduser()

    now = dt.datetime.now(dt.timezone.utc)
    for dayObs in args.day_obs:
        if dayObsEndUtc(dayObs) + dt.timedelta(seconds=args.lag_s) > now and not args.allow_partial:
            ends = dayObsEndUtc(dayObs).isoformat()
            p.error(f"dayObs {dayObs} does not end until {ends}; wait, or pass --allow-partial")

    for dayObs in args.day_obs:
        dest = out / (args.name or nightName(dayObs, site.name))
        if dest.exists():
            print(f"{dayObs}: {dest} already exists — skipping", flush=True)
            continue
        print(f"{dayObs}: capturing {site.name} ({site.cluster}/{site.namespace}) -> {dest}", flush=True)
        nightDir, meta = captureOneNight(site, dayObs, workRoot, args.username, args.workers, args.lag_s)
        # Same filesystem by default, so this is a rename; the sidecar
        # travels with it, which is what makes the master stageable.
        shutil.move(str(nightDir), str(dest))
        # The ConsDB records are a per-site cache keyed by exposure id, so
        # one file serves every captured night. Kept beside the masters in
        # the cache root's own layout so staging copies it verbatim.
        srcTimes = workRoot / EXPOSURE_TIMES_DIR
        if srcTimes.is_dir():
            shutil.copytree(srcTimes, out / EXPOSURE_TIMES_DIR, dirs_exist_ok=True)
        print(f"{dayObs}: {describe(meta)}", flush=True)
        if meta.get("errors"):
            print(f"  errors: {json.dumps(meta['errors'], indent=2)[:2000]}", flush=True)
        if meta.get("incomplete_pods"):
            print(f"  incomplete: {json.dumps(meta['incomplete_pods'], indent=2)[:2000]}", flush=True)

    # Every night moved out, so the scratch cache root holds nothing that
    # isn't also in `out` — drop it, rather than leaving a directory that
    # looks like a master sitting among the masters. Only the default one:
    # an explicitly-named --work-root is the caller's, and a run that died
    # part way through never reaches here, which is what keeps an
    # interrupted capture resumable.
    if args.work_root is None:
        shutil.rmtree(workRoot, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
