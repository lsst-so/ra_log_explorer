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
environment variable rather than committed into the repo.

Caching policy: per-day JSON responses are memoised with ``lru_cache`` so
re-querying within one process is cheap. The current ``day_obs`` (and any
future one — guards against clock skew) is fetched fresh every time,
since that file is still being written as the night progresses.
"""

from __future__ import annotations

import datetime
import json
import os
from functools import lru_cache
from urllib.error import HTTPError
from urllib.request import urlopen

EXPOSURE_TIMINGS_URL_ENV = "RA_LOG_EXPLORER_EXPOSURE_TIMINGS_URL"


def exposureTimingsUrl() -> str | None:
    """Return the configured exposure-timings base URL, or ``None`` if unset."""
    return os.environ.get(EXPOSURE_TIMINGS_URL_ENV)


def getCurrentDayObsDatetime() -> datetime.date:
    """Return the current ``day_obs`` as a `date`.

    The observatory rolls the date over at UTC-12, not UTC midnight.
    """
    nowUtc = datetime.datetime.now(datetime.timezone.utc)
    offset = datetime.timedelta(hours=-12)
    return (nowUtc + offset).date()


def getCurrentDayObsInt() -> int:
    """Return the current ``day_obs`` as the YYYYMMDD integer used in dataIds."""
    return int(getCurrentDayObsDatetime().strftime("%Y%m%d"))


def _fetchDay(rootUrl: str, dayObs: int) -> dict[str, str] | None:
    """Fetch one day's exposure-timings JSON. Never cached at this layer."""
    url = f"{rootUrl.rstrip('/')}/{dayObs}.json"
    try:
        with urlopen(url) as resp:
            return json.load(resp)
    except HTTPError as e:
        if e.code == 404:
            return None
        raise


@lru_cache(maxsize=64)
def _fetchDayCached(rootUrl: str, dayObs: int) -> dict[str, str] | None:
    return _fetchDay(rootUrl, dayObs)


def queryIsot(dataId: int, rootUrl: str) -> str | None:
    """Map a 13-digit dataId to its shutter-close TAI ISOT string.

    Returns ``None`` if the lookup can't resolve the exposure — either
    because the day's JSON file does not exist (404), or the exposure
    is not present in that day's mapping. Raises for any other HTTP
    error response.

    Skips the cache for the current ``day_obs`` (and anything in the
    future, as a clock-skew guard) because that file is still being
    written as observations continue.
    """
    dayObs = dataId // 100000
    fetch = _fetchDay if dayObs >= getCurrentDayObsInt() else _fetchDayCached
    data = fetch(rootUrl, dayObs)
    if data is None:
        return None
    return data.get(str(dataId))
