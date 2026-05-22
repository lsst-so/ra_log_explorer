"""Shared configuration and path helpers."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOKI_ADDR = "https://loki-query.ls.lsst.org"
DEFAULT_USERNAME = "merlin"
DEFAULT_CLUSTER = "yagan"
DEFAULT_NAMESPACE = "rapid-analysis"
DEFAULT_WORKERS = 8
# Window padding around the user's t-zero. The CLI applies the TAI→UTC
# conversion internally so t-zero is the actual shutter-close UTC moment;
# we shouldn't ever need to look at logs from before then for a given
# dataId (if we do, that's a real anomaly, not a window-size problem).
# A small pre-shutter buffer just covers clock skew between camera / cluster.
DEFAULT_WINDOW_BEFORE_S = 5.0
DEFAULT_WINDOW_AFTER_S = 5 * 60.0
DEFAULT_HTTP_PORT = 8765
DEFAULT_LINE_LIMIT = 50_000  # per-pod safety cap; pods rarely emit this much


def cache_root() -> Path:
    """Return the on-disk cache root, creating it if needed."""
    override = os.environ.get("RA_LOG_EXPLORER_CACHE")
    if override:
        root = Path(override).expanduser()
    else:
        root = Path.home() / ".cache" / "ra_log_explorer"
    root.mkdir(parents=True, exist_ok=True)
    return root


def windowCachePath(
    cluster: str,
    namespace: str,
    fromIso: str,
    toIso: str,
    podRegex: str | None = None,
) -> Path:
    """Return the cache directory path for a specific (cluster, namespace, window).

    Pure path computation — no filesystem I/O. Call ``ensureWindowCacheDir``
    when you actually need the directory to exist on disk.

    If ``podRegex`` is supplied the window is filtered at the Loki layer
    (e.g. night-mode AOS-only fetches) and gets its own cache subdir, so
    an exposure-mode full-pod fetch and a night-mode filtered fetch over
    the same time window can coexist without overwriting each other.
    """
    # Sanitize ISO strings: replace ':' (filesystem-unfriendly on some platforms)
    fromSlug = fromIso.replace(":", "").replace(".", "_")
    toSlug = toIso.replace(":", "").replace(".", "_")
    base = cache_root() / cluster / namespace / f"{fromSlug}__{toSlug}"
    if podRegex is None:
        return base
    # Slug the regex so distinct filters don't share a dir. Filesystem-
    # unfriendly characters are folded to underscores; we keep the
    # original regex inside _meta.json so superset-reuse can still
    # match exact-regex fetches.
    safe = "".join(c if c.isalnum() else "_" for c in podRegex)
    return base / f"pods={safe}"


def ensureWindowCacheDir(
    cluster: str,
    namespace: str,
    fromIso: str,
    toIso: str,
    podRegex: str | None = None,
) -> Path:
    """Return the cache directory, creating it if necessary."""
    path = windowCachePath(cluster, namespace, fromIso, toIso, podRegex)
    path.mkdir(parents=True, exist_ok=True)
    return path


def dayObsStartUtc(dayObs: int) -> dt.datetime:
    """Return the UTC start of the given ``dayObs`` as a timezone-aware datetime.

    The observatory rolls the calendar over at UTC-12, so ``dayObs``
    20260521 means "the 24-hour window from 2026-05-21T12:00:00Z to
    2026-05-22T12:00:00Z" — i.e. the night that begins on May 21 in
    Chile.
    """
    asDate = dt.datetime.strptime(str(dayObs), "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    return asDate + dt.timedelta(hours=12)


def dayObsEndUtc(dayObs: int) -> dt.datetime:
    """Return the UTC end (exclusive) of the given ``dayObs``."""
    return dayObsStartUtc(dayObs) + dt.timedelta(hours=24)


# LogQL pod-regex used by night mode to scope the 24h fetch to AOS-flavoured
# pods only (aos-worker, step-1b-aos-worker, metadata-server-aos, …). Any
# pod whose name contains "aos" — case-sensitive on the Loki side, which
# is fine because the actual pod names are all lowercase.
NIGHT_AOS_POD_REGEX = ".*aos.*"


@dataclass(frozen=True)
class FetchSpec:
    """Specification of what window of logs to fetch and how.

    ``podRegex`` is an optional LogQL ``pod=~"…"`` filter applied at the
    Loki layer — used by the "investigate night" mode to restrict the
    24-hour fetch to the few pod-name patterns we care about (e.g.
    ``.*aos.*``). ``None`` means no filter, i.e. fetch every pod that
    logged anything in the window.
    """

    lokiAddr: str
    username: str
    cluster: str
    namespace: str
    fromIso: str  # RFC3339Nano UTC, no timezone suffix per logcli docs
    toIso: str
    workers: int = DEFAULT_WORKERS
    lineLimit: int = DEFAULT_LINE_LIMIT
    podRegex: str | None = None
