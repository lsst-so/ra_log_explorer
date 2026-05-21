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
# Window padding around the user's t-zero. We default to a generous 60 s
# before because the user-supplied t-zero is typically the shutter-close
# DimensionRecord time, which can be in TAI (37 s ahead of UTC) and which
# in any case precedes the head node's "Defining visit" by however long
# readout + Butler ingest takes. 60 s comfortably captures both.
DEFAULT_WINDOW_BEFORE_S = 60.0
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


def window_cache_dir(cluster: str, namespace: str, fromIso: str, toIso: str) -> Path:
    """Return the cache directory for a specific (cluster, namespace, window)."""
    # Sanitize ISO strings: replace ':' (filesystem-unfriendly on some platforms)
    fromSlug = fromIso.replace(":", "").replace(".", "_")
    toSlug = toIso.replace(":", "").replace(".", "_")
    path = cache_root() / cluster / namespace / f"{fromSlug}__{toSlug}"
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
