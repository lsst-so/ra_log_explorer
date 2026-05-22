"""Tests for `ra_log_explorer.appSettings`."""

from __future__ import annotations

from pathlib import Path

from ra_log_explorer import appSettings


def test_loadAppSettings_returns_defaults_when_no_file(tmpCacheRoot: Path) -> None:
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == appSettings.DEFAULT_MAX_CACHE_BYTES


def test_save_then_load_roundtrips(tmpCacheRoot: Path) -> None:
    appSettings.saveAppSettings(appSettings.AppSettings(maxCacheBytes=123456789))
    loaded = appSettings.loadAppSettings()
    assert loaded.maxCacheBytes == 123456789


def test_loadAppSettings_falls_back_on_corrupt_file(tmpCacheRoot: Path) -> None:
    # Plant a non-JSON file at the settings path — load should not raise.
    appSettings.settingsPath().write_text("not valid json")
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == appSettings.DEFAULT_MAX_CACHE_BYTES


def test_loadAppSettings_falls_back_on_missing_field(tmpCacheRoot: Path) -> None:
    # An empty JSON object should yield default fields, not crash.
    appSettings.settingsPath().write_text("{}")
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == appSettings.DEFAULT_MAX_CACHE_BYTES
