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


def test_queryIsot_handles_missing_obs_end_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the response schema unexpectedly omits the obs_end column we
    return None rather than crashing."""

    def fakeUrlopen(req: Any, **_kw: Any) -> Any:
        return _stubResponse({"columns": ["something_else"], "data": [["x"]]})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(2026051900722, "TOKEN", instrument="lsstcam") is None
