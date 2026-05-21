"""Tests for `ra_log_explorer.exposureTimes`."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError

import pytest

from ra_log_explorer import exposureTimes


def _stubResponse(payload: dict[str, str]) -> io.BytesIO:
    """Return a file-like object that mimics what `urlopen` yields."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def test_exposureTimingsUrl_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exposureTimes.EXPOSURE_TIMINGS_URL_ENV, "https://x/")
    assert exposureTimes.exposureTimingsUrl() == "https://x/"


def test_exposureTimingsUrl_is_None_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(exposureTimes.EXPOSURE_TIMINGS_URL_ENV, raising=False)
    assert exposureTimes.exposureTimingsUrl() is None


def test_queryIsot_returns_isot_for_known_dataId(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reset the lru_cache so this test gets a fresh fetch.
    exposureTimes._loadDay.cache_clear()
    seen: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        seen.append(url)
        return _stubResponse({"2026051900722": "2026-05-20T08:46:16.267"})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    isot = exposureTimes.queryIsot(2026051900722, "https://x/")
    assert isot == "2026-05-20T08:46:16.267"
    assert seen == ["https://x/20260519.json"]


def test_queryIsot_returns_None_when_day_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    exposureTimes._loadDay.cache_clear()

    def fakeUrlopen(url: str) -> Any:
        raise HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(2026051900722, "https://x/") is None


def test_queryIsot_returns_None_when_dataId_missing_in_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposureTimes._loadDay.cache_clear()

    def fakeUrlopen(url: str) -> io.BytesIO:
        return _stubResponse({"2026051900001": "..."})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    assert exposureTimes.queryIsot(2026051900722, "https://x/") is None


def test_queryIsot_propagates_non_404_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    exposureTimes._loadDay.cache_clear()

    def fakeUrlopen(url: str) -> Any:
        raise HTTPError(url, 500, "server error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    with pytest.raises(HTTPError):
        exposureTimes.queryIsot(2026051900722, "https://x/")


def test_queryIsot_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    exposureTimes._loadDay.cache_clear()
    seen: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        seen.append(url)
        return _stubResponse({})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    # Both forms should hit the same URL (no double slash).
    exposureTimes._loadDay.cache_clear()
    exposureTimes.queryIsot(2026051900722, "https://x/")
    exposureTimes._loadDay.cache_clear()
    exposureTimes.queryIsot(2026051900722, "https://x")
    assert seen == ["https://x/20260519.json", "https://x/20260519.json"]


def test_loadDay_lru_cache_avoids_repeat_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    exposureTimes._loadDay.cache_clear()
    calls: list[str] = []

    def fakeUrlopen(url: str) -> io.BytesIO:
        calls.append(url)
        return _stubResponse({"2026051900722": "2026-05-20T08:46:16.267"})

    monkeypatch.setattr(exposureTimes, "urlopen", fakeUrlopen)
    exposureTimes.queryIsot(2026051900722, "https://x/")
    exposureTimes.queryIsot(2026051900722, "https://x/")
    exposureTimes.queryIsot(2026051900000, "https://x/")
    # Three queries for the same day; only one HTTP request.
    assert calls == ["https://x/20260519.json"]
