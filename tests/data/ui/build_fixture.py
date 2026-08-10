"""Build the UI-test fixture corpus from a full night of captured logs.

Run by hand, not by the test suite: its output (``july11.tar.xz``) is
checked in, and the suite only ever reads that. It exists so the corpus
can be regenerated — widened, re-cut to a different window, refreshed
from a newer capture — instead of being an opaque blob nobody can
reproduce.

The archive is gzip, not xz: xz would be a third smaller, but ``lzma``
is an optional CPython build dependency and is genuinely missing on some
interpreters (including the pyenv build this was developed on), which
would turn "run the tests" into "rebuild your Python". ``gzip`` is
always there.

The source is one real all-pods night fetched by this tool (dayObs
20260711 on the summit: 576 pods, 35.7M lines, 9.25 GiB). Shipping that
is obviously out of the question, so the corpus is cut down along two
axes, chosen from measurements of the real thing:

* **Pods.** 378 of the 576 pods are SFM workers and account for 370 MB
  of any 30-minute window; the entire rest of the pod-group taxonomy is
  ~25 MB. So we keep a handful of workers per group and every group,
  which preserves what the UI actually renders (group ordering, labels,
  collapse behaviour, both instruments) at a fraction of the size.
* **Time.** App logs are cut to a ~20-minute stretch of real observing,
  which still holds ~30 LSSTCam and ~20 LATISS exposures — enough for
  multi-dataId histogram bins and a populated failures table.

The ``k8s/events`` stream is kept for the **whole night** regardless: it
is 2.5 MB for all 576 pods, and the only ``POD_RESTARTED`` events in the
night happen during a rollout at 14:40 UTC, hours before any observing.
Cutting it to the app-log window would leave the restarts table empty.

Nothing here is synthesised. Every line is a real captured log line at
its real timestamp; the only edit is which lines are included.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from ra_log_explorer import parse  # noqa: E402
from ra_log_explorer.fetch import (  # noqa: E402
    LIVE_SIDECAR_VERSION,
    PODS_DIR_NAME,
    PODS_EVENTS_DIR_NAME,
    PODS_LIST_NAME,
    firstOffsetAtOrAfter,
)

UTC = dt.timezone.utc

DAY_OBS = 20260711
NIGHT_FROM_ISO = "2026-07-11T12:00:00.000000Z"
NIGHT_TO_ISO = "2026-07-12T12:00:00.000000Z"

# The app-log windows. Real observing, both instruments active, and dense
# enough in tracebacks that the night view's failure table is populated.
#
# Two of them, because an exposure id collides across instruments but a
# *time* does not: each instrument counts its own sequence from 1 each
# night, so LSSTCam ids 427..458 are taken at 04:10-04:30 while LATISS's
# ids 427..458 — different exposures, same numbers — are taken an hour
# later. Cutting a single time window would give a corpus with no
# colliding ids at all, and the instrument pin is exactly the thing that
# most needs testing against real ones. So LSSTCam and instrument-neutral
# pods get the first window; LATISS pods get both, which puts logs behind
# all 32 shared ids. (LATISS pods are small: the second window is ~3 MB.)
WINDOW_CAM = (dt.datetime(2026, 7, 12, 4, 12, tzinfo=UTC), dt.datetime(2026, 7, 12, 4, 29, tzinfo=UTC))
WINDOW_LATISS = (dt.datetime(2026, 7, 12, 5, 22, tzinfo=UTC), dt.datetime(2026, 7, 12, 5, 34, tzinfo=UTC))
WINDOW_FROM, WINDOW_TO = WINDOW_CAM[0], WINDOW_LATISS[1]


def windowsFor(pod: str) -> list[tuple[dt.datetime, dt.datetime]]:
    """The app-log spans to keep for one pod, in time order."""
    if parse.podInstrument(pod) == "LATISS":
        return [WINDOW_CAM, WINDOW_LATISS]
    return [WINDOW_CAM]


# How many pods to keep per (instrument, group), biggest-in-window first.
# Every group in the taxonomy is represented; the counts are what keeps
# the corpus small. `aos` and `step1b-aos` get more because night mode
# renders only those, and `sfm` gets more because the explore view's
# fan-out is the thing it exists to show.

POD_BUDGET: dict[tuple[str | None, str], int] = {
    ("LSSTCam", "head"): 1,
    ("LSSTCam", "sfm"): 6,
    ("LSSTCam", "aos"): 2,
    ("LSSTCam", "step1b"): 2,
    ("LSSTCam", "step1b-aos"): 2,
    ("LSSTCam", "metadata-server-aos"): 1,
    ("LSSTCam", "plotter"): 1,
    ("LSSTCam", "psf-plot"): 2,
    ("LSSTCam", "zernike-plot"): 2,
    ("LSSTCam", "fwhm-plot"): 1,
    ("LSSTCam", "radial-plot"): 1,
    ("LSSTCam", "guider"): 1,
    ("LSSTCam", "one-off-postisr"): 1,
    ("LSSTCam", "one-off-visitimage"): 1,
    ("LSSTCam", "butler-watcher"): 1,
    ("LSSTCam", "cluster-mgr"): 1,
    ("LATISS", "head"): 1,
    ("LATISS", "sfm"): 2,
    ("LATISS", "step1b"): 1,
    ("LATISS", "one-off-postisr"): 1,
    ("LATISS", "one-off-exprecord"): 1,
    ("LATISS", "butler-watcher"): 1,
}


def sliceFile(src: Path, dst: Path, windows: list[tuple[dt.datetime, dt.datetime]]) -> tuple[int, int]:
    """Copy ``src``'s lines falling in any of ``windows`` to ``dst``.

    Windows are applied in order and concatenated, so the output stays
    time-ascending — which the parser and the byte-bisect slicer both
    rely on.
    """
    limit = src.stat().st_size
    if limit <= 0:
        return 0, 0
    blobs: list[bytes] = []
    with open(src, "rb") as fh:
        for fromT, toT in windows:
            a = firstOffsetAtOrAfter(fh, fromT, limit)
            b = firstOffsetAtOrAfter(fh, toT, limit)
            if b > a:
                fh.seek(a)
                blobs.append(fh.read(b - a))
    blob = b"".join(blobs)
    if not blob:
        return 0, 0
    dst.write_bytes(blob)
    return len(blob), blob.count(b"\n")


def choosePods(master: Path, meta: dict) -> list[str]:
    """Pick the pods to keep: biggest-in-window first, capped per group."""
    byGroup: dict[tuple[str | None, str], list[tuple[int, str]]] = collections.defaultdict(list)
    for pod in sorted(meta["pod_lines"]):
        f = master / PODS_DIR_NAME / f"{pod}.jsonl"
        if not f.exists():
            continue
        size = 0
        with open(f, "rb") as fh:
            limit = f.stat().st_size
            for fromT, toT in windowsFor(pod):
                a = firstOffsetAtOrAfter(fh, fromT, limit)
                b = firstOffsetAtOrAfter(fh, toT, limit)
                size += max(0, b - a)
        if size:
            byGroup[(parse.podInstrument(pod), parse.podGroup(pod))].append((size, pod))
    chosen: list[str] = []
    for key, budget in POD_BUDGET.items():
        found = sorted(byGroup.get(key, []), reverse=True)
        if not found:
            print(f"  !! no pod active in the window for {key}", file=sys.stderr)
        chosen.extend(pod for _, pod in found[:budget])
    return sorted(chosen)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--master",
        type=Path,
        default=Path("/Users/merlin/temp/log_explorer_data/master/july11-night"),
        help="a night dir fetched by this tool (the all-pods window for one dayObs)",
    )
    ap.add_argument(
        "--exposure-times",
        type=Path,
        default=Path("/Users/merlin/temp/log_explorer_data/app-cache/exposure-times/summit.json"),
    )
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "july11")
    args = ap.parse_args()

    master: Path = args.master
    meta = json.loads((master / "_meta.json").read_text())
    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)

    nightDir = out / "yagan" / "rapid-analysis" / "2026-07-11T120000_000000Z__2026-07-12T120000_000000Z"
    (nightDir / PODS_DIR_NAME).mkdir(parents=True)
    (nightDir / PODS_EVENTS_DIR_NAME).mkdir(parents=True)

    print("choosing pods...")
    pods = choosePods(master, meta)
    print(f"  {len(pods)} pods")

    records: dict[str, dict] = {}
    for pod in pods:
        nbytes, lines = sliceFile(
            master / PODS_DIR_NAME / f"{pod}.jsonl",
            nightDir / PODS_DIR_NAME / f"{pod}.jsonl",
            windowsFor(pod),
        )
        # Lifecycle events: the whole night, not the app-log window (see
        # the module docstring — it is tiny, and the restarts all happen
        # during a rollout hours before observing starts).
        evSrc = master / PODS_EVENTS_DIR_NAME / f"{pod}.jsonl"
        evBytes = evLines = 0
        if evSrc.exists() and evSrc.stat().st_size:
            blob = evSrc.read_bytes()
            (nightDir / PODS_EVENTS_DIR_NAME / f"{pod}.jsonl").write_bytes(blob)
            evBytes, evLines = len(blob), blob.count(b"\n")
        records[pod] = {
            "watermarkIso": NIGHT_TO_ISO,
            "bytes": nbytes,
            "lines": lines,
            "eventBytes": evBytes,
            "eventLines": evLines,
        }

    (nightDir / PODS_LIST_NAME).write_text("\n".join(pods) + "\n")
    # The night's real fall-short state, narrowed to the pods we kept, so
    # the incomplete-fetch banner is driven by something that really
    # happened rather than a hand-written flag.
    incomplete = {p: why for p, why in (meta.get("incomplete_pods") or {}).items() if p in records}
    errors = {p: why for p, why in (meta.get("errors") or {}).items() if p in records}
    sidecar = {
        "version": LIVE_SIDECAR_VERSION,
        "dayObs": DAY_OBS,
        "fromIso": NIGHT_FROM_ISO,
        "toIso": NIGHT_TO_ISO,
        "watermarkIso": NIGHT_TO_ISO,
        "eventsWatermarkIso": NIGHT_TO_ISO,
        "finalised": False,
        "updatedAt": "2026-07-12T12:00:00+00:00",
        "pods": records,
        "eventPods": {},
        "errors": errors,
        "incomplete_pods": incomplete,
    }
    (nightDir / "_live.json").write_text(json.dumps(sidecar, indent=2, sort_keys=True))

    # Exposure records for every exposure whose shutter close lands in
    # the app-log window, both instruments, keyed exactly as the app
    # keys them (bare id = probe-order winner, plus per-instrument).
    src = json.loads(args.exposure_times.read_text())
    keep: dict[str, dict] = {}
    inWindow: dict[str, list[int]] = collections.defaultdict(list)
    for key, rec in src.items():
        iso = rec.get("obs_end")
        if not isinstance(iso, str):
            continue
        obsEndUtc = dt.datetime.fromisoformat(iso).replace(tzinfo=UTC) - dt.timedelta(seconds=37)
        if not any(a - dt.timedelta(minutes=10) <= obsEndUtc < b for a, b in (WINDOW_CAM, WINDOW_LATISS)):
            continue
        keep[key] = rec
        if ":" in key:
            inWindow[rec["instrument"]].append(rec["exposure_id"])
    (out / "exposure-times").mkdir(parents=True)
    (out / "exposure-times" / "summit.json").write_text(json.dumps(keep, indent=2, sort_keys=True))

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"\nwrote {out}")
    print(f"  pods:      {len(pods)}")
    print(f"  app lines: {sum(r['lines'] for r in records.values()):,}")
    print(f"  events:    {sum(r['eventLines'] for r in records.values()):,} lines")
    counts = ", ".join(f"{k}={len(v)}" for k, v in sorted(inWindow.items()))
    print(f"  exposures: {counts}")
    cam, lat = set(inWindow.get("lsstcam", [])), set(inWindow.get("latiss", []))
    print(f"  colliding ids (both instruments): {len(cam & lat)}")
    print(f"  raw size:  {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
