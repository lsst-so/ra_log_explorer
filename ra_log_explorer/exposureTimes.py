"""Resolve a dataId to its shutter-close ISOT (TAI) via a remote JSON service.

Exposure timings are pre-computed and written one JSON file per ``day_obs``,
mapping ``str(exposureId) -> isot``. The producer side is roughly::

    def writeRecords(records: Iterable[DimensionRecord], rootDir: str | Path) -> None:
        rootDir = Path(rootDir)
        rootDir.mkdir(parents=True, exist_ok=True)
        byDay: dict[int, dict[int, str]] = defaultdict(dict)
        for r in records:
            byDay[r.day_obs][r.id] = r.timespan.end.isot
        for dayObs, entries in byDay.items():
            (rootDir / f"{dayObs}.json").write_text(json.dumps(entries))

The ``isot`` values are always TAI (the Butler `DimensionRecord` convention),
so consumers of this module don't need to worry about timezones — the
returned string is the shutter-close moment in TAI.

The base URL is held in the ``RA_LOG_EXPLORER_EXPOSURE_TIMINGS_URL``
environment variable rather than committed into the repo, so the
operational endpoint stays out of source control.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from urllib.error import HTTPError
from urllib.request import urlopen

EXPOSURE_TIMINGS_URL_ENV = "RA_LOG_EXPLORER_EXPOSURE_TIMINGS_URL"


def exposureTimingsUrl() -> str | None:
    """Return the configured exposure-timings base URL, or ``None`` if unset."""
    return os.environ.get(EXPOSURE_TIMINGS_URL_ENV)


@lru_cache(maxsize=32)
def _loadDay(rootUrl: str, dayObs: int) -> dict[str, str] | None:
    url = f"{rootUrl.rstrip('/')}/{dayObs}.json"
    try:
        with urlopen(url) as resp:
            return json.load(resp)
    except HTTPError as e:
        if e.code == 404:
            return None
        raise


def queryIsot(dataId: int, rootUrl: str) -> str | None:
    """Map a 13-digit dataId to its shutter-close TAI ISOT string.

    Returns ``None`` if the lookup can't resolve the exposure — either
    because the day's JSON file does not exist (404), or the exposure
    is not present in that day's mapping. Raises for any other HTTP
    error response.
    """
    data = _loadDay(rootUrl, dataId // 100000)
    if data is None:
        return None
    return data.get(str(dataId))
