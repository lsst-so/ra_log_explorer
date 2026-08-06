"""Shared configuration and path helpers.

Everything a deployment needs to vary between the summit and the Base Test
Stand is an environment variable read here, once, at import. Nothing is
configurable from the browser: the UI is for asking questions about
exposures, not for reconfiguring the service that answers them.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """An environment variable holds something we can't make sense of."""


def _envInt(name: str, default: int) -> int:
    """Read an int from the environment, or return ``default``.

    A malformed value raises rather than quietly falling back. These come
    from Helm values in a deployment, and a typo that silently reverted to
    the built-in default would stay invisible until someone eventually
    wondered why the setting never took effect — whereas a container that
    refuses to start says so immediately.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer; got {raw!r}") from e


def _envFloat(name: str, default: float) -> float:
    """Read a float from the environment, or return ``default``.

    Fails loudly on a malformed value, for the reasons in :func:`_envInt`.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number; got {raw!r}") from e


# Loki basic-auth user. A deployed instance authenticates as a service
# account rather than a person, so the default is environment-overridable:
# the container sets LOKI_USERNAME and nobody has to type it into the UI.
DEFAULT_USERNAME = os.environ.get("LOKI_USERNAME") or "merlin"
DEFAULT_WORKERS = _envInt("RA_LOG_EXPLORER_WORKERS", 8)
# Window padding around the user's t-zero. The CLI applies the TAI→UTC
# conversion internally so t-zero is the actual shutter-close UTC moment;
# we shouldn't ever need to look at logs from before then for a given
# dataId (if we do, that's a real anomaly, not a window-size problem).
# A small pre-shutter buffer just covers clock skew between camera / cluster.
# These are the *starting* values of the per-fetch window fields, so a
# deployment can tune them to its own pipeline's timings without taking the
# widen-the-window workflow away from whoever is investigating.
DEFAULT_WINDOW_BEFORE_S = _envFloat("RA_LOG_EXPLORER_WINDOW_BEFORE_S", 5.0)
DEFAULT_WINDOW_AFTER_S = _envFloat("RA_LOG_EXPLORER_WINDOW_AFTER_S", 5 * 60.0)
# Ceiling the LRU eviction in `fetch.evictToFit` keeps the cache under.
# Deployed, this is derived from the size of the volume provisioned for the
# cache; locally it's a figure generous enough that an ordinary session
# never trips it but small enough that a forgotten browser tab can't fill
# the disk over weeks.
MAX_CACHE_BYTES = _envInt("RA_LOG_EXPLORER_MAX_CACHE_BYTES", 5 * 1024 * 1024 * 1024)
DEFAULT_HTTP_PORT = 8780
# Env var naming the URL prefix the app is served under. Empty (the local
# default) means "served at the root"; a deployment sharing a hostname with
# the rest of the RSP sets it to e.g. ``/log-explorer``.
BASE_PATH_ENV = "RA_LOG_EXPLORER_BASE_PATH"
# Range mode fetches one wide window covering [startId, stopId]. The tool is
# meant for tens of consecutive exposures; this is a fat-finger backstop so a
# transposed/typo'd pair can't generate a multi-thousand-id ConsDB sweep or a
# pathologically wide Loki window.
MAX_RANGE_SPAN = 500


def normalizeBasePath(raw: str | None) -> str:
    """Normalize a URL prefix to the one form the router and template agree on.

    Accepts anything a human or a Helm value might supply — ``log-explorer``,
    ``/log-explorer/``, ``/``, ``None`` — and returns either the empty string
    ("served at the root") or ``/segment`` with no trailing slash. Having a
    single canonical form matters because the prefix is both string-matched
    off incoming request paths and concatenated into the HTML the browser
    gets back; the two would disagree about ``//static/...`` otherwise.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = "/" + s.strip("/")
    return "" if s == "/" else s


def defaultBasePath() -> str:
    """Base path from :data:`BASE_PATH_ENV`, for callers with no explicit flag."""
    return normalizeBasePath(os.environ.get(BASE_PATH_ENV))


def cache_root() -> Path:
    """Return the on-disk cache root, creating it if needed.

    ``RA_LOG_EXPLORER_CACHE`` if set, otherwise the XDG-style default at
    ``~/.cache/ra_log_explorer``. A deployment always sets the env var: it
    points at the volume provisioned for the cache, whose size is also what
    :data:`MAX_CACHE_BYTES` is derived from.
    """
    override = os.environ.get("RA_LOG_EXPLORER_CACHE")
    root = Path(override).expanduser() if override else Path.home() / ".cache" / "ra_log_explorer"
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

    There is deliberately no per-pod line cap. A naïve ``--limit=0`` does
    *not* suffice — logcli silently drops lines on wide, busy windows
    (grafana/loki#17270). Instead each pod is fetched in count-presized,
    single-batch time-chunks whose completeness is verified structurally,
    so a window — especially a full night — is retrieved in its entirety
    or flagged where it can't be. See :func:`fetch._fetchOnePod`.
    """

    lokiAddr: str
    username: str
    cluster: str
    namespace: str
    fromIso: str  # RFC3339Nano UTC, no timezone suffix per logcli docs
    toIso: str
    workers: int = DEFAULT_WORKERS
    podRegex: str | None = None
