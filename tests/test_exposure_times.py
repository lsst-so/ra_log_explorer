"""Tests for `ra_log_explorer.exposureTimes`."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from ra_log_explorer import exposureTimes


def _stubResponse(payload: dict) -> io.BytesIO:
    """Return a file-like that mimics what `urlopen` yields."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


# ----- rspTokenFilePath -----------------------------------------------------


def test_rspTokenFilePath_uses_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(exposureTimes.RSP_TOKEN_FILE_ENV, raising=False)
    assert exposureTimes.rspTokenFilePath() == exposureTimes.DEFAULT_RSP_TOKEN_FILE


def test_rspTokenFilePath_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "tok"
    monkeypatch.setenv(exposureTimes.RSP_TOKEN_FILE_ENV, str(p))
    assert exposureTimes.rspTokenFilePath() == p


def test_rspTokenFilePath_override_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    envP = tmp_path / "env"
    overP = tmp_path / "override"
    monkeypatch.setenv(exposureTimes.RSP_TOKEN_FILE_ENV, str(envP))
    assert exposureTimes.rspTokenFilePath(str(overP)) == overP


def test_rspTokenFilePath_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(exposureTimes.RSP_TOKEN_FILE_ENV, raising=False)
    out = exposureTimes.rspTokenFilePath("~/some/token")
    # ~ must be expanded; the resulting path shouldn't start with `~`.
    assert not str(out).startswith("~")
    assert str(out).endswith("/some/token")


# ----- readRspToken ---------------------------------------------------------


def test_readRspToken_strips_whitespace(tmp_path: Path) -> None:
    p = tmp_path / "tok"
    p.write_text("  abc-def\n")
    assert exposureTimes.readRspToken(p) == "abc-def"


def test_readRspToken_returns_empty_for_whitespace_only(tmp_path: Path) -> None:
    p = tmp_path / "tok"
    p.write_text("   \n  ")
    assert exposureTimes.readRspToken(p) == ""


def test_readRspToken_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        exposureTimes.readRspToken(tmp_path / "nope")


# ----- queryIsot ------------------------------------------------------------


def test_queryIsot_returns_obs_end_on_first_instrument_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append((req.get_full_url(), req.data, dict(req.header_items())))
        return _stubResponse({"columns": ["obs_end"], "data": [["2026-05-20T08:46:16.267000"]]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    iso = exposureTimes.queryIsot(2026051900722, "TOKEN")
    assert iso == "2026-05-20T08:46:16.267000"
    # Only one HTTP call needed: the lsstcam table matched first.
    assert len(seen) == 1
    url, body, headers = seen[0]
    assert url == exposureTimes.CONSDB_URL
    parsedBody = json.loads(body.decode("utf-8"))
    assert "cdb_lsstcam.exposure" in parsedBody["query"]
    assert "2026051900722" in parsedBody["query"]
    # Token must travel as a Bearer auth header — never in the URL or body.
    assert headers["Authorization"] == "Bearer TOKEN"
    assert "TOKEN" not in url
    assert "TOKEN" not in body.decode("utf-8")


def test_queryIsot_falls_through_instruments_until_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the first instrument's table returns no rows, the loop falls
    through to the next. Here we make lsstcam return empty and latiss
    return the row."""
    seen: list[str] = []
    payloads: Any = iter(
        [
            {"columns": ["obs_end"], "data": []},  # lsstcam — empty
            {"columns": ["obs_end"], "data": [["2026-05-20T09:00:00.000"]]},  # latiss
        ]
    )

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append(json.loads(req.data.decode("utf-8"))["query"])
        return _stubResponse(next(payloads))

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    iso = exposureTimes.queryIsot(2026052000100, "TOKEN")
    assert iso == "2026-05-20T09:00:00.000"
    assert "cdb_lsstcam.exposure" in seen[0]
    assert "cdb_latiss.exposure" in seen[1]


def test_queryIsot_returns_None_when_all_instruments_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse({"columns": ["obs_end"], "data": []})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(2026051900722, "TOKEN") is None


def test_queryIsot_uses_only_the_given_instrument_when_specified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seen.append(json.loads(req.data.decode("utf-8"))["query"])
        return _stubResponse({"columns": ["obs_end"], "data": [["x"]]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryIsot(2026051900722, "TOKEN", instrument="latiss")
    assert len(seen) == 1
    assert "cdb_latiss.exposure" in seen[0]


def test_queryIsot_returns_None_for_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError(req.get_full_url(), 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(2026051900722, "TOKEN", instrument="lsstcam") is None


def test_queryIsot_raises_ConsDbError_for_other_HTTP_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError(req.get_full_url(), 500, "server error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    with pytest.raises(exposureTimes.ConsDbError):
        exposureTimes.queryIsot(2026051900722, "TOKEN", instrument="lsstcam")


def test_queryIsot_treats_500_UndefinedTable_as_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConsDB returns HTTP 500 with a psycopg2 ``UndefinedTable`` body
    when an instrument's schema doesn't exist. We must skip to the next
    instrument rather than blowing up the whole lookup."""
    seen: list[str] = []
    undefBody = b'{"message":"(psycopg2.errors.UndefinedTable) relation does not exist"}'
    payloads = iter(
        [
            ("undefined", undefBody),
            ("hit", json.dumps({"columns": ["obs_end"], "data": [["x"]]}).encode("utf-8")),
        ]
    )

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
    iso = exposureTimes.queryIsot(2026051900722, "TOKEN")
    assert iso == "x"
    assert len(seen) == 2  # lsstcam 500 -> latiss hit


def test_queryIsot_raises_for_500_that_is_not_UndefinedTable(
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
        exposureTimes.queryIsot(2026051900722, "TOKEN", instrument="lsstcam")


# ----- on-disk cache -------------------------------------------------------


def test_lookupCached_returns_None_when_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    assert exposureTimes.lookupCached(2026051900722) is None


def test_storeCached_then_lookupCached_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCached(2026051900722, "2026-05-20T08:46:16.267000")
    assert exposureTimes.lookupCached(2026051900722) == "2026-05-20T08:46:16.267000"


def test_storeCached_appends_without_clobbering(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    exposureTimes.storeCached(2026051900722, "iso-a")
    exposureTimes.storeCached(2026051900723, "iso-b")
    assert exposureTimes.lookupCached(2026051900722) == "iso-a"
    assert exposureTimes.lookupCached(2026051900723) == "iso-b"


def test_lookupCached_tolerates_corrupt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = exposureTimes.cachedExposureTimesPath()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not-json{")
    assert exposureTimes.lookupCached(2026051900722) is None


def test_lookupCached_tolerates_unexpected_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = exposureTimes.cachedExposureTimesPath()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(["not", "a", "dict"]))
    assert exposureTimes.lookupCached(2026051900722) is None


def test_queryIsotBatch_returns_resolved_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the batch helper is one round trip per
    instrument, not one per dataId."""
    callCount = 0

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        nonlocal callCount
        callCount += 1
        sentSql = json.loads(req.data.decode("utf-8"))["query"]
        assert "IN (" in sentSql
        return _stubResponse(
            {
                "columns": ["exposure_id", "obs_end"],
                "data": [
                    [2026051900722, "2026-05-20T08:46:16.267000"],
                    [2026051900723, "2026-05-20T08:47:02.724000"],
                ],
            }
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryIsotBatch([2026051900722, 2026051900723], "TOKEN")
    assert out == {
        2026051900722: "2026-05-20T08:46:16.267000",
        2026051900723: "2026-05-20T08:47:02.724000",
    }
    # One call: lsstcam matched everything, so we don't even try the
    # other instruments.
    assert callCount == 1


def test_queryIsotBatch_falls_through_to_other_instruments(
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
                {
                    "columns": ["exposure_id", "obs_end"],
                    "data": [[2026051900722, "2026-05-20T08:46:16.267000"]],
                }
            )
        return _stubResponse(
            {
                "columns": ["exposure_id", "obs_end"],
                "data": [[2026052000100, "2026-05-20T09:00:00.000000"]],
            }
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryIsotBatch([2026051900722, 2026052000100], "TOKEN")
    assert out == {
        2026051900722: "2026-05-20T08:46:16.267000",
        2026052000100: "2026-05-20T09:00:00.000000",
    }
    assert "cdb_lsstcam." in seenQueries[0]
    assert "cdb_latiss." in seenQueries[1]


def test_queryIsotBatch_chunks_oversized_in_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge IN-list would blow ConsDB's SQL-length limit. The helper
    chunks itself so this can't happen."""
    seenQueries: list[str] = []

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        seenQueries.append(json.loads(req.data.decode("utf-8"))["query"])
        return _stubResponse({"columns": ["exposure_id", "obs_end"], "data": []})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    ids = list(range(2026051900000, 2026051901500))  # 1500 dataIds
    exposureTimes.queryIsotBatch(ids, "TOKEN", chunkSize=500)
    # 1500 / 500 = 3 chunks per instrument; loop short-circuits since
    # we never resolve anything, so all 4 instruments are tried.
    assert len(seenQueries) == 3 * 4


def test_queryIsotBatch_falls_through_on_UndefinedTable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 UndefinedTable from one instrument shouldn't fail the
    whole batch — the helper should silently fall through to the next
    instrument."""
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
        # latiss returns one row.
        return _stubResponse(
            {
                "columns": ["exposure_id", "obs_end"],
                "data": [[2026051900722, "2026-05-20T08:46:16.267000"]],
            }
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryIsotBatch([2026051900722], "TOKEN")
    assert out == {2026051900722: "2026-05-20T08:46:16.267000"}
    # lsstcam tried first and 500'd → moved on to latiss → resolved.
    assert "cdb_lsstcam." in calls[0]
    assert "cdb_latiss." in calls[1]


def test_queryIsotBatch_skips_rows_with_missing_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the batch response is missing exposure_id or obs_end columns
    entirely, the batch silently yields nothing rather than indexing
    into a malformed row."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse({"columns": ["something_else"], "data": [["x"]]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryIsotBatch([2026051900722], "TOKEN")
    assert out == {}


def test_queryIsotBatch_skips_rows_with_unexpected_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with an unexpected element shape (e.g. ``None`` instead of
    an int dataId) shouldn't break the batch — that row is just
    skipped."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse(
            {
                "columns": ["exposure_id", "obs_end"],
                "data": [
                    [None, "2026-05-20T08:46:16.267000"],
                    [2026051900722, "2026-05-20T08:46:16.267000"],
                ],
            }
        )

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes.queryIsotBatch([2026051900722], "TOKEN")
    # The valid row landed; the None-id row got dropped.
    assert out == {2026051900722: "2026-05-20T08:46:16.267000"}


def test_postQuery_treats_400_as_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 from ConsDB (typically "no such row" / "invalid query")
    should surface as an empty payload, not as a ConsDbError that
    aborts the whole batch."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError("https://x", 400, "Bad Request", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    out = exposureTimes._postQuery("SELECT 1", "TOKEN")
    assert out == {"columns": [], "data": []}


def test_postQuery_raises_ConsDbError_for_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTP 5xx that isn't an UndefinedTable should bubble out as a
    typed error so the caller can surface it to the user."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        raise HTTPError("https://x", 503, "Unavailable", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    with pytest.raises(exposureTimes.ConsDbError, match="503"):
        exposureTimes._postQuery("SELECT 1", "TOKEN")


def test_storeCached_recovers_from_corrupt_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the existing cache file is corrupt, ``storeCached`` should
    silently overwrite it with a fresh single-entry map rather than
    refusing to record the new value."""
    cachePath = tmp_path / "exposure-times.json"
    cachePath.write_text("this is not json")
    monkeypatch.setattr(exposureTimes, "cachedExposureTimesPath", lambda: cachePath)
    exposureTimes.storeCached(2026051900722, "2026-05-20T08:46:16.267000")
    data = json.loads(cachePath.read_text())
    assert data == {"2026051900722": "2026-05-20T08:46:16.267000"}


def test_queryIsot_handles_missing_obs_end_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the response schema unexpectedly omits the obs_end column we
    return None rather than crashing."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse({"columns": ["something_else"], "data": [["x"]]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(2026051900722, "TOKEN", instrument="lsstcam") is None
