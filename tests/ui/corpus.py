"""The UI tests' log corpus, and how a test stages it on disk.

One archive (``tests/data/ui/july11.tar.gz``, built by
``tests/data/ui/build_fixture.py``) holds a cut-down but entirely real
night: 32 pods of captured rapid-analysis logs from dayObs 20260711 on
the summit, both instruments, plus the ConsDB exposure records for the
exposures in it. It is laid out as a **live night dir** — the shape the
poller maintains — which is what lets every other window a test needs be
produced from it by the application's own slicing code rather than by a
second fixture. A test asking for one exposure's window gets exactly the
bytes ``fetch.materializeNightSlice`` would hand a real user.

The constants below are facts about that data, hand-checked against it.
Rebuilding the corpus with different windows or pod budgets means
re-checking them; that is the point of pinning them here rather than
scattering magic numbers through the tests.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from ra_log_explorer import exposureTimes, fetch
from ra_log_explorer.config import (
    NIGHT_AOS_POD_REGEX,
    FetchSpec,
    dayObsEndUtc,
    dayObsStartUtc,
    windowCachePath,
)

ARCHIVE = Path(__file__).resolve().parents[1] / "data" / "ui" / "july11.tar.gz"

UTC = dt.timezone.utc

# ----- facts about the corpus ----------------------------------------------

SITE = "summit"
CLUSTER = "yagan"
NAMESPACE = "rapid-analysis"
DAY_OBS = 20260711

# An exposure id that exists on *both* instruments — which is the normal
# case, not an edge case: each instrument counts its own sequence from 1
# each night, so on any night both observe, the low ids collide. The two
# shutter closes are an hour apart, which is what makes the instrument
# pin observable rather than theoretical.
SHARED_ID = 2026071100445
CAM_T_ZERO_UTC = dt.datetime(2026, 7, 12, 4, 21, 22, 502000, tzinfo=UTC)
LATISS_T_ZERO_UTC = dt.datetime(2026, 7, 12, 5, 24, 53, 895000, tzinfo=UTC)

# The exposure a real crash loop is planted onto (see plantPodCrash):
# 2026071100448 is the visit that pod was really gathering, and the
# transplanted events are timed to wedge a minute into its window.
CRASH_ID = 2026071100448
CRASH_POD = "s-lsstcam-run-step-1b-aos-worker-gather1baosset-0"

# Three consecutive LSSTCam exposures, for range mode.
RANGE_START = 2026071100445
RANGE_STOP = 2026071100447

# A dataId with no exposure record in the corpus at all, for the
# "ConsDB can't resolve this" path.
UNKNOWN_ID = 2026071199999

DEFAULT_WINDOW_BEFORE_S = 5.0
DEFAULT_WINDOW_AFTER_S = 300.0


@dataclass(frozen=True)
class StagedCorpus:
    """A cache root with the corpus in it, plus helpers to stage windows."""

    root: Path

    @property
    def nightDir(self) -> Path:
        return windowCachePath(
            CLUSTER,
            NAMESPACE,
            fetch._fmtLogcliTime(dayObsStartUtc(DAY_OBS)),
            fetch._fmtLogcliTime(dayObsEndUtc(DAY_OBS)),
        )

    def spec(self, fromT: dt.datetime, toT: dt.datetime, podRegex: str | None = None) -> FetchSpec:
        return FetchSpec(
            lokiAddr="https://loki.invalid",
            username="ui-test",
            cluster=CLUSTER,
            namespace=NAMESPACE,
            fromIso=fetch._fmtLogcliTime(fromT),
            toIso=fetch._fmtLogcliTime(toT),
            podRegex=podRegex,
        )

    def tZero(self, dataId: int, instrument: str) -> dt.datetime:
        """The exposure's shutter close in UTC, from the corpus's records."""
        record = exposureTimes.lookupCachedRecord(dataId, siteName=SITE, instrument=instrument)
        assert record is not None, f"no {instrument} record for {dataId} in the corpus"
        iso = exposureTimes.obsEnd(record)
        assert iso is not None
        naive = dt.datetime.fromisoformat(iso)
        return naive.replace(tzinfo=UTC) - dt.timedelta(seconds=exposureTimes.TAI_MINUS_UTC_S)

    def stageExposure(
        self,
        dataId: int,
        instrument: str,
        *,
        windowAfterS: float = DEFAULT_WINDOW_AFTER_S,
        windowBeforeS: float = DEFAULT_WINDOW_BEFORE_S,
    ) -> Path:
        """Materialise one exposure's window as an ordinary cache dir.

        Sliced out of the night dir by the real code path, then marked
        with the dataId that "triggered" it — which is what lets the
        server rebuild the view from disk on a deep link, exactly as it
        would after somebody else had fetched it.
        """
        t0 = self.tZero(dataId, instrument)
        spec = self.spec(t0 - dt.timedelta(seconds=windowBeforeS), t0 + dt.timedelta(seconds=windowAfterS))
        cacheDir, _ = fetch.materializeNightSlice(self.nightDir, spec)
        fetch.addExposureToCache(cacheDir, dataId)
        fetch.markCacheViewed(cacheDir)
        return cacheDir

    def stageNight(self) -> Path:
        """Materialise the AOS night window night mode asks for."""
        spec = self.spec(dayObsStartUtc(DAY_OBS), dayObsEndUtc(DAY_OBS), NIGHT_AOS_POD_REGEX)
        cacheDir, _ = fetch.materializeNightSlice(self.nightDir, spec)
        fetch.markCacheViewed(cacheDir)
        return cacheDir

    def stageRange(self, startId: int, stopId: int, instrument: str = "lsstcam") -> Path:
        """Materialise the one wide window a range fetch produces."""
        fromT = self.tZero(startId, instrument) - dt.timedelta(seconds=DEFAULT_WINDOW_BEFORE_S)
        toT = self.tZero(stopId, instrument) + dt.timedelta(seconds=DEFAULT_WINDOW_AFTER_S)
        cacheDir, _ = fetch.materializeNightSlice(self.nightDir, self.spec(fromT, toT))
        fetch.markCacheRange(cacheDir, startId, stopId, instrument=instrument)
        fetch.markCacheViewed(cacheDir)
        return cacheDir

    def dropLiveSidecar(self) -> None:
        """Stop the night dir being sliceable, so a fetch really fetches.

        Call after staging whatever windows the test needs: while the
        sidecar is there, ``fetchAll`` serves any window inside the night
        by slicing it, which is right in production and useless in a test
        whose subject is the fetch.
        """
        (self.nightDir / fetch.LIVE_SIDECAR_NAME).unlink(missing_ok=True)

    def plantPodCrash(self) -> str:
        """Drop a real crash loop into one of the night's pods.

        The captured nights we have are healthy ones: their lifecycle
        streams hold `Started` and `Killing` and nothing else. A crash is
        the case these markers exist for — "the pod died here" is the
        answer to an app log that stops mid-work — so the corpus gets a
        real one transplanted in, from the same StatefulSet on BTS, with
        only its timestamps moved onto this night (see the
        ``podCrashEventsJsonl`` fixture for provenance).

        Kept out of the archive and planted per test on purpose: the
        archive is one honest cut of one night, and this is the one thing
        in the suite that came from somewhere else. It goes onto the pod
        of the same name — the same StatefulSet member — and is timed to
        wedge inside :data:`CRASH_ID`, the visit that pod really was
        gathering. Returns the pod, which is AOS-flavoured, so the crash
        reaches night mode as well as that exposure's timeline.
        """
        source = Path(__file__).resolve().parents[1] / "data" / "pod_crash_events.jsonl"
        pod = CRASH_POD
        target = self.nightDir / fetch.PODS_EVENTS_DIR_NAME / f"{pod}.jsonl"
        assert (self.nightDir / fetch.PODS_DIR_NAME / f"{pod}.jsonl").exists(), pod
        # Unlink before writing: the corpus's JSONL is hard-linked into
        # this cache root, and writing in place would go straight through
        # to the copy every other test shares.
        target.unlink(missing_ok=True)
        target.write_bytes(source.read_bytes())
        sidecar = fetch.readLiveSidecar(self.nightDir)
        if sidecar is not None:
            record = sidecar["pods"][pod]
            record["eventBytes"] = target.stat().st_size
            record["eventLines"] = target.read_bytes().count(b"\n")
            fetch.writeLiveSidecar(self.nightDir, sidecar)
        return pod

    def rewindWatermark(self, to: dt.datetime) -> None:
        """Pretend the night has only been fetched up to ``to``.

        The archive ships a night that is complete on disk and says so,
        which is the right default — most tests want to open a view, not
        watch one arrive. Live mode is the exception: its whole point is
        what happens *while* the night is still being fetched, so the
        watermark is wound back and the poller advances it again from
        there. The bytes stay put; the watermark is what gates
        visibility, and slices are still cut by time.
        """
        sidecar = fetch.readLiveSidecar(self.nightDir)
        assert sidecar is not None, "no live sidecar to rewind"
        iso = fetch._fmtLogcliTime(to)
        sidecar["watermarkIso"] = iso
        sidecar["eventsWatermarkIso"] = iso
        for record in sidecar["pods"].values():
            record["watermarkIso"] = iso
        fetch.writeLiveSidecar(self.nightDir, sidecar)

    def podFiles(self) -> list[Path]:
        return sorted((self.nightDir / fetch.PODS_DIR_NAME).glob("*.jsonl"))


def unpackCorpus(target: Path) -> Path:
    """Unpack the archive into ``target`` (once), returning the tree root.

    Safe under ``pytest-xdist``: every worker builds its own copy under a
    unique name and then atomically renames it into place, so whichever
    gets there first wins and the rest reuse it. No lock file, nothing to
    clean up if a worker dies mid-unpack.

    The stamp file makes a rebuilt archive replace a previously-unpacked
    one. pytest keeps the last few base temp dirs, so without it a corpus
    unpacked by an older archive would quietly outlive it.
    """
    stamp = target / ".corpus-stamp"
    want = f"{ARCHIVE.stat().st_size}-{int(ARCHIVE.stat().st_mtime)}"
    if stamp.exists() and stamp.read_text() == want:
        return target
    shutil.rmtree(target, ignore_errors=True)
    staging = target.with_name(f"{target.name}.{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(ARCHIVE, "r:gz") as tf:
        tf.extractall(staging, filter="data")
    (staging / "july11" / ".corpus-stamp").write_text(want)
    try:
        os.replace(staging / "july11", target)
    except OSError:
        # Another worker got there first; theirs is as good as ours.
        shutil.rmtree(staging, ignore_errors=True)
    return target


def linkInto(corpus: Path, cacheRoot: Path) -> StagedCorpus:
    """Give a per-test cache root its own copy of the corpus.

    The per-pod JSONL is 20 MB and every test gets a fresh root, so
    copying it per test would dominate the suite; it is hard-linked
    instead. The app only ever reads those files or unlinks them (the
    cache-deletion tests), both of which a link handles.

    Everything else is *copied*, because the app writes to some of it in
    place — most obviously the per-site exposure-time cache, which a
    manual shutter close rewrites. Sharing that inode between tests let
    one test's stand-in leak into another's lookups, which is the kind of
    cross-test coupling that shows up as a test that only fails in a full
    run. The small files cost microseconds.

    The links are made read-only so that mistake cannot happen twice: an
    in-place write to a shared inode now raises instead of quietly
    rewriting the corpus for every test that follows. Removing them still
    works — deletion needs the *directory* to be writable, not the file —
    which is what the cache-deletion tests do.
    """
    for src in corpus.rglob("*"):
        rel = src.relative_to(corpus)
        dst = cacheRoot / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".jsonl":
            src.chmod(0o444)
            os.link(src, dst)
        else:
            shutil.copy2(src, dst)
    return StagedCorpus(root=cacheRoot)
