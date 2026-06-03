"""Persistent app-wide settings.

Lives at ``~/.config/ra_log_explorer/settings.json`` (a fixed
location, deliberately *not* inside the cache directory — the cache
location itself is one of the settings here, so the file that
records it needs to be discoverable without knowing where the cache
is). The only writer is the ``PUT /api/settings`` endpoint; the
readers are the home-view side panel, the cache-eviction code in
:mod:`.fetch`, and :func:`config.cache_root` (the last reads the
JSON directly to avoid an import cycle).

Anything UI-only (workers, cluster, namespace, Loki URL, credentials)
stays in the browser's ``localStorage`` — only settings the *server*
needs to act on are persisted here. The structure is open-ended so
we can add fields without breaking older settings files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import settingsFilePath

# 5 GiB by default. Generous enough that a typical analysis session
# doesn't trip eviction; small enough that an unattended browser tab
# can't slowly fill the disk over weeks.
DEFAULT_MAX_CACHE_BYTES = 5 * 1024 * 1024 * 1024


@dataclass
class AppSettings:
    """Server-side settings that affect on-disk behaviour."""

    maxCacheBytes: int = DEFAULT_MAX_CACHE_BYTES
    # User-supplied override for the on-disk cache root. ``None`` means
    # "fall through to the ``RA_LOG_EXPLORER_CACHE`` env var, then the
    # built-in default". Honoured live: the next ``cache_root()`` call
    # picks up the new path without a server restart, so future fetches
    # land in the new location and the cache table re-lists from there.
    # Already-loaded in-memory states keep working because they hold
    # absolute paths to their original cache windows.
    cacheDir: str | None = None


def settingsPath() -> Path:
    return settingsFilePath()


def loadAppSettings() -> AppSettings:
    """Read settings from disk, returning defaults if the file is
    missing or malformed.

    A corrupt settings file shouldn't break the app — we silently fall
    back to defaults so the user can fix it via the UI rather than
    having to hand-edit JSON.
    """
    p = settingsPath()
    if not p.exists():
        return AppSettings()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    cacheDir = raw.get("cacheDir")
    return AppSettings(
        maxCacheBytes=int(raw.get("maxCacheBytes", DEFAULT_MAX_CACHE_BYTES)),
        cacheDir=cacheDir if isinstance(cacheDir, str) and cacheDir else None,
    )


def saveAppSettings(settings: AppSettings) -> None:
    """Persist settings to disk, creating the parent dir if needed."""
    p = settingsPath()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"maxCacheBytes": settings.maxCacheBytes}
    if settings.cacheDir:
        payload["cacheDir"] = settings.cacheDir
    p.write_text(json.dumps(payload, indent=2))
