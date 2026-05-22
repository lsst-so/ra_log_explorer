"""Resolve a dataId to its shutter-close ISOT (TAI) via the RSP ConsDB.

The RSP exposes a SQL-style query endpoint at
``https://usdf-rsp.slac.stanford.edu/consdb/query``. We POST a single
``SELECT obs_end FROM cdb_<instrument>.exposure WHERE exposure_id = N``
and get back a JSON envelope::

    {"columns": ["obs_end"], "data": [["2026-05-20T08:46:16.267000"]]}

``obs_end`` is the shutter-close moment in **TAI**, matching the Butler
`DimensionRecord.timespan.end.isot` convention — i.e. the same scale
the previous JSON-file lookup returned, so the rest of the codebase
needs no other change.

A bearer token is required. By default we read it from
``~/.lsst/log-browser-token.txt`` (the path the RSP team's own docs
use), but the path is overridable both via the
``RA_LOG_EXPLORER_RSP_TOKEN_FILE`` environment variable and via a
per-request override the home-page UI sends. The token itself never
appears in env-vars, URLs, or response bodies — only its file path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import cache_root

TAI_MINUS_UTC_S = 37.0
RSP_TOKEN_FILE_ENV = "RA_LOG_EXPLORER_RSP_TOKEN_FILE"
DEFAULT_RSP_TOKEN_FILE = Path.home() / ".lsst" / "log-browser-token.txt"
CONSDB_URL = "https://usdf-rsp.slac.stanford.edu/consdb/query"

# On-disk cache: `dataId (as string) -> obs_end ISO (TAI)`. Exposure
# end-times are immutable once a record exists, so caching is free of
# staleness concerns. The cache file lives next to the Loki window
# cache so a `rm -rf ~/.cache/ra_log_explorer` still resets everything.
EXPOSURE_TIME_CACHE_NAME = "exposure-times.json"

# Instruments to probe in order — first match wins. LSSTCam first because
# that's where ~all current rapid-analysis traffic comes from; the rest
# are cheap to retry if the first table doesn't have the row.
INSTRUMENTS_BY_PROBE_ORDER: tuple[str, ...] = (
    "lsstcam",
    "latiss",
    "lsstcomcam",
    "lsstcomcamsim",
)


class ConsDbError(RuntimeError):
    """The ConsDB query failed for a reason we can't recover from."""


def rspTokenFilePath(override: str | None = None) -> Path:
    """Resolve the path to read the RSP bearer token from.

    Resolution order: explicit ``override`` (typically from the home
    page UI), then ``RA_LOG_EXPLORER_RSP_TOKEN_FILE``, then the
    default ``~/.lsst/log-browser-token.txt``. ``~`` is expanded
    against the *server's* HOME — which is the user that started the
    process, the only sensible interpretation.
    """
    if override:
        return Path(override).expanduser()
    fromEnv = os.environ.get(RSP_TOKEN_FILE_ENV)
    if fromEnv:
        return Path(fromEnv).expanduser()
    return DEFAULT_RSP_TOKEN_FILE


def readRspToken(path: Path) -> str:
    """Read and whitespace-strip the bearer token from ``path``.

    Raises :exc:`OSError` if the file can't be read; returns the empty
    string only if the file exists but contains only whitespace.
    """
    return path.read_text().strip()


def queryIsot(dataId: int, token: str, instrument: str | None = None) -> str | None:
    """Return the shutter-close ISO (TAI) for ``dataId``, or ``None``.

    If ``instrument`` is omitted we probe each of
    :data:`INSTRUMENTS_BY_PROBE_ORDER` in turn and return the first
    match. Raises :exc:`ConsDbError` for HTTP-level failures other
    than a missing row.
    """
    instruments = (instrument,) if instrument else INSTRUMENTS_BY_PROBE_ORDER
    for inst in instruments:
        iso = _queryOne(dataId, token, inst)
        if iso is not None:
            return iso
    return None


def _queryOne(dataId: int, token: str, instrument: str) -> str | None:
    """POST one SELECT against ``cdb_<instrument>.exposure`` for this id.

    Returns ``None`` when this instrument's table doesn't have the row
    (or doesn't exist) so the caller can fall through to the next
    instrument. Raises :exc:`ConsDbError` for anything else — bad
    token, network failure, etc.
    """
    body = json.dumps({"query": _sqlFor(dataId, instrument)}).encode("utf-8")
    req = Request(
        CONSDB_URL,
        data=body,
        headers={
            "accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=15.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        errBody = ""
        try:
            errBody = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — body is best-effort
            pass
        # 400 / 404: row or table missing — try next instrument.
        if e.code in (400, 404):
            return None
        # 500 with an "UndefinedTable" SQL error means we asked for an
        # instrument table that doesn't exist (e.g. ConsDB dropped or
        # renamed a schema). Treat as "no row here, try next" rather
        # than failing the whole lookup — exactly how a stale entry in
        # our instrument probe list should behave.
        if e.code == 500 and ("UndefinedTable" in errBody or "does not exist" in errBody):
            return None
        raise ConsDbError(f"ConsDB HTTP {e.code}: {e.reason}") from e
    rows = payload.get("data") or []
    if not rows:
        return None
    cols = payload.get("columns") or []
    try:
        idx = cols.index("obs_end")
    except ValueError:
        return None
    val = rows[0][idx]
    return val if isinstance(val, str) else None


# ----- on-disk cache --------------------------------------------------------


def cachedExposureTimesPath() -> Path:
    """Where we persist the dataId → obs_end map across runs."""
    return cache_root() / EXPOSURE_TIME_CACHE_NAME


def lookupCached(dataId: int) -> str | None:
    """Return a previously-cached ``obs_end`` for ``dataId``, or ``None``.

    The cache is best-effort: any read error (missing file, invalid
    JSON, unexpected schema) is swallowed and we return ``None`` so the
    caller falls through to a fresh ConsDB query.
    """
    p = cachedExposureTimesPath()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    val = d.get(str(dataId))
    return val if isinstance(val, str) else None


def storeCached(dataId: int, iso: str) -> None:
    """Persist ``(dataId, iso)`` in the on-disk cache.

    Best-effort: any I/O error is swallowed (the cache is purely an
    optimisation). Records here never need to be invalidated — once a
    `cdb_*.exposure.obs_end` row exists in ConsDB, it's immutable.
    """
    p = cachedExposureTimesPath()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                if isinstance(raw, dict):
                    existing = raw
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing[str(dataId)] = iso
        p.write_text(json.dumps(existing, sort_keys=True, indent=2))
    except OSError:
        pass


def _sqlFor(dataId: int, instrument: str) -> str:
    """SQL we'll send to ConsDB.

    ``dataId`` is server-validated as ``int`` upstream and ``instrument``
    comes from :data:`INSTRUMENTS_BY_PROBE_ORDER`, so neither needs
    further escaping.
    """
    return f"SELECT obs_end FROM cdb_{instrument}.exposure WHERE exposure_id = {dataId}"
