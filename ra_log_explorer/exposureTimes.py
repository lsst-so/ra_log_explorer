"""Resolve a dataId to its ConsDB exposure record via a ConsDB endpoint.

The ConsDB exposes a SQL-style query endpoint at the site's
``consdbUrl`` (USDF / summit RSP for summit data, ``base-lsp.lsst.codes``
for the Base Test Stand sandbox). We POST a single ::

    SELECT * FROM cdb_<instrument>.exposure WHERE exposure_id = N

and get back a JSON envelope of ``{"columns": [...], "data": [[...]]}``,
which we project down to the curated :data:`EXPOSURE_RECORD_COLUMNS`
(dropping huge geometry blobs like ``s_region``). The record carries
``obs_end`` — the shutter-close moment in **TAI**, matching the Butler
``DimensionRecord.timespan.end.isot`` convention — plus the human-facing
properties the UI surfaces (filter, exposure time, image type, science
program, observation reason, group/index, pointing, seeing, …) so a
user can tell *what kind of image* a dataId is at a glance.

We ``SELECT *`` rather than an explicit column list so a column that's
absent from a given instrument's schema can't break the query (the row
is projected to whatever curated columns are present). ``obs_end`` is
the only column every instrument's ``exposure`` table is assumed to
carry, exactly as before.

ConsDB URL and bearer-token file are **per site** (see :mod:`.sites`):
the same dataId can refer to a real-camera exposure on the summit and a
simulated exposure on BTS, with different records. Every helper here
takes either a ``Site`` directly or its ``consdbUrl`` and the resolved
bearer token explicitly, so callers can never accidentally mix sources.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import cache_root
from .sites import Site

TAI_MINUS_UTC_S = 37.0

# On-disk cache: per-site JSON file mapping ``dataId (as string) ->
# exposure record (a JSON object of the columns below, including
# ``obs_end``)``. Exposure properties are immutable once a row exists,
# so caching is free of staleness concerns. The cache files live next
# to the Loki window cache so a ``rm -rf ~/.cache/ra_log_explorer``
# still resets everything.
EXPOSURE_TIME_CACHE_DIR = "exposure-times"

# Columns we keep from ``cdb_<instrument>.exposure``. Curated from the
# 51-column table to the properties worth showing a user — what kind of
# image this is and the context to make sense of its processing — while
# dropping the megabyte-scale geometry blobs (``s_region``,
# ``pgs_region``). ``obs_end`` is the shutter-close (TAI) t-zero; the
# rest drive the explore-view info box and the dataId-link tooltips.
EXPOSURE_RECORD_COLUMNS: tuple[str, ...] = (
    "exposure_id",
    "exposure_name",
    "obs_start",
    "obs_end",
    "exp_time",
    "physical_filter",
    "band",
    "img_type",
    "science_program",
    "observation_reason",
    "target_name",
    "group_id",
    "cur_index",
    "max_index",
    "s_ra",
    "s_dec",
    "sky_rotation",
    "airmass",
    "dimm_seeing",
)

# Instruments to probe in order — first match wins. LSSTCam first because
# that's where ~all current rapid-analysis traffic comes from; the rest
# are cheap to retry if the first table doesn't have the row.
INSTRUMENTS_BY_PROBE_ORDER: tuple[str, ...] = (
    "lsstcam",
    "latiss",
    "lsstcomcam",
    "lsstcomcamsim",
)

# One exposure's curated ConsDB columns: ``{column -> value}``. ``obs_end``
# is a TAI ISO string; numeric columns are int/float; others may be None.
ExposureRecord = dict[str, Any]


class ConsDbError(RuntimeError):
    """The ConsDB query failed for a reason we can't recover from."""


def readToken(path: Path) -> str:
    """Read and whitespace-strip the bearer token from ``path``.

    Raises :exc:`OSError` if the file can't be read; returns the empty
    string only if the file exists but contains only whitespace.
    """
    return path.read_text().strip()


def obsEnd(record: ExposureRecord | None) -> str | None:
    """Extract the shutter-close ISO (TAI) from a record, or ``None``.

    ``obs_end`` is the one column every code path that needs a t-zero
    reads; pulling it through this helper keeps the "is it a usable
    string" check in one place.
    """
    if not record:
        return None
    v = record.get("obs_end")
    return v if isinstance(v, str) else None


def queryExposureRecord(
    dataId: int, token: str, *, consdbUrl: str, instrument: str | None = None
) -> ExposureRecord | None:
    """Return the curated ConsDB exposure record for ``dataId``, or ``None``.

    If ``instrument`` is omitted we probe each of
    :data:`INSTRUMENTS_BY_PROBE_ORDER` in turn and return the first
    match. Raises :exc:`ConsDbError` for HTTP-level failures other
    than a missing row.
    """
    instruments = (instrument,) if instrument else INSTRUMENTS_BY_PROBE_ORDER
    for inst in instruments:
        rec = _queryOneRecord(dataId, token, inst, consdbUrl=consdbUrl)
        if rec is not None:
            return rec
    return None


def queryExposureRecordBatch(
    dataIds: Iterable[int],
    token: str,
    *,
    consdbUrl: str,
    chunkSize: int = 500,
) -> dict[int, ExposureRecord]:
    """Resolve many dataIds in one round trip per instrument.

    For each instrument in :data:`INSTRUMENTS_BY_PROBE_ORDER` we send
    a single ``SELECT * … WHERE exposure_id IN (…)`` covering whatever
    dataIds are still unresolved. Returns ``{dataId: record}`` for the
    matches found; dataIds with no row in any instrument's table
    simply don't appear in the output.

    ``chunkSize`` caps the IN-list size per query so an enormous
    night doesn't trip ConsDB's SQL-length limits. With the default
    500 a typical night-fetch (~600 dataIds) is two queries per
    instrument, and the instrument loop short-circuits as soon as
    all dataIds have been resolved.
    """
    out: dict[int, ExposureRecord] = {}
    remaining = [int(x) for x in dataIds]
    for instrument in INSTRUMENTS_BY_PROBE_ORDER:
        if not remaining:
            break
        found = _queryBatch(remaining, token, instrument, chunkSize, consdbUrl=consdbUrl)
        out.update(found)
        remaining = [d for d in remaining if d not in out]
    return out


def _recordFromRow(cols: list[str], row: list) -> ExposureRecord:
    """Project one ConsDB result row to the curated record.

    Only columns in :data:`EXPOSURE_RECORD_COLUMNS` that the table
    actually returned are kept — so an instrument missing a column just
    omits that key rather than failing.
    """
    idx = {c: i for i, c in enumerate(cols)}
    out: ExposureRecord = {}
    for c in EXPOSURE_RECORD_COLUMNS:
        i = idx.get(c)
        if i is not None and i < len(row):
            out[c] = row[i]
    return out


def _queryBatch(
    dataIds: list[int],
    token: str,
    instrument: str,
    chunkSize: int,
    *,
    consdbUrl: str,
) -> dict[int, ExposureRecord]:
    """Send one or more ``IN (…)`` queries against one instrument's table."""
    out: dict[int, ExposureRecord] = {}
    for i in range(0, len(dataIds), chunkSize):
        chunk = dataIds[i : i + chunkSize]
        idsSql = ",".join(str(d) for d in chunk)
        sql = f"SELECT * FROM cdb_{instrument}.exposure WHERE exposure_id IN ({idsSql})"
        try:
            payload = _postQuery(sql, token, consdbUrl=consdbUrl)
        except _UndefinedTableError:
            # This instrument has no schema — try the next one.
            return out
        cols = payload.get("columns") or []
        rows = payload.get("data") or []
        if "exposure_id" not in cols:
            continue
        for row in rows:
            rec = _recordFromRow(cols, row)
            try:
                eid = int(rec.get("exposure_id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            out[eid] = rec
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


def _queryOneRecord(dataId: int, token: str, instrument: str, *, consdbUrl: str) -> ExposureRecord | None:
    """POST one ``SELECT *`` against ``cdb_<instrument>.exposure`` for this id.

    Returns ``None`` when this instrument's table doesn't have the row
    (or doesn't exist) so the caller can fall through to the next
    instrument. Shares :func:`_postQuery`'s error handling: a missing
    row/table (400/404 or a 500 UndefinedTable) becomes an empty result;
    anything else raises :exc:`ConsDbError`.
    """
    sql = f"SELECT * FROM cdb_{instrument}.exposure WHERE exposure_id = {dataId}"
    try:
        payload = _postQuery(sql, token, consdbUrl=consdbUrl)
    except _UndefinedTableError:
        return None
    rows = payload.get("data") or []
    if not rows:
        return None
    cols = payload.get("columns") or []
    return _recordFromRow(cols, rows[0])


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
    """Where we persist the per-site dataId → exposure-record map across runs.

    Sites have separate files so a colliding bare dataId can't return
    the wrong site's record (real-camera vs BTS-simulated values can
    share a 13-digit id and *do not* share an immutable truth).
    """
    return cache_root() / EXPOSURE_TIME_CACHE_DIR / f"{siteName}.json"


def lookupCachedRecord(dataId: int, *, siteName: str) -> ExposureRecord | None:
    """Return a previously-cached exposure record for ``dataId`` under
    this site, or ``None``.

    The cache is best-effort: any read error (missing file, invalid
    JSON, unexpected schema) is swallowed and we return ``None`` so the
    caller falls through to a fresh ConsDB query. A legacy entry stored
    as a bare ``obs_end`` string (the pre-record cache format) is read
    back as ``{"obs_end": <str>}`` so the t-zero still resolves; the
    richer columns simply fill in on the next fetch.
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
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        return {"obs_end": val}
    return None


def storeCachedRecord(dataId: int, record: ExposureRecord, *, siteName: str) -> None:
    """Persist one ``(dataId, record)`` in the on-disk cache for this site."""
    storeCachedRecords({int(dataId): record}, siteName=siteName)


def storeCachedRecords(records: dict[int, ExposureRecord], *, siteName: str) -> None:
    """Persist many ``(dataId, record)`` pairs in one read-modify-write.

    Best-effort: any I/O error is swallowed (the cache is purely an
    optimisation). Records never need to be invalidated — once a
    ``cdb_*.exposure`` row exists in ConsDB, it's immutable. Taking the
    whole batch in one rewrite keeps a night-prefetch of hundreds of
    dataIds from re-serialising the file once per id.
    """
    if not records:
        return
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
        for eid, rec in records.items():
            existing[str(eid)] = rec
        p.write_text(json.dumps(existing, sort_keys=True, indent=2))
    except OSError:
        pass
