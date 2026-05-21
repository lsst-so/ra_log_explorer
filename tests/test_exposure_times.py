"""Tests for `ra_log_explorer.exposureTimes`."""

from __future__ import annotations

import datetime
import io
import json
from typing import Any
from urllib.error import HTTPError

import pytest

from ra_log_explorer import exposureTimes

# A dataId whose dayObs (20200101) is definitely in the past for any
# realistic test run — use this whenever we want the cached path to be
# selected without monkeypatching the clock.
PAST_DATAID = 2020010100001
PAST_DAYOBS = PAST_DATAID // 100000


def _stubResponse(payload: dict[str, str]) -> io.BytesIO:
    """Return a file-like object that mimics what `urlopen` yields."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _clearCaches() -> None:
    exposureTimes._fetchDayCached.cache_clear()


def test_exposureTimingsUrl_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exposureTimes.EXPOSURE_TIMINGS_URL_ENV, "https://x/")
    assert exposureTimes.exposureTimingsUrl() == "https://x/"


def test_exposureTimingsUrl_is_None_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(exposureTimes.EXPOSURE_TIMINGS_URL_ENV, raising=False)
    assert exposureTimes.exposureTimingsUrl() is None


# ----- queryIsot --------------------------------------------------------------


def test_queryIsot_returns_isot_for_known_dataId(monkeypatch: pytest.MonkeyPatch) -> None:
    _clearCaches()
    seen: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        seen.append(url)
        return _stubResponse({str(PAST_DATAID): "2020-01-02T03:04:05.067"})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    isot = exposureTimes.queryIsot(PAST_DATAID, "https://x/")
    assert isot == "2020-01-02T03:04:05.067"
    assert seen == [f"https://x/{PAST_DAYOBS}.json"]


def test_queryIsot_returns_None_when_day_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clearCaches()

    def fakeUrlopen(url: str) -> Any:
        raise HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(PAST_DATAID, "https://x/") is None


def test_queryIsot_returns_None_when_dataId_missing_in_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clearCaches()

    def fakeUrlopen(url: str) -> io.BytesIO:
        return _stubResponse({"some-other-id": "..."})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(PAST_DATAID, "https://x/") is None


def test_queryIsot_propagates_non_404_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _clearCaches()

    def fakeUrlopen(url: str) -> Any:
        raise HTTPError(url, 500, "server error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    with pytest.raises(HTTPError):
        exposureTimes.queryIsot(PAST_DATAID, "https://x/")


def test_queryIsot_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _clearCaches()
    seen: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        seen.append(url)
        return _stubResponse({})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    _clearCaches()
    exposureTimes.queryIsot(PAST_DATAID, "https://x/")
    _clearCaches()
    exposureTimes.queryIsot(PAST_DATAID, "https://x")
    assert seen == [f"https://x/{PAST_DAYOBS}.json"] * 2


def test_queryIsot_caches_past_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """For past dayObs values the cached fetch path is used."""
    _clearCaches()
    calls: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        calls.append(url)
        return _stubResponse({str(PAST_DATAID): "2020-01-02T03:04:05.067"})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryIsot(PAST_DATAID, "https://x/")
    exposureTimes.queryIsot(PAST_DATAID, "https://x/")
    exposureTimes.queryIsot(PAST_DAYOBS * 100000 + 99, "https://x/")
    # Three queries against the same day_obs; one HTTP request.
    assert calls == [f"https://x/{PAST_DAYOBS}.json"]


def test_queryIsot_bypasses_cache_for_current_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """The current dayObs is fetched fresh every time — the file is still being written."""
    _clearCaches()
    monkeypatch.setattr(exposureTimes, "getCurrentDayObsInt", lambda: 20260520)
    calls: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        calls.append(url)
        return _stubResponse({"2026052000001": "2026-05-20T20:00:00.000"})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryIsot(2026052000001, "https://x/")
    exposureTimes.queryIsot(2026052000001, "https://x/")
    # Two queries for the current dayObs => two HTTP requests, no caching.
    assert calls == ["https://x/20260520.json", "https://x/20260520.json"]


def test_queryIsot_bypasses_cache_for_future_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Future dayObs (clock-skew guard) also bypasses the cache."""
    _clearCaches()
    monkeypatch.setattr(exposureTimes, "getCurrentDayObsInt", lambda: 20260520)
    calls: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        calls.append(url)
        return _stubResponse({})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryIsot(2026052100001, "https://x/")
    exposureTimes.queryIsot(2026052100001, "https://x/")
    assert calls == ["https://x/20260521.json", "https://x/20260521.json"]


# ----- getCurrentDayObs --------------------------------------------------------


def test_getCurrentDayObsDatetime_returns_a_date() -> None:
    out = exposureTimes.getCurrentDayObsDatetime()
    assert isinstance(out, datetime.date)


def test_getCurrentDayObsInt_is_8_digit_yyyymmdd() -> None:
    n = exposureTimes.getCurrentDayObsInt()
    assert 19000000 < n < 30000000
    # Reconstruct the date from the int — if the format is right this round-trips.
    asDate = datetime.datetime.strptime(str(n), "%Y%m%d").date()
    assert asDate == exposureTimes.getCurrentDayObsDatetime()


def test_getCurrentDayObs_rolls_at_utc_minus_12(monkeypatch: pytest.MonkeyPatch) -> None:
    """Just before noon UTC the day_obs is still yesterday's date."""

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:  # type: ignore[override]
            # 2026-05-20T11:59:00Z — just before the UTC-12 rollover
            # (which lands at 12:00:00 UTC) — so day_obs is still 20260519.
            return datetime.datetime(2026, 5, 20, 11, 59, 0, tzinfo=tz or datetime.timezone.utc)

    monkeypatch.setattr(exposureTimes.datetime, "datetime", FrozenDatetime)
    assert exposureTimes.getCurrentDayObsInt() == 20260519


def test_getCurrentDayObs_rolls_at_noon_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """At noon UTC the day_obs has advanced to today's UTC date."""

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:  # type: ignore[override]
            return datetime.datetime(2026, 5, 20, 12, 0, 0, tzinfo=tz or datetime.timezone.utc)

    monkeypatch.setattr(exposureTimes.datetime, "datetime", FrozenDatetime)
    assert exposureTimes.getCurrentDayObsInt() == 20260520
