"""Shared configuration and path helpers."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

# Loki basic-auth user. A deployed instance authenticates as a service
# account rather than a person, so the default is environment-overridable:
# the container sets LOKI_USERNAME and nobody has to type it into the UI.
DEFAULT_USERNAME = os.environ.get("LOKI_USERNAME") or "merlin"
DEFAULT_WORKERS = 8
# Window padding around the user's t-zero. The CLI applies the TAI→UTC
# conversion internally so t-zero is the actual shutter-close UTC moment;
# we shouldn't ever need to look at logs from before then for a given
# dataId (if we do, that's a real anomaly, not a window-size problem).
# A small pre-shutter buffer just covers clock skew between camera / cluster.
DEFAULT_WINDOW_BEFORE_S = 5.0
DEFAULT_WINDOW_AFTER_S = 5 * 60.0
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


def settingsFilePath() -> Path:
    """Return the location of the persisted app-settings JSON.

    Defaults to ``~/.config/ra_log_explorer/settings.json``. Tests and
    scripted runs can redirect it with ``RA_LOG_EXPLORER_CONFIG_DIR`` so
    they don't share state with the real user. Kept as a public helper
    so :mod:`.appSettings` (which writes the file) and :func:`cache_root`
    (which reads it directly to avoid an import cycle) agree on the
    one true path.
    """
    override = os.environ.get("RA_LOG_EXPLORER_CONFIG_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".config" / "ra_log_explorer"
    return base / "settings.json"


def cache_root() -> Path:
    """Return the on-disk cache root, creating it if needed.

    Resolution order:

    1. The ``RA_LOG_EXPLORER_CACHE`` env var, if set — useful for tests
       and for scripted runs where the user wants a one-off override
       that ignores persisted settings entirely.
    2. The ``cacheDir`` field of the persisted app settings, if set.
       This is what the settings panel in the home view writes.
    3. The XDG-style default at ``~/.cache/ra_log_explorer``.

    The settings JSON is read here by hand (instead of via
    :mod:`.appSettings`) so this module stays an import leaf — making
    it safe for the settings module to depend on config rather than
    the other way around.
    """
    override = os.environ.get("RA_LOG_EXPLORER_CACHE")
    if override:
        root = Path(override).expanduser()
    else:
        settingsDir = _readPersistedCacheDir()
        if settingsDir:
            root = Path(settingsDir).expanduser()
        else:
            root = Path.home() / ".cache" / "ra_log_explorer"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _readPersistedCacheDir() -> str | None:
    """Best-effort read of the ``cacheDir`` setting from the appSettings
    JSON, returning ``None`` on any failure (file missing, malformed,
    field absent). Kept private to this module so :func:`cache_root`
    can call it without needing to import :mod:`.appSettings` and
    risking an import cycle.
    """
    import json

    path = settingsFilePath()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    val = raw.get("cacheDir") if isinstance(raw, dict) else None
    return val if isinstance(val, str) and val else None


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
