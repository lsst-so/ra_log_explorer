"""Shared configuration and path helpers."""

from __future__ import annotations

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


def windowCachePath(cluster: str, namespace: str, fromIso: str, toIso: str) -> Path:
    """Return the cache directory path for a specific (cluster, namespace, window).

    Pure path computation — no filesystem I/O. Call ``ensureWindowCacheDir``
    when you actually need the directory to exist on disk.
    """
    # Sanitize ISO strings: replace ':' (filesystem-unfriendly on some platforms)
    fromSlug = fromIso.replace(":", "").replace(".", "_")
    toSlug = toIso.replace(":", "").replace(".", "_")
    return cache_root() / cluster / namespace / f"{fromSlug}__{toSlug}"


def ensureWindowCacheDir(cluster: str, namespace: str, fromIso: str, toIso: str) -> Path:
    """Return the cache directory, creating it if necessary."""
    path = windowCachePath(cluster, namespace, fromIso, toIso)
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class FetchSpec:
    """Specification of what window of logs to fetch and how."""

    lokiAddr: str
    username: str
    cluster: str
    namespace: str
    fromIso: str  # RFC3339Nano UTC, no timezone suffix per logcli docs
    toIso: str
    workers: int = DEFAULT_WORKERS
    lineLimit: int = DEFAULT_LINE_LIMIT
