"""Persistent app-wide settings.

Lives at ``<cache_root>/settings.json``. The only writer is the
``PUT /api/settings`` endpoint; the only reader is the home-view side
panel plus the cache-eviction code in :mod:`.fetch`. Anything UI-only
(workers, cluster, namespace, Loki URL, credentials) stays in the
browser's ``localStorage`` — only settings the *server* needs to act
on are persisted here.

Today that's just the cache size limit. The structure is open-ended
so we can add fields without breaking older settings files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import cache_root

# 5 GiB by default. Generous enough that a typical analysis session
# doesn't trip eviction; small enough that an unattended browser tab
# can't slowly fill the disk over weeks.
DEFAULT_MAX_CACHE_BYTES = 5 * 1024 * 1024 * 1024

_SETTINGS_FILENAME = "settings.json"


@dataclass
class AppSettings:
    """Server-side settings that affect on-disk behaviour."""

    maxCacheBytes: int = DEFAULT_MAX_CACHE_BYTES


def settingsPath() -> Path:
    return cache_root() / _SETTINGS_FILENAME


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
    return AppSettings(
        maxCacheBytes=int(raw.get("maxCacheBytes", DEFAULT_MAX_CACHE_BYTES)),
    )


def saveAppSettings(settings: AppSettings) -> None:
    """Persist settings to disk. The cache root is created if needed."""
    p = settingsPath()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"maxCacheBytes": settings.maxCacheBytes}, indent=2))
