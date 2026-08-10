"""Tests for `ra_log_explorer.exposureTimes`."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from ra_log_explorer import exposureTimes
from ra_log_explorer import sites as sitesModule

# A throwaway URL used for every call below. The point of these tests is the
# helper's behaviour around the response shape, not its URL routing — the URL
# is parametrised on every call so callers can never accidentally share state
# across sites.
URL = "https://consdb-under-test.example/consdb/query"

# A realistic-ish ConsDB row, as `(columns, row)`. We always SELECT * now and
# project to EXPOSURE_RECORD_COLUMNS, so responses carry more than obs_end —
# including a couple of columns (``controller``) we deliberately drop.
_FULL_COLS = ["exposure_id", "obs_end", "physical_filter", "img_type", "exp_time", "controller"]


def _fullRow(eid: int, iso: str) -> list:
    return [eid, iso, "z_20", "science", 30.0, "O"]


def _stubResponse(payload: dict) -> io.BytesIO:
    """Return a file-like that mimics what `urlopen` yields."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


# ----- readToken -----------------------------------------------------------


def test_readToken_strips_whitespace(tmp_path: Path) -> None:
    p = tmp_path / "tok"
    p.write_text("  abc-def\n")
    assert exposureTimes.readToken(p) == "abc-def"


def test_readToken_returns_empty_for_whitespace_only(tmp_path: Path) -> None:
    p = tmp_path / "tok"
    p.write_text("   \n  ")
    assert exposureTimes.readToken(p) == ""


def test_readToken_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        exposureTimes.readToken(tmp_path / "nope")


# ----- obsEnd ---------------------------------------------------------------


def test_obsEnd_extracts_string_or_None() -> None:
    assert exposureTimes.obsEnd({"obs_end": "2026-05-20T08:46:16.267000"}) == "2026-05-20T08:46:16.267000"
    assert exposureTimes.obsEnd({"img_type": "science"}) is None  # no obs_end key
    assert exposureTimes.obsEnd({"obs_end": 12345}) is None  # non-string
    assert exposureTimes.obsEnd(None) is None


# ----- manualRecord / isManual ----------------------------------------------


def test_manualRecord_carries_obsEnd_and_is_tagged() -> None:
    rec = exposureTimes.manualRecord("2026-06-24T14:38:41.380663")
    assert exposureTimes.obsEnd(rec) == "2026-06-24T14:38:41.380663"
    assert exposureTimes.isManual(rec) is True


def test_isManual_false_for_consdb_record_and_none() -> None:
    # A real ConsDB record (no _manual tag) and a missing record are both
    # "not manual" — the distinction is what keeps a hand-entered stand-in
    # from shadowing immutable ConsDB truth on later lookups.
    assert exposureTimes.isManual({"obs_end": "2026-06-24T14:38:41.380663"}) is False
    assert exposureTimes.isManual(None) is False


def test_manualRecord_roundtrips_through_the_cache(tmpCacheRoot: Path) -> None:
    exposureTimes.storeCachedRecord(
        2026051900722, exposureTimes.manualRecord("2026-06-24T14:38:41.380663"), siteName="summit"
    )
    back = exposureTimes.lookupCachedRecord(2026051900722, siteName="summit")
    assert exposureTimes.isManual(back) is True
    assert exposureTimes.obsEnd(back) == "2026-06-24T14:38:41.380663"


# ----- queryExposureRecord --------------------------------------------------


def test_queryExposureRecord_returns_projected_record_on_first_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append((req.get_full_url(), req.data, dict(req.header_items())))
        return _stubResponse(
            {"columns": _FULL_COLS, "data": [_fullRow(2026051900722, "2026-05-20T08:46:16.267000")]}
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    rec = exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL)
    assert rec is not None
    # Curated columns are kept...
    assert rec["obs_end"] == "2026-05-20T08:46:16.267000"
    assert rec["physical_filter"] == "z_20"
    assert rec["img_type"] == "science"
    assert rec["exp_time"] == 30.0
    assert exposureTimes.obsEnd(rec) == "2026-05-20T08:46:16.267000"
    # ...and a non-curated column is dropped from the stored record.
    assert "controller" not in rec
    # Only one HTTP call needed: the lsstcam table matched first.
    assert len(seen) == 1
    url, body, headers = seen[0]
    assert url == URL
    parsedBody = json.loads(body.decode("utf-8"))
    assert "cdb_lsstcam.exposure" in parsedBody["query"]
    assert "2026051900722" in parsedBody["query"]
    # Token must travel as a Bearer auth header — never in the URL or body.
    assert headers["Authorization"] == "Bearer TOKEN"
    assert "TOKEN" not in url
    assert "TOKEN" not in body.decode("utf-8")


def test_queryExposureRecord_sends_select_star(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wire query is part of the contract with ConsDB; SELECT * is
    what makes the projection robust to per-instrument column gaps. Pin
    its shape so a refactor can't silently change it."""
    seen: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append(json.loads(req.data.decode("utf-8"))["query"])
        return _stubResponse({"columns": _FULL_COLS, "data": [_fullRow(2026051900722, "x")]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL, instrument="lsstcam")
    assert seen == ["SELECT * FROM cdb_lsstcam.exposure WHERE exposure_id = 2026051900722"]


def test_queryExposureRecord_uses_the_caller_supplied_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single process can talk to multiple ConsDB endpoints in one run
    — pin that the URL kwarg actually reaches urlopen so a regression
    can't silently route everything back to a hard-coded default."""
    seen: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append(req.get_full_url())
        return _stubResponse({"columns": _FULL_COLS, "data": [_fullRow(1, "x")]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryExposureRecord(1, "TOK", consdbUrl="https://summit.example/q", instrument="lsstcam")
    exposureTimes.queryExposureRecord(1, "TOK", consdbUrl="https://bts.example/q", instrument="lsstcam")
    assert seen == ["https://summit.example/q", "https://bts.example/q"]


def test_queryExposureRecord_falls_through_instruments_until_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    payloads: Any = iter(
        [
            {"columns": _FULL_COLS, "data": []},  # lsstcam — empty
            {"columns": _FULL_COLS, "data": [_fullRow(2026052000100, "2026-05-20T09:00:00.000")]},  # latiss
        ]
    )

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append(json.loads(req.data.decode("utf-8"))["query"])
        return _stubResponse(next(payloads))

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    rec = exposureTimes.queryExposureRecord(2026052000100, "TOKEN", consdbUrl=URL)
    assert exposureTimes.obsEnd(rec) == "2026-05-20T09:00:00.000"
    assert "cdb_lsstcam.exposure" in seen[0]
    assert "cdb_latiss.exposure" in seen[1]


def test_queryExposureRecord_returns_None_when_all_instruments_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse({"columns": _FULL_COLS, "data": []})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL) is None


def test_queryExposureRecord_uses_only_the_given_instrument_when_specified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append(json.loads(req.data.decode("utf-8"))["query"])
        return _stubResponse({"columns": _FULL_COLS, "data": [_fullRow(2026051900722, "x")]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL, instrument="latiss")
    assert len(seen) == 1
    assert "cdb_latiss.exposure" in seen[0]


def test_queryExposureRecord_returns_None_for_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError(req.get_full_url(), 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert (
        exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL, instrument="lsstcam") is None
    )


def test_queryExposureRecord_raises_ConsDbError_for_other_HTTP_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError(req.get_full_url(), 500, "server error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    with pytest.raises(exposureTimes.ConsDbError):
        exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL, instrument="lsstcam")


def test_queryExposureRecord_treats_500_UndefinedTable_as_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConsDB returns HTTP 500 with a psycopg2 ``UndefinedTable`` body
    when an instrument's schema doesn't exist. We must skip to the next
    instrument rather than blowing up the whole lookup."""
    seen: list[str] = []
    undefBody = b'{"message":"(psycopg2.errors.UndefinedTable) relation does not exist"}'
    hitBody = json.dumps({"columns": _FULL_COLS, "data": [_fullRow(2026051900722, "x")]}).encode("utf-8")
    payloads = iter([("undefined", undefBody), ("hit", hitBody)])

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        kind, body = next(payloads)
        seen.append(json.loads(req.data.decode("utf-8"))["query"])
        if kind == "undefined":
            raise HTTPError(
                req.get_full_url(),
                500,
                "Internal Server Error",
                {},  # type: ignore[arg-type]
                io.BytesIO(body),
            )
        return io.BytesIO(body)

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    rec = exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL)
    assert exposureTimes.obsEnd(rec) == "x"
    assert len(seen) == 2  # lsstcam 500 -> latiss hit


def test_queryExposureRecord_raises_for_500_that_is_not_UndefinedTable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic ConsDB 500s — DB down, transient outage, etc. — bubble
    up rather than being silently swallowed. Otherwise a real outage
    looks identical to 'dataId not found anywhere'."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError(
            req.get_full_url(),
            500,
            "Internal Server Error",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message":"connection refused"}'),
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    with pytest.raises(exposureTimes.ConsDbError):
        exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL, instrument="lsstcam")


def test_queryExposureRecord_returns_record_even_without_obs_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row that's missing the obs_end column still yields a record (the
    row exists); it's ``obsEnd`` that returns None so the caller decides
    what to do. The row isn't silently dropped as "no such exposure"."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse({"columns": ["exposure_id", "img_type"], "data": [[2026051900722, "science"]]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    rec = exposureTimes.queryExposureRecord(2026051900722, "TOKEN", consdbUrl=URL, instrument="lsstcam")
    assert rec == {"exposure_id": 2026051900722, "img_type": "science", "instrument": "lsstcam"}
    assert exposureTimes.obsEnd(rec) is None


# ----- queryExposureRecordBatch ---------------------------------------------


def test_queryExposureRecordBatch_returns_resolved_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the batch helper is one round trip per
    instrument, not one per dataId."""
    callCount = 0

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        nonlocal callCount
        callCount += 1
        sentSql = json.loads(req.data.decode("utf-8"))["query"]
        assert "SELECT * FROM" in sentSql and "IN (" in sentSql
        return _stubResponse(
            {
                "columns": _FULL_COLS,
                "data": [
                    _fullRow(2026051900722, "2026-05-20T08:46:16.267000"),
                    _fullRow(2026051900723, "2026-05-20T08:47:02.724000"),
                ],
            }
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryExposureRecordBatch([2026051900722, 2026051900723], "TOKEN", consdbUrl=URL)
    assert set(out) == {2026051900722, 2026051900723}
    assert exposureTimes.obsEnd(out[2026051900722]) == "2026-05-20T08:46:16.267000"
    assert out[2026051900723]["physical_filter"] == "z_20"
    # One call: lsstcam matched everything, so we don't even try the
    # other instruments.
    assert callCount == 1


def test_queryExposureRecordBatch_pinned_instrument_never_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``instrument`` pinned, only that instrument's table is asked —
    even for ids it has no row for. A probe-order fallthrough would hand
    back the *other* instrument's exposure for a colliding id, anchoring
    every downstream Δshutter offset to the wrong shutter."""
    tablesQueried: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        sentSql = json.loads(req.data.decode("utf-8"))["query"]
        tablesQueried.append(sentSql.split("FROM ")[1].split(".")[0])
        # latiss knows one of the two ids; the other resolves nowhere.
        return _stubResponse(
            {
                "columns": _FULL_COLS,
                "data": [_fullRow(2026051900722, "2026-05-20T08:46:16.267000")],
            }
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryExposureRecordBatch(
        [2026051900722, 2026051900999], "TOKEN", consdbUrl=URL, instrument="latiss"
    )
    assert set(out) == {2026051900722}
    assert tablesQueried == ["cdb_latiss"]  # nothing else was ever asked


def test_queryExposureRecordBatch_falls_through_to_other_instruments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If lsstcam returns only some rows, the helper queries the next
    instrument for the still-missing ids."""
    seenQueries: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        sql = json.loads(req.data.decode("utf-8"))["query"]
        seenQueries.append(sql)
        if "cdb_lsstcam." in sql:
            return _stubResponse(
                {"columns": _FULL_COLS, "data": [_fullRow(2026051900722, "2026-05-20T08:46:16.267000")]}
            )
        return _stubResponse(
            {"columns": _FULL_COLS, "data": [_fullRow(2026052000100, "2026-05-20T09:00:00.000000")]}
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryExposureRecordBatch([2026051900722, 2026052000100], "TOKEN", consdbUrl=URL)
    assert set(out) == {2026051900722, 2026052000100}
    assert "cdb_lsstcam." in seenQueries[0]
    assert "cdb_latiss." in seenQueries[1]


def test_queryExposureRecordBatch_chunks_oversized_in_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge IN-list would blow ConsDB's SQL-length limit. The helper
    chunks itself so this can't happen."""
    seenQueries: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seenQueries.append(json.loads(req.data.decode("utf-8"))["query"])
        return _stubResponse({"columns": _FULL_COLS, "data": []})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    ids = list(range(2026051900000, 2026051901500))  # 1500 dataIds
    exposureTimes.queryExposureRecordBatch(ids, "TOKEN", consdbUrl=URL, chunkSize=500)
    # 1500 / 500 = 3 chunks per instrument; loop short-circuits since
    # we never resolve anything, so all 4 instruments are tried.
    assert len(seenQueries) == 3 * 4


def test_queryExposureRecordBatch_falls_through_on_UndefinedTable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 UndefinedTable from one instrument shouldn't fail the whole
    batch — the helper should silently fall through to the next."""
    calls: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        sql = json.loads(req.data.decode("utf-8"))["query"]
        calls.append(sql)
        if "cdb_lsstcam." in sql:
            raise HTTPError(
                "https://x",
                500,
                "Server Error",
                {},  # type: ignore[arg-type]
                io.BytesIO(b'{"detail":"UndefinedTable: table not found"}'),
            )
        return _stubResponse(
            {"columns": _FULL_COLS, "data": [_fullRow(2026051900722, "2026-05-20T08:46:16.267000")]}
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryExposureRecordBatch([2026051900722], "TOKEN", consdbUrl=URL)
    assert set(out) == {2026051900722}
    assert "cdb_lsstcam." in calls[0]
    assert "cdb_latiss." in calls[1]


def test_queryExposureRecordBatch_skips_rows_without_exposure_id_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the batch response has no exposure_id column we can't key the
    rows, so the chunk yields nothing rather than mis-indexing."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse({"columns": ["something_else"], "data": [["x"]]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryExposureRecordBatch([2026051900722], "TOKEN", consdbUrl=URL)
    assert out == {}


def test_queryExposureRecordBatch_skips_rows_with_unexpected_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with an unparseable exposure_id (e.g. ``None``) shouldn't
    break the batch — that row is just skipped."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse(
            {
                "columns": _FULL_COLS,
                "data": [
                    _fullRow(2026051900722, "2026-05-20T08:46:16.267000"),
                    [None, "2026-05-20T08:46:16.267000", "z_20", "science", 30.0, "O"],
                ],
            }
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryExposureRecordBatch([2026051900722], "TOKEN", consdbUrl=URL)
    # The valid row landed; the None-id row got dropped.
    assert set(out) == {2026051900722}


def test_queryExposureRecordBatch_empty_input_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty input list must short-circuit without any HTTP call —
    the prefetch path calls this with the set of unresolved ids, which
    may legitimately be empty."""

    def fakeUrlopen(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("urlopen called for empty batch")

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryExposureRecordBatch([], "TOKEN", consdbUrl=URL)
    assert out == {}


# ----- _postQuery -----------------------------------------------------------


def test_postQuery_treats_400_as_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 from ConsDB (typically "no such row" / "invalid query")
    should surface as an empty payload, not as a ConsDbError that aborts
    the whole batch."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError("https://x", 400, "Bad Request", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes._postQuery("SELECT 1", "TOKEN", consdbUrl=URL)
    assert out == {"columns": [], "data": []}


def test_postQuery_raises_ConsDbError_for_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTP 5xx that isn't an UndefinedTable should bubble out as a
    typed error so the caller can surface it to the user."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError("https://x", 503, "Unavailable", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    with pytest.raises(exposureTimes.ConsDbError, match="503"):
        exposureTimes._postQuery("SELECT 1", "TOKEN", consdbUrl=URL)


# ----- on-disk cache --------------------------------------------------------

_REC = {"obs_end": "2026-05-20T08:46:16.267000", "physical_filter": "z_20", "img_type": "science"}


def test_lookupCachedRecord_returns_None_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    assert exposureTimes.lookupCachedRecord(2026051900722, siteName="summit") is None


def test_storeCachedRecord_then_lookup_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCachedRecord(2026051900722, _REC, siteName="summit")
    got = exposureTimes.lookupCachedRecord(2026051900722, siteName="summit")
    assert got == _REC
    assert exposureTimes.obsEnd(got) == "2026-05-20T08:46:16.267000"


def test_lookupCachedRecord_reads_legacy_obs_end_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cache written by the pre-record format stored a bare obs_end
    string per dataId. It must still resolve (wrapped as a 1-field
    record) so an existing cache keeps working across the upgrade."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = exposureTimes.cachedExposureTimesPath("summit")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"2026051900722": "2026-05-20T08:46:16.267000"}))
    got = exposureTimes.lookupCachedRecord(2026051900722, siteName="summit")
    assert got == {"obs_end": "2026-05-20T08:46:16.267000"}
    assert exposureTimes.obsEnd(got) == "2026-05-20T08:46:16.267000"


def test_storeCachedRecords_batches_one_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCachedRecords(
        {2026051900722: _REC, 2026051900723: {"obs_end": "iso-b"}}, siteName="summit"
    )
    assert exposureTimes.lookupCachedRecord(2026051900722, siteName="summit") == _REC
    assert exposureTimes.obsEnd(exposureTimes.lookupCachedRecord(2026051900723, siteName="summit")) == "iso-b"


def test_storeCachedRecords_empty_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCachedRecords({}, siteName="summit")
    assert not exposureTimes.cachedExposureTimesPath("summit").exists()


def test_storeCachedRecord_appends_without_clobbering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCachedRecord(2026051900722, {"obs_end": "iso-a"}, siteName="summit")
    exposureTimes.storeCachedRecord(2026051900723, {"obs_end": "iso-b"}, siteName="summit")
    assert exposureTimes.obsEnd(exposureTimes.lookupCachedRecord(2026051900722, siteName="summit")) == "iso-a"
    assert exposureTimes.obsEnd(exposureTimes.lookupCachedRecord(2026051900723, siteName="summit")) == "iso-b"


def test_storeCachedRecord_isolates_sites_for_the_same_dataId(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two sites can record different records for the same bare dataId
    (real-camera vs BTS-simulated). The per-site cache files keep them
    from crosstalking — a miss for one site must not see the other's."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCachedRecord(2026060200001, {"obs_end": "2026-06-03T00:42:43.632000"}, siteName="bts")
    assert exposureTimes.lookupCachedRecord(2026060200001, siteName="summit") is None
    assert (
        exposureTimes.obsEnd(exposureTimes.lookupCachedRecord(2026060200001, siteName="bts"))
        == "2026-06-03T00:42:43.632000"
    )
    base = tmp_path / "exposure-times"
    assert (base / "bts.json").exists()
    assert not (base / "summit.json").exists()


def test_lookupCachedRecord_tolerates_corrupt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = exposureTimes.cachedExposureTimesPath("summit")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not-json{")
    assert exposureTimes.lookupCachedRecord(2026051900722, siteName="summit") is None


def test_lookupCachedRecord_tolerates_unexpected_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = exposureTimes.cachedExposureTimesPath("summit")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(["not", "a", "dict"]))
    assert exposureTimes.lookupCachedRecord(2026051900722, siteName="summit") is None


def test_lookupCachedRecord_returns_None_for_unexpected_value_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt cache that maps the right key to neither a dict nor a
    string (e.g. an int) must surface as a miss, not crash the caller."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = exposureTimes.cachedExposureTimesPath("summit")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"2026051900722": 12345}))
    assert exposureTimes.lookupCachedRecord(2026051900722, siteName="summit") is None


def test_storeCachedRecords_recovers_from_corrupt_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the existing cache file is corrupt, storing should silently
    overwrite it with a fresh map rather than refusing to record."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    cachePath = exposureTimes.cachedExposureTimesPath("summit")
    cachePath.parent.mkdir(parents=True, exist_ok=True)
    cachePath.write_text("this is not json")
    exposureTimes.storeCachedRecord(
        2026051900722, {"obs_end": "2026-05-20T08:46:16.267000"}, siteName="summit"
    )
    data = json.loads(cachePath.read_text())
    assert data == {"2026051900722": {"obs_end": "2026-05-20T08:46:16.267000"}}


def test_postQuery_omits_auth_header_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-cluster ConsDB takes no token. Sending a bare ``Bearer`` with
    nothing after it would be rejected, so the header must be absent
    entirely rather than empty."""
    seen: list[dict[str, str]] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append(dict(req.header_items()))
        return _stubResponse(
            {"columns": _FULL_COLS, "data": [_fullRow(2026051900722, "2026-05-20T08:46:16.267000")]}
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    rec = exposureTimes.queryExposureRecord(2026051900722, "", consdbUrl=URL)
    assert rec is not None
    # urllib title-cases header names, so check case-insensitively.
    assert not [k for k in seen[0] if k.lower() == "authorization"]


def test_loadTokenForSite_returns_empty_for_a_token_less_site(tmp_path: Path) -> None:
    site = sitesModule.Site(
        name="incluster",
        cluster="manke",
        namespace="ns",
        lokiAddr="https://l",
        consdbUrl="http://consdb-pq.consdb:8080/consdb/query",
        consdbTokenFile=None,
    )
    assert exposureTimes.loadTokenForSite(site) == ""


# ----- instrument identity --------------------------------------------------


def _payload(rows: list[list[Any]]) -> dict:
    return {"columns": ["exposure_id", "obs_end"], "data": rows}


def test_records_are_stamped_with_the_table_they_came_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """We know which cdb_<instrument> table we queried, so the record says
    so — rather than depending on every schema carrying the column."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse(_payload([[2026071100001, "2026-07-11T12:00:37"]]))

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    rec = exposureTimes.queryExposureRecord(2026071100001, "T", consdbUrl=URL, instrument="latiss")
    assert exposureTimes.recordInstrument(rec) == "latiss"


def test_queryExposureRecordsForDayObs_keeps_both_instruments(monkeypatch: pytest.MonkeyPatch) -> None:
    """LSSTCam and LATISS number from 1 each night, so on any night both
    observe the same id names a different exposure on each. Returning a
    {id: record} map would silently drop one of every colliding pair."""

    seen: list[str] = []

    def fakePost(sql: str, token: str, *, consdbUrl: str) -> dict:
        seen.append(sql)
        if "cdb_lsstcam" in sql:
            return _payload([[2026071100002, "cam-2"], [2026071100001, "cam-1"]])
        if "cdb_latiss" in sql:
            return _payload([[2026071100001, "latiss-1"]])
        raise exposureTimes._UndefinedTableError()

    monkeypatch.setattr(exposureTimes, "_postQuery", fakePost)
    recs = exposureTimes.queryExposureRecordsForDayObs(20260711, "T", consdbUrl=URL)
    assert [(r["exposure_id"], r["instrument"], r["obs_end"]) for r in recs] == [
        (2026071100001, "lsstcam", "cam-1"),
        (2026071100002, "lsstcam", "cam-2"),
        (2026071100001, "latiss", "latiss-1"),
    ]
    # Every instrument is asked, not just the first with rows, and the id
    # range is that dayObs's own 5-digit sequence space.
    assert len(seen) == len(exposureTimes.INSTRUMENTS_BY_PROBE_ORDER)
    assert "BETWEEN 2026071100000 AND 2026071199999" in seen[0]


def test_probeOrderWinners_resolves_a_shared_id_the_bare_lookup_way() -> None:
    """The bare-id view has to agree with queryExposureRecord, which stops
    at the first instrument with the row — otherwise the same dataId
    resolves differently depending on which code path answered."""
    cam = {"exposure_id": 1, "instrument": "lsstcam"}
    latiss = {"exposure_id": 1, "instrument": "latiss"}
    assert exposureTimes.probeOrderWinners([latiss, cam]) == {1: cam}
    assert exposureTimes.probeOrderWinners([cam, latiss]) == {1: cam}
    # A record with no instrument (legacy / manual) never beats a real one.
    assert exposureTimes.probeOrderWinners([{"exposure_id": 1}, latiss]) == {1: latiss}


def test_storeCachedRecordList_keys_by_instrument_and_bare_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    cam = {"exposure_id": 2026071100001, "obs_end": "cam", "instrument": "lsstcam"}
    latiss = {"exposure_id": 2026071100001, "obs_end": "latiss", "instrument": "latiss"}
    exposureTimes.storeCachedRecordList([latiss, cam], siteName="summit")

    byInstrument = exposureTimes.lookupCachedRecord(2026071100001, siteName="summit", instrument="latiss")
    assert byInstrument is not None and byInstrument["obs_end"] == "latiss"
    bare = exposureTimes.lookupCachedRecord(2026071100001, siteName="summit")
    assert bare is not None and bare["obs_end"] == "cam"  # probe order wins


def test_instrument_lookup_never_falls_back_to_the_bare_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Falling back would hand out a *different exposure* with the same id."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCachedRecord(
        2026071100001,
        {"exposure_id": 2026071100001, "obs_end": "cam", "instrument": "lsstcam"},
        siteName="summit",
    )
    assert exposureTimes.lookupCachedRecord(2026071100001, siteName="summit") is not None
    assert exposureTimes.lookupCachedRecord(2026071100001, siteName="summit", instrument="latiss") is None
    # The instrument-scoped key for the record we *did* store is there.
    assert (
        exposureTimes.lookupCachedRecord(2026071100001, siteName="summit", instrument="lsstcam") is not None
    )


def test_pinning_the_first_probe_instrument_still_answers_the_bare_id() -> None:
    """A hit against the instrument a bare lookup probes first *is* the
    bare-id answer, so pinning it may still warm the bare cache key."""
    assert exposureTimes.isProbeOrderFirst(None) is True
    assert exposureTimes.isProbeOrderFirst("lsstcam") is True
    assert exposureTimes.isProbeOrderFirst("latiss") is False


def test_storeCachedRecord_can_skip_the_bare_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    latiss = {"exposure_id": 7, "obs_end": "latiss", "instrument": "latiss"}
    exposureTimes.storeCachedRecord(7, latiss, siteName="summit", bareKey=False)
    assert exposureTimes.lookupCachedRecord(7, siteName="summit") is None
    assert exposureTimes.lookupCachedRecord(7, siteName="summit", instrument="latiss") == latiss
