"""Resolve a dataId to its shutter-close ISOT (TAI) via a ConsDB endpoint.

The ConsDB exposes a SQL-style query endpoint at the site's
``consdbUrl`` (USDF / summit RSP for summit data, ``base-lsp.lsst.codes``
for the Base Test Stand sandbox). We POST a single ::

    SELECT obs_end FROM cdb_<instrument>.exposure WHERE exposure_id = N

and get back a JSON envelope::

    {"columns": ["obs_end"], "data": [["2026-05-20T08:46:16.267000"]]}

``obs_end`` is the shutter-close moment in **TAI**, matching the Butler
``DimensionRecord.timespan.end.isot`` convention — i.e. the same scale
the previous JSON-file lookup returned, so downstream code needs no
other change.

ConsDB URL and bearer-token file are **per site** (see :mod:`.sites`):
the same dataId can refer to a real-camera exposure on the summit and a
simulated exposure on BTS, with different ``obs_end`` values. Every
helper here takes either a ``Site`` directly or its ``consdbUrl`` and
the resolved bearer token explicitly, so callers can never accidentally
mix sources.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import cache_root
from .sites import Site

TAI_MINUS_UTC_S = 37.0

# On-disk cache: per-site JSON file mapping ``dataId (as string) ->
# obs_end ISO (TAI)``. Exposure end-times are immutable once a row
# exists, so caching is free of staleness concerns. The cache files
# live next to the Loki window cache so a ``rm -rf
# ~/.cache/ra_log_explorer`` still resets everything.
EXPOSURE_TIME_CACHE_DIR = "exposure-times"

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


def readToken(path: Path) -> str:
    """Read and whitespace-strip the bearer token from ``path``.

    Raises :exc:`OSError` if the file can't be read; returns the empty
    string only if the file exists but contains only whitespace.
    """
    return path.read_text().strip()


def queryIsot(dataId: int, token: str, *, consdbUrl: str, instrument: str | None = None) -> str | None:
    """Return the shutter-close ISO (TAI) for ``dataId``, or ``None``.

    If ``instrument`` is omitted we probe each of
    :data:`INSTRUMENTS_BY_PROBE_ORDER` in turn and return the first
    match. Raises :exc:`ConsDbError` for HTTP-level failures other
    than a missing row.
    """
    instruments = (instrument,) if instrument else INSTRUMENTS_BY_PROBE_ORDER
    for inst in instruments:
        iso = _queryOne(dataId, token, inst, consdbUrl=consdbUrl)
        if iso is not None:
            return iso
    return None


def queryIsotBatch(
    dataIds: Iterable[int],
    token: str,
    *,
    consdbUrl: str,
    chunkSize: int = 500,
) -> dict[int, str]:
    """Resolve many dataIds in one round trip per instrument.

    For each instrument in :data:`INSTRUMENTS_BY_PROBE_ORDER` we send
    a single ``SELECT … WHERE exposure_id IN (…)`` covering whatever
    dataIds are still unresolved. Returns ``{dataId: iso}`` for the
    matches found; dataIds with no row in any instrument's table
    simply don't appear in the output.

    ``chunkSize`` caps the IN-list size per query so an enormous
    night doesn't trip ConsDB's SQL-length limits. With the default
    500 a typical night-fetch (~600 dataIds) is two queries per
    instrument, and the instrument loop short-circuits as soon as
    all dataIds have been resolved.
    """
    out: dict[int, str] = {}
    remaining = [int(x) for x in dataIds]
    for instrument in INSTRUMENTS_BY_PROBE_ORDER:
        if not remaining:
            break
        found = _queryBatch(remaining, token, instrument, chunkSize, consdbUrl=consdbUrl)
        out.update(found)
        remaining = [d for d in remaining if d not in out]
    return out


def _queryBatch(
    dataIds: list[int],
    token: str,
    instrument: str,
    chunkSize: int,
    *,
    consdbUrl: str,
) -> dict[int, str]:
    """Send one or more ``IN (…)`` queries against one instrument's table."""
    out: dict[int, str] = {}
    for i in range(0, len(dataIds), chunkSize):
        chunk = dataIds[i : i + chunkSize]
        idsSql = ",".join(str(d) for d in chunk)
        sql = f"SELECT exposure_id, obs_end FROM cdb_{instrument}.exposure WHERE exposure_id IN ({idsSql})"
        try:
            payload = _postQuery(sql, token, consdbUrl=consdbUrl)
        except _UndefinedTableError:
            # This instrument has no schema — try the next one.
            return out
        cols = payload.get("columns") or []
        rows = payload.get("data") or []
        try:
            iIdCol = cols.index("exposure_id")
            iIsoCol = cols.index("obs_end")
        except ValueError:
            continue
        for row in rows:
            try:
                eid = int(row[iIdCol])
                iso = row[iIsoCol]
            except (IndexError, TypeError, ValueError):
                continue
            if isinstance(iso, str):
                out[eid] = iso
    return out


class _UndefinedTableError(Exception):
    """Internal: ConsDB 500'd with a SQL UndefinedTable body. Caller
    should try the next instrument rather than surface this."""


def _postQuery(sql: str, token: str, *, consdbUrl: str) -> dict:
    """POST one SQL query, return the parsed JSON payload."""
    body = json.dumps({"query": sql}).encode("utf-8")
    req = Request(
        consdbUrl,
        data=body,
        headers={
            "accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        errBody = ""
        try:
            errBody = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        if e.code == 500 and ("UndefinedTable" in errBody or "does not exist" in errBody):
            raise _UndefinedTableError() from e
        if e.code in (400, 404):
            # Empty result rather than an error.
            return {"columns": [], "data": []}
        raise ConsDbError(f"ConsDB HTTP {e.code}: {e.reason}") from e


def _queryOne(dataId: int, token: str, instrument: str, *, consdbUrl: str) -> str | None:
    """POST one SELECT against ``cdb_<instrument>.exposure`` for this id.

    Returns ``None`` when this instrument's table doesn't have the row
    (or doesn't exist) so the caller can fall through to the next
    instrument. Raises :exc:`ConsDbError` for anything else — bad
    token, network failure, etc.
    """
    body = json.dumps({"query": _sqlFor(dataId, instrument)}).encode("utf-8")
    req = Request(
        consdbUrl,
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


# ----- site-aware convenience wrappers -------------------------------------


def loadTokenForSite(site: Site) -> str:
    """Read and return the bearer token for ``site.consdbTokenFile``.

    Raises :exc:`OSError` if the file can't be read; returns the empty
    string only if the file exists but is whitespace-only. Callers
    check both conditions explicitly so they can report a clear UI
    message ("token file missing" vs "token file empty") to the user.
    """
    return readToken(site.consdbTokenFile)


# ----- on-disk cache --------------------------------------------------------


def cachedExposureTimesPath(siteName: str) -> Path:
    """Where we persist the per-site dataId → obs_end map across runs.

    Sites have separate files so a colliding bare dataId can't return
    the wrong site's obs_end (real-camera vs BTS-simulated values can
    share a 13-digit id and *do not* share an immutable truth).
    """
    return cache_root() / EXPOSURE_TIME_CACHE_DIR / f"{siteName}.json"


def lookupCached(dataId: int, *, siteName: str) -> str | None:
    """Return a previously-cached ``obs_end`` for ``dataId`` under this
    site, or ``None``.

    The cache is best-effort: any read error (missing file, invalid
    JSON, unexpected schema) is swallowed and we return ``None`` so
    the caller falls through to a fresh ConsDB query.
    """
    p = cachedExposureTimesPath(siteName)
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


def storeCached(dataId: int, iso: str, *, siteName: str) -> None:
    """Persist ``(dataId, iso)`` in the on-disk cache for this site.

    Best-effort: any I/O error is swallowed (the cache is purely an
    optimisation). Records here never need to be invalidated — once
    a ``cdb_*.exposure.obs_end`` row exists in ConsDB, it's immutable.
    """
    p = cachedExposureTimesPath(siteName)
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
