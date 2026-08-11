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

An exposure id is also **not unique within a site**: it is only unique
within one instrument (see :data:`INSTRUMENT_RECORD_KEY`). Every record
carries the instrument it came from, the on-disk cache keys records both
ways, and :func:`queryExposureRecordsForDayObs` returns a list rather
than an id-keyed map so a shared id can't silently drop an exposure.
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

# The instrument is part of an exposure's *identity*, not one of its
# properties. `exposure_id` is only unique within one instrument's
# `cdb_<instrument>.exposure` table: the id is `dayObs * 100000 + seqNum`
# and every instrument counts its own seqNum from 1 each night, so on any
# night where LSSTCam and LATISS both observe — which is most of them —
# ids 1..N name a different exposure per instrument. Every record this
# module hands out is therefore stamped with the table it came from,
# authoritatively (we know which table we queried), and the on-disk cache
# keys records by (instrument, id) as well as by the bare id.
INSTRUMENT_RECORD_KEY = "instrument"

# One exposure's curated ConsDB columns: ``{column -> value}``. ``obs_end``
# is a TAI ISO string; numeric columns are int/float; others may be None.
# ``instrument`` is stamped on by us rather than projected from the row.
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


def recordInstrument(record: ExposureRecord | None) -> str | None:
    """The ``cdb_<instrument>`` table a record came from, or ``None``.

    ``None`` only when the record genuinely carries no instrument — a
    manual stand-in typed without one.
    """
    if not record:
        return None
    v = record.get(INSTRUMENT_RECORD_KEY)
    return v if isinstance(v, str) and v else None


def recordExposureId(record: ExposureRecord | None) -> int | None:
    """The record's ``exposure_id`` as an int, or ``None``."""
    if not record:
        return None
    try:
        return int(record["exposure_id"])
    except (KeyError, TypeError, ValueError):
        return None


def probeOrderWinners(records: Iterable[ExposureRecord]) -> dict[int, ExposureRecord]:
    """Collapse records to one per *bare* exposure id, probe order deciding.

    The bare-id views of the world — ``GET /api/exposure-time/<id>`` with
    no ``instrument``, and the bare keys in the on-disk cache — answer
    with whichever instrument :data:`INSTRUMENTS_BY_PROBE_ORDER` reaches
    first. Anything that writes into those views has to agree with that
    rule, or a night where two instruments share ids would answer
    differently depending on who wrote last.
    """
    rank = {inst: i for i, inst in enumerate(INSTRUMENTS_BY_PROBE_ORDER)}
    unranked = len(rank)
    best: dict[int, tuple[int, ExposureRecord]] = {}
    for rec in records:
        eid = recordExposureId(rec)
        if eid is None:
            continue
        r = rank.get(recordInstrument(rec) or "", unranked)
        current = best.get(eid)
        if current is None or r < current[0]:
            best[eid] = (r, rec)
    return {eid: rec for eid, (_, rec) in best.items()}


# A hand-entered shutter close: the user typed a timestamp because ConsDB
# was down/unreachable or had no row for the dataId. We persist it to the
# same per-site cache as a real record (so the explore view can be
# reopened/refreshed without re-typing), but tag it so the live lookup
# still prefers a real ConsDB answer once one becomes available — a manual
# value is a stand-in, not the immutable truth a ConsDB row is.
MANUAL_RECORD_KEY = "_manual"


def manualRecord(obsEndTai: str, instrument: str | None = None) -> ExposureRecord:
    """Build a minimal manual exposure record carrying just ``obs_end``.

    Stamped with ``instrument`` when the caller knows it, so the
    instrument-pinned lookups used everywhere else can find the
    stand-in — an unstamped manual record only answers bare lookups.
    """
    record: ExposureRecord = {"obs_end": obsEndTai, MANUAL_RECORD_KEY: True}
    if instrument:
        record[INSTRUMENT_RECORD_KEY] = instrument
    return record


def isManual(record: ExposureRecord | None) -> bool:
    """True if ``record`` is a hand-entered stand-in (see :func:`manualRecord`)."""
    return bool(record and record.get(MANUAL_RECORD_KEY))


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
    instrument: str | None = None,
) -> dict[int, ExposureRecord]:
    """Resolve many dataIds in one round trip per instrument.

    With ``instrument`` pinned, only that instrument's table is asked —
    the caller knows which instrument's exposures these ids name (a
    range of one instrument's exposures, a night of AOS work), and a
    probe-order answer could silently hand back the *other* instrument's
    rows for colliding ids, anchoring every downstream Δshutter offset
    to the wrong shutter. Unpinned, each instrument in
    :data:`INSTRUMENTS_BY_PROBE_ORDER` is asked in turn with a single
    ``SELECT * … WHERE exposure_id IN (…)`` covering whatever dataIds
    are still unresolved. Returns ``{dataId: record}`` for the matches
    found; dataIds with no row simply don't appear in the output.

    ``chunkSize`` caps the IN-list size per query so an enormous
    night doesn't trip ConsDB's SQL-length limits. With the default
    500 a typical night-fetch (~600 dataIds) is two queries per
    instrument, and the instrument loop short-circuits as soon as
    all dataIds have been resolved.
    """
    out: dict[int, ExposureRecord] = {}
    remaining = [int(x) for x in dataIds]
    instruments = (instrument,) if instrument else INSTRUMENTS_BY_PROBE_ORDER
    for inst in instruments:
        if not remaining:
            break
        found = _queryBatch(remaining, token, inst, chunkSize, consdbUrl=consdbUrl)
        out.update(found)
        remaining = [d for d in remaining if d not in out]
    return out


def queryExposureRecordsForDayObs(
    dayObs: int,
    token: str,
    *,
    consdbUrl: str,
) -> list[ExposureRecord]:
    """Return every instrument's exposure records for one dayObs.

    The 13-digit dataId embeds its dayObs (``YYYYMMDDSSSSS``), so one
    range predicate per instrument table covers the whole night without
    needing a ``day_obs`` column in every schema. Unlike
    :func:`queryExposureRecordBatch` this does **not** stop at the first
    instrument with rows — LSSTCam and LATISS routinely observe on the
    same night, and the caller wants both.

    Returns a **list**, not a ``{id: record}`` map, precisely because the
    two instruments' ids collide (see :data:`INSTRUMENT_RECORD_KEY`): a
    map would silently drop one instrument's exposure for every shared
    id. Callers that need a bare-id view collapse it themselves with
    :func:`probeOrderWinners`. Ordered by (probe order, exposure id).
    """
    lo = dayObs * 100000
    hi = lo + 99999
    out: list[ExposureRecord] = []
    for instrument in INSTRUMENTS_BY_PROBE_ORDER:
        sql = f"SELECT * FROM cdb_{instrument}.exposure WHERE exposure_id BETWEEN {lo} AND {hi}"
        try:
            payload = _postQuery(sql, token, consdbUrl=consdbUrl)
        except _UndefinedTableError:
            continue
        cols = payload.get("columns") or []
        rows = payload.get("data") or []
        if "exposure_id" not in cols:
            continue
        found = [_recordFromRow(cols, row, instrument) for row in rows]
        keyed = [(eid, rec) for eid, rec in ((recordExposureId(r), r) for r in found) if eid is not None]
        out.extend(rec for _, rec in sorted(keyed, key=lambda pair: pair[0]))
    return out


def _recordFromRow(cols: list[str], row: list, instrument: str) -> ExposureRecord:
    """Project one ConsDB result row to the curated record.

    Only columns in :data:`EXPOSURE_RECORD_COLUMNS` that the table
    actually returned are kept — so an instrument missing a column just
    omits that key rather than failing. ``instrument`` is stamped on
    afterwards from the table we queried rather than read out of the row:
    we know which table this came from, and not every schema carries the
    column.
    """
    idx = {c: i for i, c in enumerate(cols)}
    out: ExposureRecord = {}
    for c in EXPOSURE_RECORD_COLUMNS:
        i = idx.get(c)
        if i is not None and i < len(row):
            out[c] = row[i]
    out[INSTRUMENT_RECORD_KEY] = instrument
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
            rec = _recordFromRow(cols, row, instrument)
            eid = recordExposureId(rec)
            if eid is None:
                continue
            out[eid] = rec
    return out


class _UndefinedTableError(Exception):
    """Internal: ConsDB 500'd with a SQL UndefinedTable body. Caller
    should try the next instrument rather than surface this."""


def _postQuery(sql: str, token: str, *, consdbUrl: str) -> dict:
    """POST one SQL query, return the parsed JSON payload."""
    body = json.dumps({"query": sql}).encode("utf-8")
    headers = {"accept": "application/json", "Content-Type": "application/json"}
    # An empty token means the endpoint needs no auth (an in-cluster ConsDB
    # Service, reached without going through Gafaelfawr). Sending
    # ``Bearer`` with nothing after it would be rejected outright.
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(consdbUrl, data=body, headers=headers, method="POST")
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
    return _recordFromRow(cols, rows[0], instrument)


# ----- site-aware convenience wrappers -------------------------------------


def loadTokenForSite(site: Site) -> str:
    """Read and return the bearer token for ``site.consdbTokenFile``.

    Returns the empty string — meaning "send no Authorization header" —
    for a site that declares no token file at all. Otherwise raises
    :exc:`OSError` if the file can't be read, and returns the empty
    string if the file exists but is whitespace-only. Callers check
    both conditions explicitly so they can report a clear UI message
    ("token file missing" vs "token file empty") to the user.
    """
    if site.consdbTokenFile is None:
        return ""
    return readToken(site.consdbTokenFile)


# ----- on-disk cache --------------------------------------------------------


def cachedExposureTimesPath(siteName: str) -> Path:
    """Where we persist the per-site dataId → exposure-record map across runs.

    Sites have separate files so a colliding bare dataId can't return
    the wrong site's record (real-camera vs BTS-simulated values can
    share a 13-digit id and *do not* share an immutable truth).
    """
    return cache_root() / EXPOSURE_TIME_CACHE_DIR / f"{siteName}.json"


def cacheKey(dataId: int, instrument: str | None = None) -> str:
    """The per-site cache key for a dataId, optionally scoped to an instrument.

    Bare ``"<id>"`` is the probe-order view — what a lookup that doesn't
    know (or care about) the instrument resolves to.
    ``"<instrument>:<id>"`` is the unambiguous one, and is the only key
    an instrument-scoped lookup will accept: falling back to the bare
    key there could hand back a different instrument's exposure with the
    same id.
    """
    return f"{instrument}:{dataId}" if instrument else str(dataId)


def lookupCachedRecord(dataId: int, *, siteName: str, instrument: str | None = None) -> ExposureRecord | None:
    """Return a previously-cached exposure record for ``dataId`` under
    this site, or ``None``.

    With ``instrument`` set, only that instrument's entry can match — a
    bare-id fallback would defeat the point, since the bare key holds the
    probe-order winner, which for a colliding id is a *different*
    exposure. Without it, the bare (probe-order) entry is returned.

    The cache is best-effort: any read error (missing file, invalid
    JSON, unexpected schema) is swallowed and we return ``None`` so the
    caller falls through to a fresh ConsDB query. There is exactly one
    on-disk shape — a record object; anything else (including entries an
    older build wrote) is a miss, not something to interpret. Caches are
    a convenience, and the schema flush is the upgrade path.
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
    val = d.get(cacheKey(dataId, instrument))
    return val if isinstance(val, dict) else None


def storeCachedRecord(dataId: int, record: ExposureRecord, *, siteName: str, bareKey: bool = True) -> None:
    """Persist one ``(dataId, record)`` in the on-disk cache for this site."""
    storeCachedRecords({int(dataId): record}, siteName=siteName, bareKey=bareKey)


def storeCachedRecords(records: dict[int, ExposureRecord], *, siteName: str, bareKey: bool = True) -> None:
    """Persist ``{dataId: record}`` — resolved the bare-id way — in one write.

    For callers that resolved their ids the way a bare-id lookup does
    (``queryExposureRecord`` / ``queryExposureRecordBatch``, both of which
    stop at the first instrument that has the row) plus manual
    stand-ins. Each record lands under the bare key *and*, when it knows
    its instrument, under the instrument-scoped one.

    ``bareKey=False`` for a caller that pinned a non-first instrument:
    its answer is right for that instrument but is *not* what a bare-id
    lookup resolves to, and writing it there would make the same dataId
    answer differently depending on who asked last.
    """
    entries: dict[str, ExposureRecord] = {}
    for eid, rec in records.items():
        instrument = recordInstrument(rec)
        if instrument:
            entries[cacheKey(int(eid), instrument)] = rec
        if bareKey:
            entries[cacheKey(int(eid))] = rec
    _mergeIntoCache(entries, siteName=siteName)


def isProbeOrderFirst(instrument: str | None) -> bool:
    """True if resolving against ``instrument`` also answers the bare id.

    A bare-id lookup probes :data:`INSTRUMENTS_BY_PROBE_ORDER` and stops
    at the first table with the row — so a hit against the *first*
    instrument is, by construction, also the bare-id answer, and may be
    cached as one. ``None`` means the caller didn't pin an instrument at
    all, which is the bare-id path itself.
    """
    return instrument is None or instrument == INSTRUMENTS_BY_PROBE_ORDER[0]


def storeCachedRecordList(records: Iterable[ExposureRecord], *, siteName: str) -> None:
    """Persist a batch of records whose exposure ids may collide.

    Every record is stored under its own ``(instrument, id)`` key, so no
    exposure is lost to a shared id; the *probe-order winner* for each id
    is additionally stored under the bare key, so the bare-id view agrees
    with what :func:`queryExposureRecord` would have answered. Used by
    the live poller, which pulls a whole night from every instrument at
    once (see :func:`queryExposureRecordsForDayObs`).
    """
    records = list(records)
    entries: dict[str, ExposureRecord] = {}
    for rec in records:
        eid = recordExposureId(rec)
        instrument = recordInstrument(rec)
        if eid is None or not instrument:
            continue
        entries[cacheKey(eid, instrument)] = rec
    for eid, rec in probeOrderWinners(records).items():
        entries[cacheKey(eid)] = rec
    _mergeIntoCache(entries, siteName=siteName)


def _mergeIntoCache(entries: dict[str, ExposureRecord], *, siteName: str) -> None:
    """Merge pre-keyed entries into the per-site cache file.

    Best-effort: any I/O error is swallowed (the cache is purely an
    optimisation). Records never need to be invalidated — once a
    ``cdb_*.exposure`` row exists in ConsDB, it's immutable. They are
    plainly overwritten, though, which is how a real record supersedes a
    ``_manual`` stand-in (see :func:`manualRecord`). Taking the whole
    batch in one rewrite keeps a night-prefetch of hundreds of dataIds
    from re-serialising the file once per id.
    """
    if not entries:
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
        existing.update(entries)
        p.write_text(json.dumps(existing, sort_keys=True, indent=2))
    except OSError:
        pass
