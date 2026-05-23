"""Tests for `ra_log_explorer.appSettings`."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_loadAppSettings_ignores_unknown_fields(tmpCacheRoot: Path) -> None:
    """A future version may add fields; an older client should still read
    its known field without choking on the extras."""
    import json as _json

    appSettings.settingsPath().write_text(
        _json.dumps({"maxCacheBytes": 42, "futureKnob": "abc", "another": 1})
    )
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == 42


def test_saveAppSettings_creates_cache_root_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """saveAppSettings claims to create the parent dir if needed — pin
    that so a fresh install (no `~/.cache/ra_log_explorer/` yet) works
    on first settings write."""
    target = tmp_path / "fresh-cache-root"
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(target))
    # cache_root() will mkdir the root, but the settings file should also
    # land cleanly when the directory was created from scratch.
    appSettings.saveAppSettings(appSettings.AppSettings(maxCacheBytes=7))
    assert appSettings.loadAppSettings().maxCacheBytes == 7
